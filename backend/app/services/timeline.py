"""作业阶段时间轴。

用户最困惑的场景是「任务提交了，然后呢？」。Kueue 与 PyTorchJob 的状态散落在
多个对象里（Workload 条件、Job 条件、Pod 阶段、容器状态），把它们拼成一条
有先后、有时间戳的流水线，用户就能一眼看出卡在哪：

    已提交 → 已入队 → 已准入 → 拉镜像 → 容器启动 → 训练中 → 已结束

每个阶段给三种状态：``done``（已完成）、``current``（进行中）、``pending``（未到达），
以及失败时的 ``failed``。时间戳能取到就带，取不到留空——不要编造时间。
"""

from __future__ import annotations

from typing import Any

STAGES = [
    ("submitted", "已提交"),
    ("queued", "已入队"),
    ("admitted", "已准入"),
    ("image", "拉取镜像"),
    ("container", "容器启动"),
    ("running", "训练中"),
    ("finished", "已结束"),
]


def build_timeline(
    *,
    job: dict[str, Any],
    workload: dict[str, Any] | None,
    pod: dict[str, Any] | None,
    status: str,
) -> dict[str, Any]:
    """组装阶段时间轴。

    只根据实际观测到的对象状态推断，不猜测。例如没有 Pod 时，后面的阶段一律 pending。
    """
    job_meta = job.get("metadata", {})
    job_status = job.get("status", {}) or {}
    created_at = job_meta.get("creationTimestamp")

    reached: dict[str, dict[str, Any]] = {}

    if created_at:
        reached["submitted"] = {"at": _iso(created_at)}

    # Workload 存在 = 已入队
    if workload is not None:
        wl_meta = workload.get("metadata", {})
        reached["queued"] = {"at": _iso(wl_meta.get("creationTimestamp") or created_at)}
        admitted = _condition(workload, "Admitted")
        if admitted and admitted.get("status") == "True":
            reached["admitted"] = {"at": _iso(admitted.get("lastTransitionTime"))}

    # Pod 存在 = 已准入（Pod 通常由 PyTorchJob 在准入后创建）
    if pod is not None:
        reached.setdefault("admitted", {"at": _pod_time(pod, "created")})
        started, _finished = _container_times(pod)
        if started:
            # 容器启动过就说明镜像已经拉好了，且训练已经开始
            reached["image"] = {"at": started}
            reached["container"] = {"at": started}
            reached["running"] = {"at": started}

    job_running = _condition(job, "Running")
    if job_running and job_running.get("status") == "True":
        reached.setdefault("running", {"at": _iso(job_running.get("lastTransitionTime"))})

    terminal = _condition(job, "Succeeded") or _condition(job, "Failed")
    if status in ("SUCCEEDED", "FAILED", "CANCELLED"):
        reached["finished"] = {
            "at": _iso(job_status.get("completionTime") or job_status.get("completion_time"))
        }
    elif terminal:
        reached["finished"] = {"at": _iso(terminal.get("lastTransitionTime"))}

    return _render(reached, status)


def _render(reached: dict[str, dict[str, Any]], status: str) -> dict[str, Any]:
    """把「已到达的阶段」渲染成带状态的列表。

    状态规则很简单，关键是**按顺序**判断：
      - 没到达 → pending
      - 已到达 且 下一个阶段也到达 → done
      - 已到达 但 下一个未到达 → current（就是卡在这儿）
      - 最后一个阶段已到达 → done；FAILED 时标 failed
    """
    keys = [stage for stage, _label in STAGES]
    items = []
    current_stage = None
    for index, (key, label) in enumerate(STAGES):
        info = reached.get(key)
        if info is None:
            state = "pending"
        else:
            is_last = index == len(keys) - 1
            next_reached = (not is_last) and (keys[index + 1] in reached)
            if is_last:
                state = "failed" if status == "FAILED" else "done"
            elif next_reached:
                state = "done"
            else:
                state = "current"
                if current_stage is None:
                    current_stage = key
        items.append({"stage": key, "label": label, "state": state, "at": (info or {}).get("at")})

    # 已结束的任务没有「当前阶段」——所有阶段都走完了。
    # 之前的 bug：容器状态缺失时 image 被标成 current，导致「已结束」和
    # 「卡在拉镜像」同时出现，用户被误导。
    if status in ("SUCCEEDED", "FAILED", "CANCELLED"):
        current_stage = None
        for item in items:
            if item["state"] == "current":
                item["state"] = "done"
    return {"current_stage": current_stage, "items": items}


def _condition(obj: dict[str, Any], condition_type: str) -> dict[str, Any] | None:
    for condition in (obj.get("status") or {}).get("conditions") or []:
        if condition.get("type") == condition_type:
            return condition
    return None


def _pod_time(pod: dict[str, Any], which: str) -> str | None:
    meta = pod.get("metadata", {})
    return _iso(meta.get("creationTimestamp") or meta.get("creation_timestamp")) if which == "created" else None


def _container_times(pod: dict[str, Any]) -> tuple[str | None, str | None]:
    """容器的 (启动时间, 结束时间)。

    任务结束后容器状态是 ``terminated`` 而不是 ``running``——只看 running 会让
    已经跑完的任务在时间轴上显示「容器未启动」，这是曾经的真实 bug。
    两种状态都要处理：running 的 startedAt，或 terminated 的 startedAt。
    """
    pod_status = pod.get("status") or {}
    for container_status in pod_status.get("container_statuses") or pod_status.get("containerStatuses") or []:
        state = container_status.get("state") or {}
        running = state.get("running") or {}
        started = _iso(running.get("startedAt") or running.get("started_at"))
        if started:
            return started, None
        terminated = state.get("terminated") or {}
        if terminated:
            return (
                _iso(terminated.get("startedAt") or terminated.get("started_at")),
                _iso(terminated.get("finishedAt") or terminated.get("finished_at")),
            )
    return None, None


def _container_start(pod: dict[str, Any]) -> str | None:
    """镜像拉取完成的近似时间：容器开始运行的时刻。"""
    return _container_times(pod)[0]


def _iso(value: Any) -> str | None:
    """K8s 客户端有时给 datetime，有时给字符串，统一成 ISO-8601 Z 格式。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value.replace("+00:00", "Z")
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat().replace("+00:00", "Z")
    return str(value).replace("+00:00", "Z")