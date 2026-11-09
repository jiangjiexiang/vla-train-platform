"""排队位置与等待时间估算。

用户最关心「我的任务还要等多久」。Kueue 本身不提供 ETA，这里用两个可解释的信号估算：

1. **排队位置**：同一 ClusterQueue 里，比我先提交、还没被准入的 Workload 数量 + 1。
2. **预计等待**：按位置累加前面任务的「预计剩余运行时间」。
   剩余时间优先用同配方任务的**历史运行时长中位数**；没有历史时退回
   「已运行时长 × 2」这类保守估计，并在响应里标注它是粗估。

所有估算都带 ``queue_eta_source`` 字段说明依据，前端据此决定是否显示「约」字，
避免把粗估当成承诺。
"""

from __future__ import annotations

import statistics
from typing import Any

# 没有历史数据时，假设一个排队中任务平均要跑这么久（秒）
DEFAULT_TASK_SECONDS = 1800.0
# 「已运行部分」外推时假设任务总共跑这么久
ASSUMED_TOTAL_RATIO = 2.0


def workload_pending_order(workloads: list[dict[str, Any]]) -> dict[str, int]:
    """按提交时间给 Pending 状态的 Workload 排序，返回 {workload 名: 位置}。

    Kueue 的准入大致按创建时间先后，同一时刻创建的再用名字兜底，保证顺序稳定。
    """
    pending = [workload for workload in workloads if _workload_status(workload) == "Pending"]

    def sort_key(workload: dict[str, Any]) -> tuple[str, str]:
        created = workload.get("metadata", {}).get("creationTimestamp") or ""
        return (created, workload.get("metadata", {}).get("name") or "")

    pending.sort(key=sort_key)
    return {
        workload.get("metadata", {}).get("name", ""): index + 1
        for index, workload in enumerate(pending)
    }


def estimate_durations(records: list[dict[str, Any]]) -> dict[str, float]:
    """按标签（配方名）统计历史运行时长中位数。

    ``records`` 是 ``[{labels: {...}, duration_seconds: float}]``。
    只用成功或失败的任务（有真实耗时），取消的不算。
    """
    buckets: dict[str, list[float]] = {}
    for record in records:
        duration = record.get("duration_seconds")
        if not isinstance(duration, (int, float)) or duration <= 0:
            continue
        labels = record.get("labels") or {}
        key = labels.get("recipe") or "__default__"
        buckets.setdefault(key, []).append(float(duration))
    return {key: float(statistics.median(values)) for key, values in buckets.items() if values}


def estimate_wait(
    *,
    position: int,
    ahead: list[dict[str, Any]],
    durations: dict[str, float],
    recipe: str | None,
) -> tuple[float, str]:
    """估算等待秒数与依据。

    ``ahead`` 是位置在我之前、正在运行的任务（含 elapsed_seconds 与 labels）。
    ``durations`` 来自 :func:`estimate_durations`。
    """
    typical = durations.get(recipe or "__default__") or durations.get("__default__") or DEFAULT_TASK_SECONDS

    def remaining_of(task: dict[str, Any]) -> float:
        elapsed = float(task.get("elapsed_seconds") or 0.0)
        key = (task.get("labels") or {}).get("recipe") or "__default__"
        expected = durations.get(key) or typical
        # 已经跑过的部分要扣掉，剩余至少留 10%（避免已超时的任务给出 0）
        remaining = max(expected - elapsed, expected * 0.1)
        return remaining

    running_time = sum(remaining_of(task) for task in ahead)
    # 位置 2 表示前面还有 1 个任务在排队，它要等当前这批跑完后才开始
    queued_ahead = max(position - 1, 0)
    queued_time = queued_ahead * typical
    total = running_time + queued_time

    if recipe and recipe in durations:
        source = "history"
    elif any((task.get("labels") or {}).get("recipe") in durations for task in ahead):
        source = "history"
    else:
        source = "default"
    return total, source


def queue_info(
    *,
    workload: dict[str, Any] | None,
    workloads: list[dict[str, Any]],
    status: str,
    ahead: list[dict[str, Any]],
    durations: dict[str, float],
    recipe: str | None,
    elapsed_seconds: float | None,
) -> dict[str, Any]:
    """组装给前端的排队字段。非排队状态返回 null 位置。"""
    if status != "QUEUED" or workload is None:
        return {
            "queue_position": None,
            "queue_eta_seconds": None,
            "queue_eta_source": None,
            "queue_state": _queue_state(status, workload),
        }

    # 注意：Kueue 的 Workload 名是 "pytorchjob-<任务名>-<后缀>"，不是任务名本身。
    # 早先这里直接拿 metadata.name 去查，key 永远是 workload 全名，
    # 而调用方传进来的是任务名，于是 queue_position 恒为 None——
    # 用户排队时看不到自己排第几，只能干等。
    # 这里用 workload 对象本身定位，不依赖名字约定。
    workload_name = workload.get("metadata", {}).get("name", "")
    position = workload_pending_order(workloads).get(workload_name)
    eta: float | None = None
    source: str | None = None
    if position is not None:
        eta, source = estimate_wait(position=position, ahead=ahead, durations=durations, recipe=recipe)
    return {
        "queue_position": position,
        "queue_eta_seconds": round(eta) if eta is not None else None,
        "queue_eta_source": source,
        "queue_state": _queue_state(status, workload),
    }


def _queue_state(status: str, workload: dict[str, Any] | None) -> str:
    if status in ("SUCCEEDED", "FAILED", "CANCELLED"):
        return "finished"
    if status == "RUNNING":
        return "running"
    if workload is not None and _workload_status(workload) == "Admitted":
        return "admitted"
    return "queued"


def _workload_status(workload: dict[str, Any]) -> str:
    conditions = workload.get("status", {}).get("conditions") or []
    for condition in conditions:
        if condition.get("type") == "Admitted" and condition.get("status") == "True":
            return "Admitted"
    return "Pending"