"""排队根因分析与出路。

Kueue 只告诉你「没被准入」，不告诉你「为什么」和「怎么办」。本模块把集群状态
归一到 5 类根因（参考火山引擎 veMLP 的产品设计），每条都给出**可直接执行的动作**：

1. ``quota_full``      队列配额已满——有任务在跑，等它结束
2. ``high_priority``   被更高优先级任务抢占——提高优先级或等
3. ``user_quota``      个人配额用完——取消自己的旧任务或申请提额
4. ``fragmentation``   资源碎片——剩余 GPU 不足以满足本次申请
5. ``resource_group``  资源组/节点不可用——集群层面问题，需运维介入

关键设计：**没有诊断出来就不猜**。返回 ``None`` 比给一个错误的根因更有价值，
因为错误的建议会让用户白等或误操作。
"""

from __future__ import annotations

from typing import Any


def diagnose_queue_block(
    *,
    job_gpus: int,
    cluster_queue: dict[str, Any] | None,
    workloads: list[dict[str, Any]],
    job_priority: str,
    user_usage: dict[str, Any] | None = None,
    user_quota: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """分析一个 Pending 任务为什么排不上。

    所有入参都来自平台已有的数据源，不额外请求集群。
    返回 ``{"code", "title", "message", "actions": [{"label", "hint"}]}`` 或 None。
    """
    nominal = _nominal_gpu(cluster_queue)
    admitted_gpu = _admitted_gpu(workloads)
    pending = [workload for workload in workloads if _condition_status(workload, "Admitted") != "True"]

    # 1) 资源碎片：队列还有余量，但不够这次申请
    if nominal is not None and admitted_gpu is not None:
        free = nominal - admitted_gpu
        if 0 < free < job_gpus:
            return {
                "code": "fragmentation",
                "title": "资源碎片",
                "message": f"队列还有 {free} 卡空闲，但本次需要 {job_gpus} 卡，需要等前面的任务整体释放。",
                "actions": [
                    {"label": "等当前任务结束", "hint": "碎片会在运行中的任务结束后合并"},
                    {"label": "降低本次卡数", "hint": "单卡任务更容易被调度"},
                ],
            }

    # 2) 高优先级插队
    priority_rank = {"low": 0, "normal": 1, "high": 2}
    my_rank = priority_rank.get(job_priority, 1)
    blocking = [
        workload
        for workload in pending
        if priority_rank.get(_priority_of(workload), 1) > my_rank
    ]
    if blocking:
        return {
            "code": "high_priority",
            "title": "被更高优先级任务插队",
            "message": f"有 {len(blocking)} 个高优先级任务排在前面，它们会先获得资源。",
            "actions": [
                {"label": "等待高优任务完成", "hint": "高优任务通常也会尽快结束"},
                {"label": "提高本次优先级", "hint": "需要在提交时选择「高」，或联系管理员"},
            ],
        }

    # 3) 个人配额
    if user_usage and user_quota:
        max_gpus = user_quota.get("max_gpus")
        used = user_usage.get("gpu_in_use") or 0
        if max_gpus is not None and used + job_gpus > max_gpus:
            return {
                "code": "user_quota",
                "title": "个人 GPU 配额不足",
                "message": f"你已使用 {used} 卡，本次需要 {job_gpus} 卡，超出个人上限 {max_gpus} 卡。",
                "actions": [
                    {"label": "取消自己的旧任务", "hint": "取消后会立即释放配额"},
                    {"label": "申请提高配额", "hint": "联系管理员在「用户与配额」页调整"},
                ],
            }
        max_jobs = user_quota.get("max_active_jobs")
        active = user_usage.get("active_jobs") or 0
        if max_jobs is not None and active >= max_jobs:
            return {
                "code": "user_quota",
                "title": "个人任务数已达上限",
                "message": f"你已有 {active} 个活跃任务，上限为 {max_jobs} 个。",
                "actions": [{"label": "取消自己的旧任务", "hint": "释放活跃任务名额"}],
            }

    # 4) 队列配额已满（最常见的正常情况）
    if nominal is not None and admitted_gpu is not None and admitted_gpu >= nominal:
        running = _running_count(workloads)
        if running == 0:
            # 理论上不该出现：没有运行中任务却算满配额，说明有残留对象
            message = (
                f"队列共 {nominal} 卡，但没有检测到运行中的任务，可能是已结束的任务尚未释放配额。"
                "请稍等或联系运维检查 Kueue 的 Workload 是否卡在未结束状态。"
            )
        else:
            message = (
                f"队列共 {nominal} 卡，当前已全部分配给 {running} 个运行中的任务。"
                "等其中任何一个结束，你的任务会自动开始。"
            )
        return {
            "code": "quota_full",
            "title": "队列配额已满",
            "message": message,
            "actions": [
                {"label": "等待自动开始", "hint": "无需任何操作，Kueue 会自动放行"},
                {"label": "取消运行中的任务", "hint": "仅在你确认那些任务可以中断时"},
            ],
        }

    # 5) 集群层面：没有可用配额定义
    if nominal is None:
        return {
            "code": "resource_group",
            "title": "集群队列配置异常",
            "message": "无法读取 ClusterQueue 的 GPU 配额，可能是 Kueue 未正确安装或队列名不匹配。",
            "actions": [{"label": "联系运维", "hint": "检查 ClusterQueue vla-cluster-queue 是否存在"}],
        }

    return None


# ---------------------------------------------------------------------------
# 取值辅助
# ---------------------------------------------------------------------------
def _nominal_gpu(cluster_queue: dict[str, Any] | None) -> int | None:
    """从 ClusterQueue 读 nominalQuota 里的 GPU 总数。"""
    if not isinstance(cluster_queue, dict):
        return None
    try:
        groups = cluster_queue["spec"]["resourceGroups"]
        total = 0
        for group in groups:
            for flavor in group.get("flavors", []):
                for resource in flavor.get("resources", []):
                    if resource.get("name") == "nvidia.com/gpu":
                        total += int(resource.get("nominalQuota", 0))
        return total
    except (KeyError, TypeError, ValueError):
        return None


def _admitted_gpu(workloads: list[dict[str, Any]]) -> int | None:
    """所有已准入 Workload 占用的 GPU 之和。"""
    total = 0
    for workload in workloads:
        if _condition_status(workload, "Admitted") != "True":
            continue
        # 已结束的 Workload 不再占用配额
        if _condition_status(workload, "Finished") == "True":
            continue
        total += _workload_gpu(workload)
    return total


def _workload_gpu(workload: dict[str, Any]) -> int:
    try:
        for pod_set in workload["spec"]["podSets"]:
            requests = pod_set.get("template", {}).get("spec", {}).get("containers", [{}])[0].get("resources", {})
            gpu = requests.get("requests", {}).get("nvidia.com/gpu")
            if gpu:
                return int(gpu) * int(pod_set.get("count", 1))
    except (KeyError, IndexError, TypeError, ValueError):
        return 0
    return 0


def _priority_of(workload: dict[str, Any]) -> str:
    labels = workload.get("metadata", {}).get("labels", {})
    return str(labels.get("kueue.x-k8s.io/priority-class") or "normal")


def _running_count(workloads: list[dict[str, Any]]) -> int:
    """真正还在跑的任务数。

    已准入但已经 Finished 的 Workload 仍会留在集群里（对应已完成的历史任务），
    把它们算进「运行中」会让提示变成「配额分给了 5 个运行中任务」这种明显错误的话。
    """
    count = 0
    for workload in workloads:
        if _condition_status(workload, "Admitted") != "True":
            continue
        if _condition_status(workload, "Finished") == "True":
            continue
        count += 1
    return count


def _condition_status(workload: dict[str, Any], condition_type: str) -> str | None:
    for condition in workload.get("status", {}).get("conditions") or []:
        if condition.get("type") == condition_type:
            return condition.get("status")
    return None