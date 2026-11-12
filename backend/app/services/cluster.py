from __future__ import annotations

import copy
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import ServiceError
from .job_views import _condition_true, _dict, _group_pods_by_job, _list, _workload_status
from .queue_block import diagnose_queue_block
from .queue_est import estimate_durations, queue_info


class ClusterServiceMixin:
    def cluster_summary(self) -> dict[str, Any]:
        """集群总览，使用短 TTL 缓存合并总览页和预检的重复 K8s 查询。"""
        ttl = self.settings.cluster_summary_ttl
        if ttl > 0:
            with self._cluster_lock:
                cached = self._cluster_cache
                if cached is not None and (time.monotonic() - cached[0]) < ttl:
                    return copy.deepcopy(cached[1])
        result = self._cluster_summary_uncached()
        if ttl > 0:
            with self._cluster_lock:
                self._cluster_cache = (time.monotonic(), copy.deepcopy(result))
        return result

    def _cluster_summary_uncached(self) -> dict[str, Any]:
        nodes = self.kubernetes.list_nodes()
        cluster_pods = self.kubernetes.list_training_pods()
        allocated_by_node: dict[str, int] = {}
        for pod in cluster_pods:
            status = _dict(pod.get("status"))
            if status.get("phase") in ("Succeeded", "Failed"):
                continue
            node_name = pod.get("spec", {}).get("node_name") or pod.get("spec", {}).get("nodeName")
            if not node_name:
                continue
            allocated_by_node[node_name] = allocated_by_node.get(node_name, 0) + _pod_gpu_request(pod)

        node_items = []
        for node in nodes:
            name = node.get("metadata", {}).get("name", "")
            status = _dict(node.get("status"))
            resources = status.get("allocatable") or status.get("capacity") or {}
            gpu_total = _gpu_quantity(resources.get("nvidia.com/gpu", 0))
            gpu_allocated = allocated_by_node.get(name, 0)
            ready = any(
                condition.get("type") == "Ready" and _condition_true(condition)
                for condition in _list(status.get("conditions"))
            )
            node_items.append(
                {
                    "name": name,
                    "ready": ready,
                    "gpu_total": gpu_total,
                    "gpu_allocated": gpu_allocated,
                }
            )

        gpu_total = sum(node["gpu_total"] for node in node_items)
        gpu_allocated = sum(node["gpu_allocated"] for node in node_items)
        job_items = self._list_jobs(cluster_pods)["items"]
        counts = {key: 0 for key in ("queued", "running", "succeeded", "failed", "cancelled")}
        for item in job_items:
            key = item["status"].lower()
            if key in counts:
                counts[key] += 1
        return {
            "gpu_total": gpu_total,
            "gpu_allocated": gpu_allocated,
            "gpu_available": max(0, gpu_total - gpu_allocated),
            "node_count": len(node_items),
            "nodes": node_items,
            "gpu_metrics": self.gpu_metrics(),
            "jobs": counts,
        }

    def queues(self) -> dict[str, Any]:
        local_queue = self.kubernetes.get_local_queue()
        if local_queue is None:
            raise ServiceError(503, f"Kueue LocalQueue {self.settings.queue} not found")
        cluster_queue_name = local_queue.get("spec", {}).get("clusterQueue") or self.settings.cluster_queue
        cluster_queue = self.kubernetes.get_cluster_queue(cluster_queue_name)
        if cluster_queue is None:
            raise ServiceError(503, f"Kueue ClusterQueue {cluster_queue_name} not found")

        workloads = self.kubernetes.list_workloads()
        jobs = self.kubernetes.list_jobs()
        pods_by_job = _group_pods_by_job(self.kubernetes.list_namespace_pods())
        queue_items = []
        for job in jobs:
            pods = pods_by_job.get(job.get("metadata", {}).get("name", ""), [])
            workload = self._matching_workload(job, workloads, pods)
            response = self._job_response(job, workload, pods)
            if response["queue"] == self.settings.queue:
                queue_items.append(
                    {
                        "name": response["name"],
                        "priority": response["priority"],
                        "status": response["status"],
                        "created_at": response["created_at"],
                    }
                )
        queue_items.sort(key=lambda item: item.get("created_at") or "", reverse=True)

        status = _dict(local_queue.get("status"))
        pending = status.get("pendingWorkloads")
        admitted = status.get("admittedWorkloads")
        if admitted is None:
            admitted = sum(1 for workload in workloads if _workload_status(workload) == "Admitted")
        if pending is None:
            pending = sum(1 for workload in workloads if _workload_status(workload) == "Pending")

        gpu_used = _queue_gpu_used(status.get("flavorsUsage", status.get("flavorUsage", [])))
        if gpu_used is None:
            gpu_used = sum(
                _workload_gpus(workload)
                for workload in workloads
                if _workload_status(workload) == "Admitted"
            )
        return {
            "items": [
                {
                    "name": self.settings.queue,
                    "cluster_queue": cluster_queue_name,
                    "gpu_quota": _cluster_queue_quota(cluster_queue),
                    "gpu_used": gpu_used,
                    "pending": int(pending),
                    "admitted": int(admitted),
                    "items": queue_items,
                }
            ]
        }

    def _annotate_queue(
        self,
        live: dict[str, dict[str, Any]],
        workloads: list[dict[str, Any]],
        cluster_queue: dict[str, Any] | None = None,
    ) -> None:
        """统一计算排队位置、预计等待和阻塞原因，避免逐任务重复查询。"""
        durations: dict[str, float] = {}
        try:
            records = [
                {"labels": item.get("labels") or {}, "duration_seconds": item.get("duration_seconds")}
                for item in live.values()
                if item.get("status") in ("SUCCEEDED", "FAILED")
            ]
            durations = estimate_durations(records)
        except Exception:  # noqa: BLE001 - 估算失败不能影响任务列表本身
            durations = {}

        running = [
            {
                "labels": item.get("labels") or {},
                "elapsed_seconds": item.get("duration_seconds") or 0.0,
            }
            for item in live.values()
            if item.get("status") == "RUNNING"
        ]

        for name, item in live.items():
            workload = self._workload_for_name(workloads, name)
            info = queue_info(
                workload=workload,
                workloads=workloads,
                status=str(item.get("status")),
                ahead=running,
                durations=durations,
                recipe=(item.get("labels") or {}).get("recipe"),
                elapsed_seconds=item.get("duration_seconds"),
            )
            item.update(info)
            if info.get("queue_state") == "queued" and workload is not None:
                try:
                    owner = str(item.get("owner") or "")
                    item["block_reason"] = diagnose_queue_block(
                        job_gpus=int(item.get("gpus") or 1),
                        cluster_queue=cluster_queue,
                        workloads=workloads,
                        job_priority=str(item.get("priority") or "normal"),
                        user_usage=(self.usage_for(owner, list(live.values())).as_dict() if owner else None),
                        user_quota=(self.quota_for(owner).as_dict() if owner else None),
                    )
                except Exception:  # noqa: BLE001 - 诊断失败不能影响任务列表
                    item["block_reason"] = None
            else:
                item["block_reason"] = None

    @staticmethod
    def _workload_for_name(workloads: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
        for workload in workloads:
            metadata = workload.get("metadata", {})
            if metadata.get("labels", {}).get("vla-platform/job-name") == name:
                return workload
            for owner in metadata.get("ownerReferences", []):
                if owner.get("name") == name:
                    return workload
        return None


def _gpu_quantity(value: Any) -> int:
    if value is None:
        return 0
    text = str(value)
    try:
        quantity = Decimal(text[:-1]) / 1000 if text.endswith("m") else Decimal(text)
    except InvalidOperation:
        return 0
    return max(0, int(quantity.to_integral_value()))


def _pod_gpu_request(pod: dict[str, Any]) -> int:
    total = 0
    for container in pod.get("spec", {}).get("containers", []):
        resources = container.get("resources", {})
        value = resources.get("requests", {}).get("nvidia.com/gpu")
        if value is None:
            value = resources.get("limits", {}).get("nvidia.com/gpu", 0)
        total += _gpu_quantity(value)
    return total


def _queue_gpu_used(flavor_usage: list[dict[str, Any]]) -> int | None:
    for flavor in flavor_usage:
        for resource in flavor.get("resources", []):
            if resource.get("name") == "nvidia.com/gpu":
                return _gpu_quantity(resource.get("total", 0))
    return None


def _cluster_queue_quota(cluster_queue: dict[str, Any]) -> int:
    total = 0
    for group in cluster_queue.get("spec", {}).get("resourceGroups", []):
        covered = group.get("coveredResources", [])
        if covered and "nvidia.com/gpu" not in covered:
            continue
        for flavor in group.get("flavors", []):
            for resource in flavor.get("resources", []):
                if resource.get("name") == "nvidia.com/gpu":
                    total += _gpu_quantity(resource.get("nominalQuota", 0))
    return total


def _workload_gpus(workload: dict[str, Any]) -> int:
    total = 0
    for pod_set in workload.get("spec", {}).get("podSets", []):
        pod = pod_set.get("template", {})
        total += _pod_gpu_request(pod) * int(pod_set.get("count", 1))
    return total
