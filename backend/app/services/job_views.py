from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from ..history import utc_now
from ..models import SubmitSpec
from .errors import ServiceError

logger = logging.getLogger(__name__)


class JobViewServiceMixin:
    def list_jobs(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        status: str | None = None,
        q: str | None = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        """`owner` 为用户名时只保留该用户的任务；None 表示不过滤（admin）。"""
        return self._list_jobs(
            self.kubernetes.list_namespace_pods(),
            limit=limit,
            offset=offset,
            status=status,
            q=q,
            owner=owner if isinstance(owner, str) else None,
        )

    def _list_jobs(
        self,
        pods: list[dict[str, Any]],
        *,
        limit: int | None = None,
        offset: int = 0,
        status: str | None = None,
        q: str | None = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        """用一次命名空间 Pod 快照构建所有任务响应。

        先合并实时任务与取消记录，再过滤、排序和分页；total 是分页前数量。
        """
        pods_by_job = _group_pods_by_job(pods)
        jobs = self.kubernetes.list_jobs()
        workloads = self.kubernetes.list_workloads()
        live: dict[str, dict[str, Any]] = {}
        # 老任务没有 owner 注解，按用户过滤时从提交历史回填。
        submitters = self.history.owners_by_name() if owner is not None else {}
        for job in jobs:
            name = job.get("metadata", {}).get("name", "")
            job_pods = pods_by_job.get(name, [])
            workload = self._matching_workload(job, workloads, job_pods)
            response = self._job_response(job, workload, job_pods, submitters.get(name))
            live[name] = response

        # 排队位置需要同一队列的全局 Workload 快照，收集完任务后统一计算。
        cluster_queue: dict[str, Any] | None = None
        try:
            cluster_queue = self.kubernetes.get_cluster_queue(self.settings.cluster_queue)
        except Exception:  # noqa: BLE001
            cluster_queue = None
        self._annotate_queue(live, workloads, cluster_queue)

        cancelled = {
            name: self._cancelled_response(record)
            for record in self.history.list_cancellations(owner)
            if (name := record["name"]) not in live
        }
        items = sorted(
            [*live.values(), *cancelled.values()],
            key=lambda item: item.get("created_at") or "",
            reverse=True,
        )
        if owner is not None:
            items = [item for item in items if item.get("owner") == owner]
        if status is not None:
            items = [item for item in items if item.get("status") == status]
        if q:
            needle = q.casefold()
            items = [item for item in items if needle in str(item.get("name", "")).casefold()]
        total = len(items)
        effective_offset = offset if limit is not None else 0
        if limit is not None:
            items = items[effective_offset : effective_offset + limit]
        return {"items": items, "total": total, "limit": limit, "offset": effective_offset}

    def _job_existed(self, name: str) -> bool:
        """K8s 对象、取消记录或提交记录任一存在，即认为任务曾经存在。"""
        if self.kubernetes.get_job(name) is not None:
            return True
        if self.history.get_cancellation(name) is not None:
            return True
        return self.history.find_submission(name) is not None

    def _require_job_existed(self, name: str) -> None:
        if not self._job_existed(name):
            raise ServiceError(404, f"job {name} not found")

    def get_job(self, name: str) -> dict[str, Any]:
        job = self.kubernetes.get_job(name)
        if job is None:
            cancellation = self.history.get_cancellation(name)
            if cancellation is not None:
                return self._cancelled_response(cancellation)
            # 对象被 TTL 回收或手工删除后，仍返回当初提交的内容。
            submission = self.history.find_submission(name)
            if submission is not None:
                return self._vanished_response(name, *submission)
            raise ServiceError(404, f"job {name} not found")
        pods = self.kubernetes.list_job_pods(name)
        workload = self._matching_workload(job, self.kubernetes.list_workloads(), pods)
        response = self._job_response(job, workload, pods)
        # 趁对象仍在时记录真实终态，供 TTL 回收后还原。
        self._remember_terminal(name, response)
        return response

    def _remember_terminal(self, name: str, response: dict[str, Any]) -> None:
        """终态落库，供对象被回收后还原。失败不影响主流程。"""
        status = response.get("status")
        if status not in ("SUCCEEDED", "FAILED"):
            return
        try:
            spec = self.history.latest_submission(name)
            if spec is None:
                return
            self.history.mark_cancelled(
                spec[0],
                response.get("created_at") or spec[1],
                response.get("finished_at") or utc_now(),
                status=status,
            )
        except Exception:  # noqa: BLE001 - 缓存写失败不该让读接口挂掉
            logger.debug("无法记录任务终态: %s", name, exc_info=True)

    def _vanished_response(self, name: str, spec: Any, submitted_at: str) -> dict[str, Any]:
        """对象消失后优先返回已记录的真实终态，否则明确返回 GONE。"""
        recorded = self.history.get_cancellation(name)
        recorded_status = (recorded or {}).get("status")
        if recorded_status in ("SUCCEEDED", "FAILED"):
            return {
                "name": name,
                "status": recorded_status,
                "image": spec.image,
                "gpus": spec.gpus,
                "queue": spec.queue,
                "priority": spec.priority,
                "command": spec.command,
                "env": dict(spec.env),
                "max_retries": spec.max_retries,
                "owner": None,
                "created_at": (recorded or {}).get("created_at") or submitted_at,
                "finished_at": (recorded or {}).get("cancelled_at"),
                "message": (
                    "任务已完成，训练对象已被集群回收；"
                    "下面是平台记录下的最终状态"
                ),
            }
        return {
            "name": name,
            "status": "GONE",
            "image": spec.image,
            "gpus": spec.gpus,
            "queue": spec.queue,
            "priority": spec.priority,
            "command": spec.command,
            "env": dict(spec.env),
            "max_retries": spec.max_retries,
            "owner": None,
            "created_at": submitted_at,
            "message": (
                "任务对象已从集群中清理（通常是完成后自动回收，或被手工删除），"
                "平台保留了你当初的提交内容"
            ),
        }

    def cancel(self, name: str) -> dict[str, Any]:
        job = self.kubernetes.get_job(name)
        if job is None:
            cancellation = self.history.get_cancellation(name)
            if cancellation is not None:
                return self._cancelled_response(cancellation)
            raise ServiceError(404, f"job {name} not found")

        spec = self._spec_for_job(job)
        created_at = self._created_at(job)
        # 删除已完成对象是清理而非取消，历史中保留真实终态。
        current = self._job_response(job, None, [], spec.owner)["status"]
        preserved = current if current in ("SUCCEEDED", "FAILED") else "CANCELLED"
        try:
            self.kubernetes.delete_job(name)
        except ServiceError as exc:
            # 并发取消时另一个请求可能已删除对象，幂等返回已有记录。
            if exc.status_code != 404:
                raise
            existing = self.history.get_cancellation(name)
            if existing is not None:
                return self._cancelled_response(existing)
            raise
        cancellation = self.history.mark_cancelled(spec, created_at, status=preserved)
        self.audit.record(
            "job.cancel",
            actor=self._current_actor,
            target=name,
            detail={"previous_status": current, "recorded_as": preserved},
        )
        return self._cancelled_response(cancellation)

    def describe_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """把单个 PyTorchJob 整理成与列表/详情一致的响应。

        Kueue 尚未创建 Workload 是常态；Pod 或 Workload 查询失败也要给出状态。
        """
        name = str(job.get("metadata", {}).get("name") or "")
        try:
            pods = self.kubernetes.list_job_pods(name) if name else []
        except Exception:  # noqa: BLE001 - 拿不到 Pod 也要能给出状态
            pods = []
        try:
            workload = self._matching_workload(job, self.kubernetes.list_workloads(), pods)
        except Exception:  # noqa: BLE001 - 没有 Workload 是常态，不是错误
            workload = None
        return self._job_response(job, workload, pods)

    def _job_response(
        self,
        job: dict[str, Any],
        workload: dict[str, Any] | None,
        pods: list[dict[str, Any]],
        fallback_owner: str | None = None,
    ) -> dict[str, Any]:
        spec = self._spec_for_job(job)
        if not spec.owner and fallback_owner:
            spec = spec.model_copy(update={"owner": fallback_owner})
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
            # 永久启动失败不能继续显示为排队，否则用户会一直等待。
            waiting_reason = _pod_waiting_reason(pod)
            if _is_permanent_start_failure(waiting_reason):
                status = "STARTING_FAILED"
                message = _start_failure_message(waiting_reason, spec.image)
            else:
                message = f"Admitted but pod is not ready: {waiting_reason}"
        elif workload_status == "Admitted":
            message = "Admitted by Kueue; waiting for training pod"
        elif workload and _latest_workload_message(workload):
            message = _latest_workload_message(workload)
        else:
            message = "Waiting for Kueue admission"

        return {
            **_spec_response_fields(spec, status),
            "created_at": created_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": _duration_seconds(started_at, finished_at, status),
            "workload_status": workload_status,
            "pod_status": pod_phase,
            "message": message,
            "checkpoint_dir": _checkpoint_dir(self.settings.checkpoints_mount_path, spec.name),
        }

    def _spec_for_job(self, job: dict[str, Any]) -> SubmitSpec:
        return self._spec_from_job(job, SubmitSpec.model_validate)

    def _spec_from_job(self, job: dict[str, Any], parse) -> SubmitSpec:
        """按注解、历史、清单的顺序还原 spec；parse 决定校验宽容度。"""
        metadata = job.get("metadata", {})
        name = metadata.get("name", "")
        annotation = metadata.get("annotations", {}).get("vla-platform/spec")
        if annotation:
            try:
                return parse(json.loads(annotation))
            except (json.JSONDecodeError, ValidationError, ValueError):
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

    def _parse_strict(self, payload: dict[str, Any]) -> SubmitSpec:
        # 克隆会原样重新提交，因此保留 extra="forbid" 的严格语义。
        try:
            return SubmitSpec.model_validate(payload)
        except (ValidationError, ValueError) as exc:
            raise ServiceError(422, f"job spec cannot be cloned: {exc}") from exc

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

    def _cancelled_response(self, record: dict[str, Any]) -> dict[str, Any]:
        spec: SubmitSpec = record["spec"]
        created_at = record["created_at"]
        cancelled_at = record["cancelled_at"]
        status = record.get("status") or "CANCELLED"
        # 这张表也保存对象回收前的真实终态，提示语不能一律写成用户取消。
        if status == "CANCELLED":
            message = "Cancelled by user"
        elif status == "SUCCEEDED":
            message = "任务已成功完成（训练对象已被集群回收）"
        elif status == "FAILED":
            message = "任务已失败（训练对象已被集群回收）"
        else:
            message = f"Deleted after the job finished with {status}"
        return {
            **_spec_response_fields(spec, status),
            "created_at": created_at,
            "started_at": None,
            "finished_at": cancelled_at,
            "duration_seconds": None,
            "workload_status": "Cancelled" if status == "CANCELLED" else "Finished",
            "pod_status": None,
            "message": message,
            "checkpoint_dir": _checkpoint_dir(self.settings.checkpoints_mount_path, spec.name),
        }


def _spec_response_fields(spec: SubmitSpec, status: str) -> dict[str, Any]:
    """实时与历史任务共有的响应字段。"""
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
        "owner": spec.owner,
        "labels": spec.labels,
        "gpu_type": "nvidia.com/gpu",
        "nodes": spec.nodes,
        # 单机也回填实际每节点 GPU 数，避免前端处理无意义的 null。
        "gpus_per_node": spec.gpus_per_node
        if spec.gpus_per_node is not None
        else (spec.gpus // spec.nodes if spec.nodes else None),
    }


def _checkpoint_dir(root: str, name: str) -> str:
    return f"{root.rstrip('/')}/{name}"


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


_PERMANENT_START_FAILURES = (
    "ImagePullBackOff",
    "ErrImagePull",
    "InvalidImageName",
    "CreateContainerConfigError",
    "CreateContainerError",
    "RunContainerError",
)


def _is_permanent_start_failure(reason: str) -> bool:
    """区分不会自行恢复的启动失败与值得继续等待的排队状态。"""
    return any(code in reason for code in _PERMANENT_START_FAILURES)


def _start_failure_message(reason: str, image: str) -> str:
    """把 K8s 的 waiting reason 翻译成一句能照着做事的话。"""
    if "ImagePullBackOff" in reason or "ErrImagePull" in reason:
        return (
            f"镜像拉取失败：{image} 在训练节点上不存在。"
            "buildah 里有的镜像 K3s 看不到，需要导出后导入："
            f"buildah push localhost/{image} "
            f"docker-archive:/tmp/x.tar:docker.io/library/{image} "
            "&& k3s ctr -n k8s.io images import /tmp/x.tar"
        )
    if "InvalidImageName" in reason:
        return f"镜像名不合法：{image}"
    if "CreateContainerConfigError" in reason or "CreateContainerError" in reason:
        return f"容器配置有误，创建失败：{reason}"
    return f"训练 Pod 启动失败：{reason}"


def _pod_waiting_reason(pod: dict[str, Any] | None) -> str:
    if not pod:
        return "pod has not been created"
    status = _dict(pod.get("status"))
    # initContainer 的镜像拉取失败同样属于启动失败，不能误判为排队。
    all_statuses = (
        _list(status.get("init_container_statuses"))
        + _list(status.get("initContainerStatuses"))
        + _list(status.get("container_statuses"))
        + _list(status.get("containerStatuses"))
    )
    for container_status in all_statuses:
        waiting = (container_status.get("state") or {}).get("waiting")
        if waiting and waiting.get("reason"):
            detail = f": {waiting['message']}" if waiting.get("message") else ""
            return f"{waiting['reason']}{detail}"
    for condition in _list(status.get("conditions")):
        if condition.get("type") == "PodScheduled" and not _condition_true(condition):
            detail = condition.get("message") or condition.get("reason") or "unschedulable"
            return f"NotScheduled: {detail}"
    return "waiting for container start"
