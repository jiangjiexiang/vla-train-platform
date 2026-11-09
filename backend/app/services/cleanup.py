"""历史任务与产物的清理。

平台跑久了会积累三类垃圾：
  1. 已结束的 K8s 对象（PyTorchJob / Pod）——本仓已通过 ttlSecondsAfterFinished 处理新任务，
     但历史遗留的还在；
  2. history 数据库里的任务记录——列表页会越来越长；
  3. checkpoint 目录——单个真实训练产物就有 1.3G，不清理磁盘很快耗尽。

这里提供**按条件删除**的能力，而不是无差别清库。清理是不可逆操作，
所以每个边界都要明确：默认不删仍在运行的任务，默认不删 checkpoint，
要求调用方显式说明范围。

本模块只管「该删哪些」，真正的删除动作由 PlatformService 执行——
这样历史库与 K8s 两侧的删除可以放在同一个事务性流程里。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# 可以清理的终态。运行中的任务即使早于截止时间也不能删——
# 那是用户正在等的结果。
TERMINAL_STATUSES = ("SUCCEEDED", "FAILED", "CANCELLED")


@dataclass
class CleanupPlan:
    """一次清理要做什么。先算出来给用户看，确认后再执行。"""

    job_names: list[str]
    checkpoints: list[str]
    keep: list[str]
    errors: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "jobs": self.job_names,
            "checkpoints": self.checkpoints,
            "kept_running": self.keep,
            "errors": self.errors,
            "total_jobs": len(self.job_names),
            "total_checkpoints": len(self.checkpoints),
        }


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def plan_cleanup(
    jobs: list[dict[str, Any]],
    *,
    older_than_days: int,
    statuses: tuple[str, ...] = TERMINAL_STATUSES,
    keep_latest: int = 0,
    include_checkpoints: bool = False,
    now: datetime | None = None,
) -> CleanupPlan:
    """算出该清理哪些任务。

    判定顺序（每一步都可以否决删除）：
      1. 状态必须是终态，运行中的一律保留；
      2. 结束时间（没有就用创建时间）必须早于截止时间；
      3. 最新的 keep_latest 个任务永远保留——避免「清完发现最近的记录也没了」，
         这在排查刚出的问题时很致命。
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=older_than_days)

    eligible: list[tuple[datetime, dict[str, Any]]] = []
    keep: list[str] = []
    errors: list[str] = []

    for job in jobs:
        name = str(job.get("name") or "")
        status = str(job.get("status") or "")
        if status not in statuses:
            keep.append(name)
            continue
        # 用结束时间；老记录可能没有，退回创建时间
        finished = _parse_time(job.get("finished_at")) or _parse_time(job.get("created_at"))
        if finished is None:
            errors.append(f"{name}: 无法解析时间，跳过")
            continue
        if finished >= cutoff:
            continue
        eligible.append((finished, job))

    # 最新的 keep_latest 个不删
    eligible.sort(key=lambda pair: pair[0], reverse=True)
    protected = {str(job.get("name")) for _, job in eligible[:keep_latest]} if keep_latest else set()
    selected = [(when, job) for when, job in eligible if str(job.get("name")) not in protected]

    names = [str(job.get("name")) for _, job in selected]
    checkpoints = names if include_checkpoints else []
    return CleanupPlan(job_names=names, checkpoints=checkpoints, keep=keep, errors=errors)
