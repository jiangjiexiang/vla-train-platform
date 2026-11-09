from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from ..config import Settings
from ..history import HistoryStore, utc_now
from ..models import SubmitSpec, check_payload_size
from ..templates.pytorchjob import render_pytorch_job
from .errors import ServiceError
from .kubernetes import KubernetesClient


class PlatformService:
    def __init__(self, settings: Settings, history: HistoryStore, kubernetes: KubernetesClient):
        self.settings = settings
        self.history = history
        self.kubernetes = kubernetes

    def submit(self, spec: SubmitSpec) -> dict[str, Any]:
        if spec.queue != self.settings.queue:
            raise ServiceError(422, f"queue must be {self.settings.queue}")
        if spec.gpus not in self.settings.allowed_gpus:
            allowed = ", ".join(str(value) for value in self.settings.allowed_gpus)
            raise ServiceError(422, f"gpus must be one of: {allowed}")
        try:
            check_payload_size(spec)
        except ValueError as exc:
            raise ServiceError(422, str(exc)) from exc
        if self.kubernetes.get_job(spec.name) is not None:
            raise ServiceError(409, f"job {spec.name} already exists")

        submitted_at = utc_now()
        body = render_pytorch_job(spec, submitted_at, self.settings)
        try:
            job = self.kubernetes.create_job(body)
        except ServiceError as exc:
            if exc.status_code == 409:
                raise ServiceError(409, f"job {spec.name} already exists") from exc
            raise
        # Record history only after the Kubernetes object exists, so a rejected
        # or failed create cannot leave a phantom submission behind.
        self.history.record_submission(spec, submitted_at)
        return self._job_response(job, None, [])

    def list_jobs(self) -> dict[str, Any]:
        return self._list_jobs(self.kubernetes.list_namespace_pods())

    def _list_jobs(self, pods: list[dict[str, Any]]) -> dict[str, Any]:
        """Build every job response from one namespace-wide pod snapshot.

        ``pods`` is listed once and bucketed by the ``vla-platform/job-name``
        label, so the response for a job sees exactly the pods the per-job
        label selector would have returned.
        """
        pods_by_job = _group_pods_by_job(pods)
        jobs = self.kubernetes.list_jobs()
        workloads = self.kubernetes.list_workloads()
        live: dict[str, dict[str, Any]] = {}
        for job in jobs:
            name = job.get("metadata", {}).get("name", "")
            job_pods = pods_by_job.get(name, [])
            workload = self._matching_workload(job, workloads, job_pods)
            response = self._job_response(job, workload, job_pods)
            live[name] = response

        cancelled = {
            name: self._cancelled_response(record)
            for record in self.history.list_cancellations()
            if (name := record["name"]) not in live
        }
        items = sorted(
            [*live.values(), *cancelled.values()],
            key=lambda item: item.get("created_at") or "",
            reverse=True,
        )
        return {"items": items, "total": len(items)}

    def get_job(self, name: str) -> dict[str, Any]:
        job = self.kubernetes.get_job(name)
        if job is None:
            cancellation = self.history.get_cancellation(name)
            if cancellation is not None:
                return self._cancelled_response(cancellation)
            raise ServiceError(404, f"job {name} not found")
        pods = self.kubernetes.list_job_pods(name)
        workload = self._matching_workload(job, self.kubernetes.list_workloads(), pods)
        return self._job_response(job, workload, pods)

    def cancel(self, name: str) -> dict[str, Any]:
        job = self.kubernetes.get_job(name)
        if job is None:
            cancellation = self.history.get_cancellation(name)
            if cancellation is not None:
                return self._cancelled_response(cancellation)
            raise ServiceError(404, f"job {name} not found")

        spec = self._spec_for_job(job)
        created_at = self._created_at(job)
        # Deleting a finished job is a cleanup, not a cancellation. Keep the real
        # terminal status in history so a successful run is not rewritten as
        # CANCELLED once its Kubernetes object is removed.
        current = self._job_response(job, None, [])["status"]
        preserved = current if current in ("SUCCEEDED", "FAILED") else "CANCELLED"
        self.kubernetes.delete_job(name)
        cancellation = self.history.mark_cancelled(spec, created_at, status=preserved)
        return self._cancelled_response(cancellation)

    def clone(self, name: str, new_name: str) -> dict[str, Any]:
        job = self.kubernetes.get_job(name)
        if job is not None:
            source = self._spec_for_job(job)
        else:
            cancellation = self.history.get_cancellation(name)
            if cancellation is None:
                raise ServiceError(404, f"job {name} not found")
            source = cancellation["spec"]
        return self.submit(source.model_copy(update={"name": new_name}))

    def logs(self, name: str, tail_lines: int) -> dict[str, Any]:
        job = self.kubernetes.get_job(name)
        if job is None:
            if self.history.get_cancellation(name) is None:
                raise ServiceError(404, f"job {name} not found")
            return {"name": name, "pod_name": None, "logs": ""}
        pods = self.kubernetes.list_job_pods(name)
        pod = self._select_pod(pods)
        if pod is None:
            return {"name": name, "pod_name": None, "logs": ""}
        pod_name = pod.get("metadata", {}).get("name")
        return {
            "name": name,
            "pod_name": pod_name,
            "logs": self.kubernetes.read_pod_log(pod_name, tail_lines),
        }

    def events(self, name: str) -> dict[str, Any]:
        job = self.kubernetes.get_job(name)
        if job is None:
            if self.history.get_cancellation(name) is None:
                raise ServiceError(404, f"job {name} not found")
            return {"name": name, "items": []}
        pod_names = [
            pod.get("metadata", {}).get("name", "")
            for pod in self.kubernetes.list_job_pods(name)
        ]
        raw = self.kubernetes.list_events([name, *pod_names])
        items = [_event_item(event) for event in raw]
        items.sort(key=lambda item: item.get("last_seen") or "", reverse=True)
        return {"name": name, "items": items}

    def cluster_summary(self) -> dict[str, Any]:
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

    def gpu_metrics(self) -> dict[str, Any]:
        """Live per-GPU numbers scraped from the vla-gpu-metrics DaemonSet.

        Kubernetes only knows how many GPUs it handed out; it cannot see host
        processes that use a GPU outside the cluster, so utilisation and memory
        come from nvidia-smi through that service. Never raises: a missing or
        broken service degrades to available=false so the dashboard keeps working.
        """
        url = self.settings.gpu_metrics_url
        if not url:
            return {"available": False, "items": []}
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return {"available": False, "items": []}
        if not isinstance(payload, list):
            return {"available": False, "items": []}
        items = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            items.append(
                {
                    "index": _whole_number(entry.get("index")),
                    "name": str(entry.get("name") or "unknown"),
                    "utilization_percent": _whole_number(entry.get("utilization_percent")),
                    "memory_used_mib": _whole_number(entry.get("memory_used_mib")),
                    "memory_total_mib": _whole_number(entry.get("memory_total_mib")),
                    "temperature_celsius": _whole_number(entry.get("temperature_celsius")),
                }
            )
        items.sort(key=lambda item: item["index"])
        return {"available": True, "items": items}

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

    def _job_response(
        self,
        job: dict[str, Any],
        workload: dict[str, Any] | None,
        pods: list[dict[str, Any]],
    ) -> dict[str, Any]:
        spec = self._spec_for_job(job)
        pod = self._select_pod(pods)
        pod_phase = _dict(pod.get("status")).get("phase") if pod else None
        conditions = _list(_dict(job.get("status")).get("conditions"))
        terminal = _active_condition(conditions, ("Succeeded", "Failed"))
        running = _active_condition(conditions, ("Running",))
        if terminal:
            status = terminal["type"].upper()
        elif pod_phase == "Succeeded":
            status = "SUCCEEDED"
        elif pod_phase == "Failed":
            status = "FAILED"
        elif (running is not None) or pod_phase == "Running":
            status = "RUNNING"
        else:
            status = "QUEUED"

        job_status = _dict(job.get("status"))
        started_at = _format_time(job_status.get("startTime"))
        if started_at is None and pod:
            pod_status = _dict(pod.get("status"))
            started_at = _format_time(pod_status.get("start_time") or pod_status.get("startTime"))
        if started_at is None and running:
            started_at = _format_time(running.get("lastTransitionTime"))
        finished_at = _format_time(terminal.get("lastTransitionTime")) if terminal else None
        created_at = self._created_at(job)
        workload_status = _workload_status(workload) if workload else "Pending"

        if terminal and terminal.get("message"):
            message = terminal["message"]
        elif status == "RUNNING":
            message = "Training job is running"
        elif status == "QUEUED" and pod_phase == "Pending":
            # The pod exists but cannot start (image pull failure, unschedulable,
            # crash loop). Without this the job is indistinguishable from one
            # still waiting for quota, because both map to QUEUED.
            message = f"Admitted but pod is not ready: {_pod_waiting_reason(pod)}"
        elif workload_status == "Admitted":
            message = "Admitted by Kueue; waiting for training pod"
        elif workload and _latest_workload_message(workload):
            message = _latest_workload_message(workload)
        else:
            message = "Waiting for Kueue admission"

        return {
            "name": spec.name,
            "status": status,
            "image": spec.image,
            "gpus": spec.gpus,
            "queue": spec.queue,
            "priority": spec.priority,
            "command": spec.command,
            "env": spec.env,
            "max_retries": spec.max_retries,
            "termination_grace_period_seconds": spec.termination_grace_period_seconds,
            "created_at": created_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": _duration_seconds(started_at, finished_at, status),
            "workload_status": workload_status,
            "pod_status": pod_phase,
            "message": message,
            "checkpoint_dir": f"/checkpoints/{spec.name}",
        }

    def _spec_for_job(self, job: dict[str, Any]) -> SubmitSpec:
        metadata = job.get("metadata", {})
        name = metadata.get("name", "")
        annotation = metadata.get("annotations", {}).get("vla-platform/spec")
        if annotation:
            try:
                return SubmitSpec.model_validate(json.loads(annotation))
            except (json.JSONDecodeError, ValidationError):
                pass
        submission = self.history.latest_submission(name)
        if submission is not None:
            return submission[0]
        return self._spec_from_manifest(job)

    def _spec_from_manifest(self, job: dict[str, Any]) -> SubmitSpec:
        metadata = job.get("metadata", {})
        labels = metadata.get("labels", {})
        master = job.get("spec", {}).get("pytorchReplicaSpecs", {}).get("Master", {})
        container = master.get("template", {}).get("spec", {}).get("containers", [{}])[0]
        command = container.get("command", [])
        shell_command = command[2] if len(command) >= 3 else " ".join(command)
        environment = {
            item["name"]: item.get("value", "")
            for item in container.get("env", [])
            if item.get("name") and item.get("name") != "CHECKPOINT_DIR"
        }
        gpu = container.get("resources", {}).get("limits", {}).get("nvidia.com/gpu", 1)
        retries = job.get("spec", {}).get("backoffLimit", 0)
        grace = master.get("template", {}).get("spec", {}).get("terminationGracePeriodSeconds", 30)
        return SubmitSpec.model_validate(
            {
                "name": metadata.get("name"),
                "image": container.get("image", self.settings.default_image),
                "gpus": int(gpu),
                "queue": labels.get("kueue.x-k8s.io/queue-name", self.settings.queue),
                "priority": labels.get("kueue.x-k8s.io/priority-class", "normal"),
                "command": shell_command,
                "env": environment,
                "max_retries": retries if retries in (0, 1, 2) else 0,
                "termination_grace_period_seconds": grace if isinstance(grace, int) and 1 <= grace <= 600 else 30,
            }
        )

    def _created_at(self, job: dict[str, Any]) -> str:
        metadata = job.get("metadata", {})
        return (
            _format_time(metadata.get("annotations", {}).get("vla-platform/submitted-at"))
            or _format_time(metadata.get("creationTimestamp") or metadata.get("creation_timestamp"))
            or utc_now()
        )

    def _matching_workload(
        self,
        job: dict[str, Any],
        workloads: list[dict[str, Any]],
        pods: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        metadata = job.get("metadata", {})
        name = metadata.get("name")
        uid = metadata.get("uid")
        pod_uids = {pod.get("metadata", {}).get("uid") for pod in pods or []}
        pod_names = {pod.get("metadata", {}).get("name") for pod in pods or []}
        for workload in workloads:
            workload_metadata = workload.get("metadata", {})
            if workload_metadata.get("labels", {}).get("vla-platform/job-name") == name:
                return workload
            for owner in workload_metadata.get("ownerReferences", []):
                if owner.get("kind") == "PyTorchJob" and (
                    (uid and owner.get("uid") == uid) or owner.get("name") == name
                ):
                    return workload
                if owner.get("kind") == "Pod" and (
                    (owner.get("uid") in pod_uids and owner.get("uid") is not None)
                    or owner.get("name") in pod_names
                ):
                    return workload
            for pod_name in pod_names:
                if pod_name and workload_metadata.get("name", "").startswith(f"pod-{pod_name}-"):
                    return workload
        return None

    @staticmethod
    def _select_pod(pods: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not pods:
            return None
        return sorted(
            pods,
            key=lambda pod: (
                _format_time(
                    pod.get("metadata", {}).get("creationTimestamp")
                    or pod.get("metadata", {}).get("creation_timestamp")
                )
                or "",
                pod.get("metadata", {}).get("name", ""),
            ),
            reverse=True,
        )[0]

    @staticmethod
    def _cancelled_response(record: dict[str, Any]) -> dict[str, Any]:
        spec: SubmitSpec = record["spec"]
        created_at = record["created_at"]
        cancelled_at = record["cancelled_at"]
        status = record.get("status") or "CANCELLED"
        message = "Cancelled by user" if status == "CANCELLED" else f"Deleted after the job finished with {status}"
        return {
            "name": spec.name,
            "status": status,
            "image": spec.image,
            "gpus": spec.gpus,
            "queue": spec.queue,
            "priority": spec.priority,
            "command": spec.command,
            "env": spec.env,
            "max_retries": spec.max_retries,
            "termination_grace_period_seconds": spec.termination_grace_period_seconds,
            "created_at": created_at,
            "started_at": None,
            "finished_at": cancelled_at,
            "duration_seconds": None,
            "workload_status": "Cancelled" if status == "CANCELLED" else "Finished",
            "pod_status": None,
            "message": message,
            "checkpoint_dir": f"/checkpoints/{spec.name}",
        }


def _whole_number(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _pod_waiting_reason(pod: dict[str, Any] | None) -> str:
    if not pod:
        return "pod has not been created"
    status = _dict(pod.get("status"))
    for container_status in _list(status.get("container_statuses")) + _list(status.get("containerStatuses")):
        waiting = (container_status.get("state") or {}).get("waiting")
        if waiting and waiting.get("reason"):
            detail = f": {waiting['message']}" if waiting.get("message") else ""
            return f"{waiting['reason']}{detail}"
    for condition in _list(status.get("conditions")):
        if condition.get("type") == "PodScheduled" and not _condition_true(condition):
            detail = condition.get("message") or condition.get("reason") or "unschedulable"
            return f"NotScheduled: {detail}"
    return "waiting for container start"


def _event_item(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata", {})
    involved = event.get("involved_object") or event.get("involvedObject") or {}
    return {
        "type": event.get("type"),
        "reason": event.get("reason"),
        "message": event.get("message"),
        "count": event.get("count") or 1,
        "object_kind": involved.get("kind"),
        "object_name": involved.get("name"),
        "first_seen": _format_time(event.get("first_timestamp") or event.get("firstTimestamp")),
        "last_seen": _format_time(
            event.get("last_timestamp")
            or event.get("lastTimestamp")
            or metadata.get("creation_timestamp")
            or metadata.get("creationTimestamp")
        ),
    }


def _dict(value: Any) -> dict[str, Any]:
    """Kubernetes objects can carry an explicit null where a mapping is expected."""
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    """Repeated fields behave the same: .get(key, []) yields None when the key is null."""
    return value if isinstance(value, list) else []


def _group_pods_by_job(pods: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket a namespace-wide pod snapshot by the vla-platform/job-name label."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for pod in pods:
        job_name = _dict(_dict(pod.get("metadata")).get("labels")).get("vla-platform/job-name")
        if job_name:
            grouped.setdefault(job_name, []).append(pod)
    return grouped


def _active_condition(conditions: list[dict[str, Any]], types: tuple[str, ...]) -> dict[str, Any] | None:
    matches = [
        condition
        for condition in conditions
        if condition.get("type") in types and _condition_true(condition)
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: _format_time(item.get("lastTransitionTime")) or "")[-1]


def _condition_true(condition: dict[str, Any]) -> bool:
    return condition.get("status") is True or str(condition.get("status")).lower() == "true"


def _workload_status(workload: dict[str, Any]) -> str:
    conditions = _list(_dict(workload.get("status")).get("conditions"))
    for condition_type in ("Finished", "Admitted", "QuotaReserved", "PodsReady"):
        condition = _active_condition(conditions, (condition_type,))
        if condition is not None:
            return condition_type
    return "Pending"


def _latest_workload_message(workload: dict[str, Any]) -> str | None:
    conditions = _list(_dict(workload.get("status")).get("conditions"))
    messages = [condition.get("message") for condition in conditions if condition.get("message")]
    return messages[-1] if messages else None


def _format_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _duration_seconds(started_at: str | None, finished_at: str | None, status: str) -> int | None:
    if started_at is None or status == "QUEUED":
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        finished = (
            datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            if finished_at
            else datetime.now(timezone.utc)
        )
    except ValueError:
        return None
    return max(0, int((finished - started).total_seconds()))


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
