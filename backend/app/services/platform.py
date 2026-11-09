from __future__ import annotations

import copy
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..config import ROLE_ADMIN, ROLE_USER, Settings
from ..datasets.store import DatasetStore
from ..history import HistoryStore, utc_now
from ..models import SubmitSpec, check_labels, check_payload_size
from ..params_templates import (
    BUILTIN_TEMPLATE,
    MAX_TEMPLATE_BYTES,
    parse_template,
    validate_image_name,
)
from ..recipes.loader import Recipe, RecipeStore
from ..templates.pytorchjob import render_pytorch_job
from .errors import ServiceError
from .kubernetes import KubernetesClient
from .quotas import ACTIVE_STATUSES, Quota, QuotaStore, Usage, check_quota, effective_quota
from .sessions import SessionStore

if TYPE_CHECKING:  # 仅类型标注使用，运行时延迟导入以避免循环依赖
    from ..params_templates import ParamsTemplate
    from .runs import RunRequest

class PlatformService:
    def __init__(
        self,
        settings: Settings,
        history: HistoryStore,
        kubernetes: KubernetesClient,
        sessions: SessionStore | None = None,
        quotas: QuotaStore | None = None,
    ):
        self.settings = settings
        self.history = history
        self.kubernetes = kubernetes
        # 会话与配额表和历史库共用一个 SQLite 文件（契约 1.3 / 1.5）。
        # 只有 ldap 模式签发会话令牌；其它模式下拒绝一切会话令牌（契约 1.3）。
        self.sessions = sessions or SessionStore(settings.db_path, enabled=settings.auth_mode == "ldap")
        self.quotas = quotas or QuotaStore(settings.db_path)
        self._metrics_cache: tuple[float, dict[str, Any]] | None = None
        self._metrics_lock = threading.Lock()
        self._submit_lock = threading.Lock()

    # --- 提交 --------------------------------------------------------------
    def submit(self, spec: SubmitSpec, owner: str | None = None) -> dict[str, Any]:
        """提交任务。`owner` 为空时使用 spec.owner（供老测试与 CLI 直接调用）。"""
        if owner is not None:
            spec = spec.model_copy(update={"owner": owner})
        if spec.queue != self.settings.queue:
            raise ServiceError(422, f"queue must be {self.settings.queue}")
        # 多机：gpus 是总卡数，每节点卡数单独校验（契约 1.7）。
        if spec.nodes > self.settings.max_nodes:
            raise ServiceError(422, "multi-node training is not enabled")
        per_node = spec.gpus_per_node if spec.gpus_per_node is not None else spec.gpus // spec.nodes
        allowed = self.settings.allowed_gpus
        if per_node not in allowed or spec.gpus != per_node * spec.nodes:
            joined = ", ".join(str(value) for value in allowed)
            raise ServiceError(422, f"gpus must be one of: {joined}")
        try:
            check_payload_size(spec)
            check_labels(spec.labels)
        except ValueError as exc:
            raise ServiceError(422, str(exc)) from exc
        if self.kubernetes.get_job(spec.name) is not None:
            raise ServiceError(409, f"job {spec.name} already exists")

        # 配额校验与创建之间必须原子，否则同一用户并发提交能绕过上限。
        with self._submit_lock:
            check_quota(self.quota_for(spec.owner), self.usage_for(spec.owner), spec.gpus)
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

    # --- 配额 --------------------------------------------------------------
    def quota_for(self, username: str) -> Quota:
        return effective_quota(self.settings, self.quotas.get_override(username))

    def usage_for(self, username: str, job_items: list[dict[str, Any]] | None = None) -> Usage:
        """该用户非终态任务（QUEUED + RUNNING）的数量与 GPU 之和。

        口径：K3s 实时任务 ∪ 历史中未取消的记录。历史里只有终态记录，
        与实时任务天然不重复；取消或删除后记录变终态，额度立即释放。
        """
        if not username:
            return Usage()
        if job_items is None:
            # 用整个命名空间的 pod 快照一次算完，避免每个任务查一次 Pod。
            job_items = self._list_jobs(self.kubernetes.list_namespace_pods())["items"]
        active = [
            item for item in job_items
            if item.get("owner") == username and item.get("status") in ACTIVE_STATUSES
        ]
        return Usage(active_jobs=len(active), gpu_in_use=sum(int(item.get("gpus") or 0) for item in active))

    def user_view(self, username: str, role: str, source: str, last_seen_at: Any = None) -> dict[str, Any]:
        usage = self.usage_for(username)
        return {
            "username": username,
            "role": role,
            "source": source,
            "last_seen_at": last_seen_at,
            "active_jobs": usage.active_jobs,
            "gpu_in_use": usage.gpu_in_use,
            # GET /users 每行的 quota 只放上限（与契约 1.4 一致）。
            "quota": self.quota_for(username).as_dict(),
        }

    def set_quota(self, username: str, max_gpus: int | None, max_active_jobs: int | None) -> dict[str, Any]:
        """写入单用户配额并返回最新上限 + 实时用量（前端行内编辑直接回填）。"""
        self.quotas.set_override(username, max_gpus, max_active_jobs)
        usage = self.usage_for(username)
        return {"username": username, "quota": self.quota_for(username).render(usage)}

    def list_users(self) -> dict[str, Any]:
        """已知用户 = 活跃会话里的 LDAP 用户 ∪ 有配额覆盖的账号 ∪ 有任务的提交者。

        平台没有独立用户库：token / off 模式天然只有一个内置身份，
        ldap 模式的用户从会话与历史里发现，避免额外维护一张用户表。
        """
        names: dict[str, dict[str, Any]] = dict(self.sessions.known_users())
        if self.settings.auth_mode == "token":
            names.setdefault(
                "token", {"username": "token", "role": ROLE_ADMIN, "source": "token", "last_seen_at": None}
            )
        if self.settings.auth_mode == "off":
            names.setdefault(
                "anonymous",
                {"username": "anonymous", "role": ROLE_ADMIN, "source": "off", "last_seen_at": None},
            )
        overrides = self.quotas.list_overrides()
        for username, record in overrides.items():
            # 只有覆盖配置、从未登录过的账号：角色未知，按最小权限展示为 user。
            names.setdefault(
                username,
                {"username": username, "role": ROLE_USER, "source": "ldap", "last_seen_at": record.get("updated_at")},
            )
        job_items = self._list_jobs(self.kubernetes.list_namespace_pods())["items"]
        for item in job_items:
            username = item.get("owner") or ""
            if username and username not in names:
                names[username] = {"username": username, "role": ROLE_USER, "source": "ldap", "last_seen_at": None}
        items = [
            self.user_view(
                info["username"],
                info.get("role") or ROLE_USER,
                info.get("source") or "ldap",
                _format_time_text(info.get("last_seen_at")),
            )
            for info in sorted(names.values(), key=lambda item: item["username"])
        ]
        return {"items": items}

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
        """Build every job response from one namespace-wide pod snapshot.

        ``pods`` is listed once and bucketed by the ``vla-platform/job-name``
        label, so the response for a job sees exactly the pods the per-job
        label selector would have returned.

        Filtering runs on the merged live + cancelled list, then pagination
        slices the sorted result. ``total`` is counted before that slice.
        """
        pods_by_job = _group_pods_by_job(pods)
        jobs = self.kubernetes.list_jobs()
        workloads = self.kubernetes.list_workloads()
        live: dict[str, dict[str, Any]] = {}
        # 普通用户只看自己的任务：实时任务的 owner 来自注解，老任务没有，
        # 因此在只过滤一种角色时才需要回填提交历史里的 owner。
        submitters = self.history.owners_by_name() if owner is not None else {}
        for job in jobs:
            name = job.get("metadata", {}).get("name", "")
            job_pods = pods_by_job.get(name, [])
            workload = self._matching_workload(job, workloads, job_pods)
            response = self._job_response(job, workload, job_pods, submitters.get(name))
            live[name] = response

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

    # ------------------------------------------------------------------
    # 训练配方与一键训练
    # ------------------------------------------------------------------
    def _recipe_store(self) -> RecipeStore:
        store = getattr(self, "_recipes", None)
        if store is None:
            store = RecipeStore()
            self._recipes = store
        return store

    def list_recipes(self) -> list[Recipe]:
        return self._recipe_store().load_all()

    def get_recipe(self, recipe_id: str) -> Recipe:
        recipe = self._recipe_store().get(recipe_id)
        if recipe is None:
            raise ServiceError(404, f"recipe {recipe_id} not found")
        return recipe

    def recipe_template(self, recipe: Recipe) -> ParamsTemplate:
        from .runs import resolve_template  # 延迟导入，避免循环依赖

        return resolve_template(recipe, self)

    def create_run(self, request: RunRequest, owner: str) -> dict[str, Any]:
        """一键训练：展开配方与数据集，然后走既有 submit 路径。"""
        from .runs import build_run  # 延迟导入，避免循环依赖

        recipe = self.get_recipe(request.recipe_id)
        dataset = self.get_dataset(request.dataset_id)
        existing = {item["name"] for item in self._list_jobs(self.kubernetes.list_namespace_pods())["items"]}
        payload = build_run(
            request,
            recipe=recipe,
            dataset=dataset,
            service=self,
            owner=owner,
            existing_names=existing,
        )
        spec = SubmitSpec.model_validate(payload)
        return self.submit(spec, owner=owner)

    # ------------------------------------------------------------------
    # 数据集
    # ------------------------------------------------------------------
    def _dataset_store(self) -> DatasetStore:
        """惰性创建，避免每次启动都建表（表本身是幂等的）。"""
        store = getattr(self, "_datasets", None)
        if store is None:
            store = DatasetStore(self.settings.db_path)
            self._datasets = store
        return store

    def _datasets_root(self) -> Path:
        return Path(self.settings.datasets_mount_path)

    def list_datasets(self, refresh: bool = False) -> dict[str, Any]:
        """列出数据集。缓存为准，refresh 时先重扫有变化的目录。"""
        store = self._dataset_store()
        root = self._datasets_root()
        if refresh:
            pending = store.needs_rescan(root)
            if pending:
                store.scan_many(root, pending)
        items = [meta.as_dict() for meta in store.list_all()]
        summary = {
            "total": len(items),
            "ready": sum(1 for item in items if item["status"] == "ready"),
            "warning": sum(1 for item in items if item["status"] == "warning"),
            "unusable": sum(1 for item in items if item["status"] in ("unusable", "error")),
            "total_frames": sum(int(item["frames"]) for item in items),
        }
        return {"items": items, "summary": summary, "root": str(root)}

    def get_dataset(self, dataset_id: str, deep: bool = False) -> dict[str, Any]:
        store = self._dataset_store()
        root = self._datasets_root()
        meta = store.get(dataset_id)
        # 缓存没有、或要求深扫描而现有记录还是浅扫描时，补扫一次
        if meta is None or (deep and meta.scan_depth != "deep"):
            try:
                meta = store.scan_one(root, dataset_id, deep=deep)
            except FileNotFoundError as exc:
                raise ServiceError(404, f"dataset {dataset_id} not found") from exc
        return meta.as_dict()

    def scan_datasets(self, dataset_id: str | None = None, deep: bool = False) -> dict[str, Any]:
        store = self._dataset_store()
        root = self._datasets_root()
        targets = [dataset_id] if dataset_id else None
        if targets is not None and not (root / dataset_id).is_dir():  # type: ignore[operator]
            raise ServiceError(404, f"dataset {dataset_id} not found")
        results = store.scan_many(root, targets, deep=deep)
        return {
            "scanned": len(results),
            "items": [meta.as_dict() for meta in results],
        }

    def get_params_template(self, image: str) -> dict[str, Any]:
        """返回镜像的参数模板；未登记时回退内置通用模板。"""
        if not validate_image_name(image):
            raise ServiceError(404, f"image {image} not found")
        record = self.history.get_image_params(image)
        if record is None:
            return {
                "image": image,
                "source": "builtin",
                "template": copy.deepcopy(BUILTIN_TEMPLATE),
            }
        return {"image": image, "source": "file", "template": record["template"]}

    def put_params_template(self, image: str, payload: Any) -> dict[str, Any]:
        if not validate_image_name(image):
            raise ServiceError(404, f"image {image} not found")
        try:
            template = parse_template(payload)
        except ValueError as exc:
            raise ServiceError(422, str(exc)) from exc
        serialized = template.model_dump_json(exclude_none=True)
        if len(serialized.encode("utf-8")) > MAX_TEMPLATE_BYTES:
            raise ServiceError(422, f"template must not exceed {MAX_TEMPLATE_BYTES} bytes")
        self.history.put_image_params(image, serialized, utc_now())
        return {"image": image, "source": "file", "template": json.loads(serialized)}

    def delete_params_template(self, image: str) -> None:
        if not validate_image_name(image):
            raise ServiceError(404, f"image {image} not found")
        self.history.delete_image_params(image)

    def list_images(self) -> list[dict[str, Any]]:
        """Image suggestions for the submit form.

        The web pod cannot query the node's container runtime, and the
        Kubernetes API does not expose local images, so suggestions come from
        the configured default plus every image that a live or historical job
        already used. An image in that set has provably been pulled onto the
        node at least once, which is exactly what the form needs.
        """
        seen: dict[str, dict[str, Any]] = {}

        def add(image: str, source: str) -> None:
            image = (image or "").strip()
            if not image:
                return
            entry = seen.setdefault(image, {"image": image, "sources": []})
            if source not in entry["sources"]:
                entry["sources"].append(source)

        add(self.settings.default_image, "default")
        for job in self.kubernetes.list_jobs():
            add(_spec_image(job), "running")
        for record in self.history.list_cancellations():
            add(record["spec"].image, "history")
        for spec, _submitted_at in self.history.iter_submissions():
            add(spec.image, "history")

        return sorted(
            seen.values(),
            key=lambda item: (item["image"] != self.settings.default_image, item["image"]),
        )

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
        current = self._job_response(job, None, [], spec.owner)["status"]
        preserved = current if current in ("SUCCEEDED", "FAILED") else "CANCELLED"
        try:
            self.kubernetes.delete_job(name)
        except ServiceError as exc:
            # 并发取消时，另一个请求可能已经删掉了对象；此时应幂等返回已有记录，
            # 而不是把 404 抛给调用方。
            if exc.status_code != 404:
                raise
            existing = self.history.get_cancellation(name)
            if existing is not None:
                return self._cancelled_response(existing)
            raise
        cancellation = self.history.mark_cancelled(spec, created_at, status=preserved)
        return self._cancelled_response(cancellation)

    def clone(self, name: str, new_name: str, owner: str | None = None) -> dict[str, Any]:
        """克隆任务。`owner` 为克隆者：新任务归属他，而不是原任务的主人。"""
        job = self.kubernetes.get_job(name)
        if job is not None:
            source = self._spec_from_job(job, self._parse_strict)
        else:
            cancellation = self.history.get_cancellation(name)
            if cancellation is None:
                raise ServiceError(404, f"job {name} not found")
            source = cancellation["spec"]
        new_owner = owner if owner is not None else source.owner
        return self.submit(source.model_copy(update={"name": new_name, "owner": new_owner}))

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

        The service is a single-connection process, so concurrent callers would
        queue behind it and every cache expiry would produce a thundering herd.
        A short TTL plus a lock keeps one fetch per window, and failures are
        cached too so an unreachable service cannot stall every request.
        """
        url = self.settings.gpu_metrics_url
        if not url:
            return {"available": False, "items": []}
        ttl = self.settings.gpu_metrics_ttl
        cached = self._fresh_metrics(ttl)
        if cached is not None:
            return cached
        with self._metrics_lock:
            cached = self._fresh_metrics(ttl)
            if cached is not None:
                return cached
            result = self._fetch_gpu_metrics(url)
            if ttl > 0:
                self._metrics_cache = (time.monotonic(), result)
            return result

    def _fresh_metrics(self, ttl: float) -> dict[str, Any] | None:
        if ttl <= 0 or self._metrics_cache is None:
            return None
        cached_at, cached = self._metrics_cache
        return cached if time.monotonic() - cached_at < ttl else None

    @staticmethod
    def _fetch_gpu_metrics(url: str) -> dict[str, Any]:
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
            # v0.9 新增字段；owner 为空串 = v0.8 老任务（没有提交者信息）。
            "owner": spec.owner,
            "labels": spec.labels,
            "gpu_type": "nvidia.com/gpu",
            "nodes": spec.nodes,
            "gpus_per_node": spec.gpus_per_node,
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
        return self._spec_from_job(job, SubmitSpec.model_validate)

    def _spec_from_job(self, job: dict[str, Any], parse) -> SubmitSpec:
        """按 注解 → 历史 → 清单 的顺序还原 spec。

        `parse` 由调用方决定宽容度：读取路径允许丢弃老记录里不认识的字段，
        而克隆会重新提交，因此必须用严格校验，不能让非法 spec 混进来。
        """
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
        # 克隆会原样重新提交，因此这里保留 extra="forbid" 的严格语义。
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
            # v0.9 新增字段；owner 为空串 = v0.8 老任务（没有提交者信息）。
            "owner": spec.owner,
            "labels": spec.labels,
            "gpu_type": "nvidia.com/gpu",
            "nodes": spec.nodes,
            "gpus_per_node": spec.gpus_per_node,
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


def _format_time_text(value: Any) -> str | None:
    """把任意时间表示规整成 Z 结尾的 ISO 字符串；解析不了的原样返回。"""
    return _format_time(value)

def _spec_image(job: dict[str, Any]) -> str:
    """Best-effort image lookup on a live PyTorchJob object."""
    try:
        raw = job.get("metadata", {}).get("annotations", {}).get("vla-platform/spec")
        if raw:
            return json.loads(raw).get("image", "")
    except (ValueError, TypeError, AttributeError):
        pass
    try:
        containers = job["spec"]["pytorchReplicaSpecs"]["Master"]["template"]["spec"]["containers"]
        return containers[0].get("image", "")
    except (KeyError, IndexError, TypeError):
        return ""
