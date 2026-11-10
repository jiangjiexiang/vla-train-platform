from __future__ import annotations

import copy
import json
import re
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
from ..datasets.scanner import DatasetMeta
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
from .audit import AuditStore
from .checkpoint_retention import UnsafePath, safe_job_dir
from .dataset_register import UnsafeDatasetPath, check_lerobot_format, resolve_dataset_dir
from .errors import ServiceError
from .kubernetes import KubernetesClient
from .queue_block import diagnose_queue_block
from .queue_est import estimate_durations, queue_info
from .quotas import ACTIVE_STATUSES, Quota, QuotaStore, Usage, check_quota, effective_quota
from .sessions import SessionStore

if TYPE_CHECKING:  # 仅类型标注使用，运行时延迟导入以避免循环依赖
    from ..params_templates import ParamsTemplate
    from .runs import RunRequest


def _dir_usage(path: Path) -> tuple[int, int]:
    """返回 (字节数, 文件数)。单个文件统计失败就跳过，不因一块坏数据整页报错。"""
    total = 0
    files = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
                files += 1
        except OSError:
            continue
    return total, files


def _parse_training_args(command: str) -> dict[str, int] | None:
    """从任务命令里抠出 steps 和 batch_size。

    命令是平台自己拼的，格式稳定，所以正则足够；找不到就返回 None，
    让调用方跳过这条样本而不是拿 0 去平均。
    """
    import re as _re

    if not command:
        return None
    result: dict[str, int] = {}
    for key in ("steps", "batch_size"):
        match = _re.search(rf"--{key}[= ](\d+)", command)
        if match:
            result[key] = int(match.group(1))
    return result or None

class PlatformService:
    @property
    def current_actor(self) -> str:
        return self._current_actor

    @current_actor.setter
    def current_actor(self, value: str) -> None:
        self._current_actor = value or "anonymous"

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
        # 审计日志：只记变更类操作，写失败绝不阻塞业务
        self.audit = AuditStore(settings.db_path)
        # 当前操作者，由 API 层在每个请求开始时设置（审计用）
        self._current_actor = "anonymous"
        # 集群摘要缓存：预检与总览页都会读它，频繁点击时不该每次都打 K8s API。
        # TTL 很短（2 秒），用户看到的数字仍然接近实时。
        self._cluster_cache: tuple[float, dict[str, Any]] | None = None
        self._cluster_lock = threading.Lock()
        # 一键训练的名字生成必须串行，避免并发下撞名
        self._run_create_lock = threading.Lock()
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
        # 审计记「谁触发的」，与任务归属 owner 是两个概念：
        # 免登录模式下 owner 恒为 anonymous，但审计要能追溯到来源。
        self.audit.record(
            "job.submit",
            actor=self._current_actor,
            target=spec.name,
            detail={"image": spec.image, "gpus": spec.gpus, "priority": spec.priority},
        )
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
        self.audit.record(
            "user.quota.update",
            actor=self._current_actor,
            target=username,
            detail={"max_gpus": max_gpus, "max_active_jobs": max_active_jobs},
        )
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

    def checkpoint_usage(self) -> dict[str, Any]:
        """每个任务的产物占用，用来判断磁盘都花在哪了。

        清理前最想知道的是「哪些任务占了大头」，而不是挨个 du。
        查不到的任务（已被清理或从没跑过）不出现在结果里，
        避免列表里塞满 0 字节条目。

        统计的是 Web 容器里实际挂载的目录（checkpoints_mount_path）。
        宿主机路径在容器内不存在，拿它 stat 只会永远得到 0。
        """
        root = Path(self.settings.checkpoints_mount_path)
        items: list[dict[str, Any]] = []
        total = 0

        if root.is_dir():
            for entry in root.iterdir():
                if not entry.is_dir() or entry.is_symlink():
                    continue
                try:
                    size, files = _dir_usage(entry)
                except OSError:
                    continue
                items.append({"job": entry.name, "bytes": size, "files": files})
                total += size

        items.sort(key=lambda item: item["bytes"], reverse=True)
        return {"total_bytes": total, "items": items}

    def estimate_run(self, request: Any) -> dict[str, Any]:
        """按历史实测速度估算这次训练要跑多久。

        用户填 5000 步时最想知道的就是这个。用同 batch 的历史样本算，
        没有样本就退到一个偏保守的默认值。
        """
        from .estimate import estimate_duration

        recipe = self.get_recipe(request.recipe_id)
        defaults = dict(recipe.defaults or {})
        defaults.update({k: v for k, v in (request.overrides or {}).items() if v is not None})

        try:
            steps = int(defaults.get("steps") or 0)
        except (TypeError, ValueError):
            steps = 0
        if steps <= 0:
            return {"estimated_seconds": None, "estimated_text": "—", "source": "unknown", "samples": 0}

        raw_batch = defaults.get("batch_size")
        try:
            batch_size = int(raw_batch) if raw_batch is not None else None
        except (TypeError, ValueError):
            batch_size = None

        # 从历史任务里取真实速度：命令里带着 batch_size 和 steps
        history: list[dict[str, Any]] = []
        for job in self._list_jobs(self.kubernetes.list_namespace_pods(), limit=None)["items"]:
            if job.get("status") != "SUCCEEDED":
                continue
            parsed = _parse_training_args(str(job.get("command") or ""))
            if not parsed:
                continue
            history.append(
                {
                    "steps": parsed.get("steps"),
                    "batch_size": parsed.get("batch_size"),
                    "duration_seconds": job.get("duration_seconds"),
                }
            )

        result = estimate_duration(steps=steps, batch_size=batch_size, history=history)
        result["steps"] = steps
        result["batch_size"] = batch_size
        return result

    def failure_summary(self, *, limit: int = 20) -> dict[str, Any]:
        """失败原因分布。运维用来判断「是不是平台自己在制造失败」。

        只分析最近 limit 个失败任务——更早的历史没有排查价值，
        全量分析还会把接口拖慢。
        """
        from .failure_stats import summarize_failures

        jobs = self._list_jobs(self.kubernetes.list_namespace_pods(), limit=None)["items"]
        failed = [job for job in jobs if job.get("status") == "FAILED"][:limit]

        diagnoses: dict[str, Any] = {}
        for job in failed:
            name = str(job.get("name") or "")
            try:
                result = self.job_diagnosis(name)
            except Exception:  # noqa: BLE001 - 单个任务诊断失败不该影响整体统计
                result = None
            diagnoses[name] = (result or {}).get("diagnosis")

        return summarize_failures(failed, diagnoses)

    def plan_cleanup(
        self,
        *,
        older_than_days: int,
        keep_latest: int = 20,
        include_checkpoints: bool = False,
    ) -> dict[str, Any]:
        """算出该清理什么，不执行。清理不可逆，先让用户看一眼。"""
        from .cleanup import plan_cleanup as build_plan

        jobs = self._list_jobs(self.kubernetes.list_namespace_pods(), limit=None)["items"]
        plan = build_plan(
            jobs,
            older_than_days=older_than_days,
            keep_latest=keep_latest,
            include_checkpoints=include_checkpoints,
        )
        return plan.as_dict()

    def cleanup_jobs(
        self,
        *,
        older_than_days: int,
        keep_latest: int = 20,
        include_checkpoints: bool = False,
    ) -> dict[str, Any]:
        """清理历史任务：K8s 对象、历史记录、可选地删 checkpoint。

        每一步独立记录成败——删 K8s 对象可能因为已被 TTL 回收而 404（这不算失败），
        删目录可能因为权限失败。整体不因为个别失败而中断，最后汇总报告。
        """
        from .cleanup import plan_cleanup as build_plan

        jobs = self._list_jobs(self.kubernetes.list_namespace_pods(), limit=None)["items"]
        plan = build_plan(
            jobs,
            older_than_days=older_than_days,
            keep_latest=keep_latest,
            include_checkpoints=include_checkpoints,
        )

        deleted: list[str] = []
        failed: list[dict[str, str]] = []

        for name in plan.job_names:
            try:
                # 对象可能已被 ttlSecondsAfterFinished 回收，404 视为已完成
                self.kubernetes.delete_job(name)
            except ServiceError as exc:
                if exc.status_code != 404:
                    failed.append({"job": name, "stage": "k8s", "error": str(exc)})
                    continue
            try:
                self.history.forget_job(name)
            except Exception as exc:  # noqa: BLE001 - 单条失败不该中断整批
                failed.append({"job": name, "stage": "history", "error": str(exc)})
                continue
            deleted.append(name)

        removed_checkpoints: list[str] = []
        for name in plan.checkpoints:
            # 路径必须先校验：直接字符串拼接 + rmtree 是删库级别的风险，
            # 我们已经因为一次变量展开为空丢掉过全部产物。
            # 删除动作发生在 Web 容器里，根目录必须是容器内路径——
            # 拿宿主机路径去 rmtree，要么删不到，要么删到同名的错误目录。
            try:
                path = safe_job_dir(Path(self.settings.checkpoints_mount_path), name)
            except UnsafePath as exc:
                failed.append({"job": name, "stage": "path_check", "error": str(exc)})
                continue
            try:
                if path.is_dir():
                    import shutil

                    shutil.rmtree(path)
                    removed_checkpoints.append(name)
            except OSError as exc:
                failed.append({"job": name, "stage": "checkpoint", "error": str(exc)})

        self.audit.record(
            "jobs.cleanup",
            actor=self._current_actor,
            target=f"{len(deleted)} jobs",
            detail={
                "older_than_days": older_than_days,
                "keep_latest": keep_latest,
                "checkpoints_removed": len(removed_checkpoints),
            },
        )
        return {
            "deleted_jobs": deleted,
            "removed_checkpoints": removed_checkpoints,
            "failed": failed,
            "kept_running": plan.keep,
            "summary": {
                "deleted": len(deleted),
                "checkpoints": len(removed_checkpoints),
                "failed": len(failed),
            },
        }

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

        # 排队位置与等待估算。需要「同一队列里所有 Pending 的 Workload」做全局排序，
        # 因此放在收集完所有任务之后统一计算，而不是每个任务各算一次。
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

    def preflight_run(self, request: RunRequest) -> dict[str, Any]:
        """提交前预检：不创建任何对象，只检查能不能跑、会有什么问题。"""
        from .preflight import preflight as run_preflight

        recipe = self.get_recipe(request.recipe_id)
        dataset = self.get_dataset(request.dataset_id)
        summary = self.cluster_summary()
        return run_preflight(
            dataset=dataset,
            recipe=recipe.as_dict(),
            # 镜像可用性：Web 查不到节点镜像，只能靠历史推断，
            # 但能提前提醒「这个镜像平台没用过，可能拉不到」
            incoming_image=str(recipe.as_dict().get("image") or self.settings.default_image),
            known_images=self.list_images(),
            gpu_total=int(summary.get("gpu_total") or 0),
            gpu_allocated=int(summary.get("gpu_allocated") or 0),
            requested_gpus=recipe.gpu_count,
        )

    def create_run(self, request: RunRequest, owner: str) -> dict[str, Any]:
        """一键训练：展开配方与数据集，然后走既有 submit 路径。

        「查重名 → 生成编号 → 创建」这三步必须串行：多个用户同时点开始时，
        并发查到的 existing 是同一份快照，会生成同一个任务名，只有一个能建成功，
        其余用户看到「任务名已存在」这种对他们毫无意义的错误。
        """
        recipe = self.get_recipe(request.recipe_id)
        dataset = self.get_dataset(request.dataset_id)
        # 提交前预检：把能在入队前发现的问题直接拦下，不做无谓的 GPU 调度
        preflight_result = self.preflight_run(request)
        if not preflight_result["ok"]:
            errors = [check for check in preflight_result["checks"] if check["level"] == "error"]
            raise ServiceError(422, errors[0]["message"] if errors else "预检未通过")

        with self._run_create_lock:
            payload = self._build_run_payload(request, recipe, dataset, owner)
            spec = SubmitSpec.model_validate(payload)
            # 与 job.submit 一致：审计记操作者（current_actor），不是任务归属 owner
            self.audit.record(
                "run.create",
                actor=self._current_actor,
                target=spec.name,
                detail={"recipe": recipe.id, "dataset": request.dataset_id, "overrides": request.overrides},
            )
            return self.submit(spec, owner=owner)

    def _build_run_payload(
        self,
        request: RunRequest,
        recipe: Any,
        dataset: dict[str, Any],
        owner: str,
    ) -> dict[str, Any]:
        """在锁内完成「查已有名字 → 生成唯一名字 → 组装提交体」。"""
        from .runs import build_run

        existing = {item["name"] for item in self._list_jobs(self.kubernetes.list_namespace_pods())["items"]}
        return build_run(
            request,
            recipe=recipe,
            dataset=dataset,
            service=self,
            owner=owner,
            existing_names=existing,
        )

    # ------------------------------------------------------------------
    # 数据集
    # ------------------------------------------------------------------
    def dataset_store(self) -> DatasetStore:
        """惰性创建，避免每次启动都建表（表本身是幂等的）。

        API 层判断「这个 id 是否已注册」也用它，所以是公开方法。
        """
        store = getattr(self, "_datasets", None)
        if store is None:
            store = DatasetStore(self.settings.db_path)
            self._datasets = store
        return store

    def _datasets_root(self) -> Path:
        return Path(self.settings.datasets_mount_path)

    def list_datasets(self, refresh: bool = False) -> dict[str, Any]:
        """列出数据集。缓存为准，refresh 时先重扫有变化的目录。"""
        store = self.dataset_store()
        root = self._datasets_root()
        if refresh:
            pending = store.needs_rescan(root)
            if pending:
                store.scan_many(root, pending)
        # 列表页只需要展示用字段：action_names / state_names / stats 这些长数组
        # 在大数据集上会让响应膨胀十几倍。详情接口再返回完整内容。
        items = [_slim_dataset(_with_path_check(meta.as_dict())) for meta in store.list_all()]
        summary = {
            "total": len(items),
            "ready": sum(1 for item in items if item["status"] == "ready"),
            "warning": sum(1 for item in items if item["status"] == "warning"),
            # missing 必须算进「不可用」：它和 unusable 一样不能拿来训练，
            # 若不计入，页面顶部会显示「0 个不可用」而列表里全是红色徽标。
            "unusable": sum(
                1 for item in items if item["status"] in ("unusable", "error", "missing")
            ),
            "missing": sum(1 for item in items if item["status"] == "missing"),
            "total_frames": sum(int(item["frames"]) for item in items),
        }
        return {"items": items, "summary": summary, "root": str(root)}

    def get_dataset(self, dataset_id: str, deep: bool = False) -> dict[str, Any]:
        store = self.dataset_store()
        meta = store.get(dataset_id)
        # 缓存没有、或要求深扫描而现有记录还是浅扫描时，补扫一次
        if meta is None or (deep and meta.scan_depth != "deep"):
            try:
                meta = self._rescan(store, dataset_id, deep=deep)
            except FileNotFoundError as exc:
                raise ServiceError(404, f"dataset {dataset_id} not found") from exc
        return meta.as_dict()

    def scan_datasets(self, dataset_id: str | None = None, deep: bool = False) -> dict[str, Any]:
        store = self.dataset_store()
        root = self._datasets_root()
        if dataset_id:
            if store.get(dataset_id) is None and not (root / dataset_id).is_dir():
                raise ServiceError(404, f"dataset {dataset_id} not found")
            try:
                results = [self._rescan(store, dataset_id, deep=deep)]
            except FileNotFoundError as exc:
                # 目录被删了：扫描器会把它从库里清掉，但要告诉调用方扫不到。
                store.delete(dataset_id)
                raise ServiceError(404, f"dataset {dataset_id} not found") from exc
        else:
            results = store.scan_many(root, None, deep=deep)
        return {
            "scanned": len(results),
            "items": [meta.as_dict() for meta in results],
        }

    def scan_dataset(self, dataset_id: str, deep: bool = False) -> DatasetMeta:
        """重扫单个已注册数据集并返回元数据。未注册返回 404，目录不在返回 404。"""
        store = self.dataset_store()
        if store.get(dataset_id) is None:
            raise ServiceError(404, f"dataset {dataset_id} not found")
        try:
            return self._rescan(store, dataset_id, deep=deep)
        except FileNotFoundError as exc:
            store.delete(dataset_id)
            raise ServiceError(404, f"dataset {dataset_id} not found") from exc

    def _rescan(self, store: DatasetStore, dataset_id: str, deep: bool = False) -> DatasetMeta:
        """按数据集的**实际路径**重扫。

        启动扫描发现的数据集都在数据集根下，``store.scan_one(root, id)`` 就够；
        但通过注册接口登记的目录可能位于 ``PLATFORM_DATASET_ALLOWED_ROOTS``
        指定的其它根下，所以优先用库里记下的路径。路径缺失时退回扫描根，
        兼容早期版本写入的记录。
        """
        meta = store.get(dataset_id)
        if meta is not None and meta.path and (Path(meta.path) / "meta" / "info.json").is_file():
            return store.scan_path(Path(meta.path), dataset_id, deep=deep)
        return store.scan_one(self._datasets_root(), dataset_id, deep=deep)

    def register_dataset(self, path: str, dataset_id: str | None = None) -> DatasetMeta:
        """注册一个已存在的数据集目录（仅 admin 可达，鉴权在 API 层）。

        不做任何文件系统写入：验证通过后按实际路径浅扫描一次并落库即可。
        重复 id 返回 409——不覆盖已有记录，避免用户「注册」一个 id 就把别人的
        数据集从列表里顶掉。
        """
        try:
            target = resolve_dataset_dir(self.settings, path, dataset_id)
            check_lerobot_format(target.path)
        except UnsafeDatasetPath as exc:
            raise ServiceError(400, str(exc)) from exc

        store = self.dataset_store()
        if store.get(target.dataset_id) is not None:
            raise ServiceError(409, f"dataset {target.dataset_id} already registered")

        # 校验阶段已经确认 info.json 合法；扫描器若仍判 unusable（例如 features 缺
        # observation.state / action），照样落库——让用户在列表里看到具体 issue，
        # 比返回一个含糊的 400 有用得多。
        meta = store.scan_path(target.path, target.dataset_id)
        self.audit.record(
            "dataset.register",
            actor=self._current_actor,
            target=target.dataset_id,
            detail={"path": str(target.path), "format": meta.format, "status": meta.status},
        )
        return meta

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
        self.audit.record(
            "image.params.put",
            actor=self._current_actor,
            target=image,
            detail={"params": [param.key for param in template.params]},
        )
        return {"image": image, "source": "file", "template": json.loads(serialized)}

    def delete_params_template(self, image: str) -> None:
        if not validate_image_name(image):
            raise ServiceError(404, f"image {image} not found")
        self.history.delete_image_params(image)
        self.audit.record("image.params.delete", actor=self._current_actor, target=image)

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

    def _job_existed(self, name: str) -> bool:
        """这个任务是否曾经存在过（不管 K8s 对象还在不在）。

        为什么要单独抽一个方法：每个读接口都各自写一遍「K8s 有没有、取消记录
        有没有」，很容易漏。实测就漏过——任务被 ttlSecondsAfterFinished 回收后，
        /metrics 能读（那里改过一次），/logs、/events、/timeline 却坚持 404。
        同一个任务一半接口说存在、一半说不存在，用户只会觉得平台坏了。

        判据有三个，任一成立就算存在过：
          - K8s 对象还在（正在跑或还没被回收）
          - 有取消记录
          - 有提交记录（最常见：跑完了，对象被 TTL 回收了）
        """
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
            # K8s 对象没了但历史里有提交记录：说明它曾经存在过。
            # 常见原因是 ttlSecondsAfterFinished 到期回收、节点重启或手工删除。
            # 直接报 404 会让用户看到任务"凭空消失"且没有任何解释——
            # 至少要把当初提交了什么、什么时候提交的告诉他。
            submission = self.history.find_submission(name)
            if submission is not None:
                return self._vanished_response(name, *submission)
            raise ServiceError(404, f"job {name} not found")
        pods = self.kubernetes.list_job_pods(name)
        workload = self._matching_workload(job, self.kubernetes.list_workloads(), pods)
        return self._job_response(job, workload, pods)

    def _vanished_response(self, name: str, spec: Any, submitted_at: str) -> dict[str, Any]:
        """任务对象已不存在但历史里有提交记录时的响应。

        status 用 ``GONE`` 而不是某个终态：我们并不知道它最后是成功还是失败，
        编造一个终态比不解释更糟。
        """
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
        self.audit.record(
            "job.cancel",
            actor=self._current_actor,
            target=name,
            detail={"previous_status": current, "recorded_as": preserved},
        )
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
            # 任务跑完被回收后日志就读不到了（Pod 已经不存在），
            # 但它确实存在过，不该报 404——返回空日志并说清楚原因。
            self._require_job_existed(name)
            return {
                "name": name,
                "pod_name": None,
                "logs": "任务已经结束，训练 Pod 已被回收，日志不再可取。\n"
                "需要排查历史问题时可以看「事件」和「时间线」，那里保留了任务的生命周期记录。",
            }
        pods = self.kubernetes.list_job_pods(name)
        pod = self._select_pod(pods)
        if pod is None:
            return {
                "name": name,
                "pod_name": None,
                "logs": "训练 Pod 还没有创建。任务可能仍在等待 Kueue 准入或集群配额，"
                "稍后再看；如果一直停在这里，看「时间线」确认卡在哪个阶段。",
            }
        pod_name = pod.get("metadata", {}).get("name")
        waiting = _container_waiting(pod)
        if waiting is not None:
            # 容器还在 waiting（拉镜像、创建失败、崩溃退避），Pod 里没有可读的
            # 容器实例，此时调 K8s 日志接口只会拿到 400 —— 那正是用户看到的
            # 「Kubernetes rejected the request: 400 Bad Request」。
            # 直接把容器状态翻译成人话返回，比一个 400 有用得多。
            return {
                "name": name,
                "pod_name": pod_name,
                "logs": _waiting_log_message(waiting, _pod_image(pod)),
                "container_waiting": waiting["state"],
                "container_reason": waiting["reason"],
            }
        raw = self.kubernetes.read_pod_log(pod_name, tail_lines)
        return {
            "name": name,
            "pod_name": pod_name,
            "logs": normalize_log_newlines(raw),
        }

    def job_checkpoints(self, name: str, spec_owner: str | None = None) -> dict[str, Any]:
        """列出某个任务写出的 checkpoint 文件。"""
        del spec_owner  # 归属校验由路由层统一做
        from .checkpoints import list_checkpoints

        # 容器内路径：宿主机路径在这里读不到
        result = list_checkpoints(self.settings.checkpoints_mount_path, name)
        return {"name": name, **result}

    def job_timeline(self, name: str) -> dict[str, Any]:
        """作业阶段时间轴：已提交 → 已入队 → 已准入 → 拉镜像 → 启动 → 训练 → 结束。"""
        from .timeline import build_timeline

        job = self.kubernetes.get_job(name)
        if job is None:
            self._require_job_existed(name)
            return {"name": name, "current_stage": "finished", "items": []}
        pods = self.kubernetes.list_job_pods(name)
        workload = self._matching_workload(job, self.kubernetes.list_workloads(), pods)
        status = str(self._job_response(job, workload, pods)["status"])
        pod = self._select_pod(pods)
        result = build_timeline(job=job, workload=workload, pod=pod, status=status)
        return {"name": name, "status": status, **result}

    def job_diagnosis(self, name: str) -> dict[str, Any]:
        """归一化失败原因。只读，不写库。"""
        from .diagnostics import diagnose

        job = self.get_job(name)
        status = str(job.get("status"))
        if status not in ("FAILED", "UNKNOWN"):
            return {"name": name, "status": status, "diagnosis": None}

        log_tail = ""
        try:
            log_tail = self.logs(name, 200).get("logs") or ""
        except ServiceError:
            log_tail = ""
        events = []
        try:
            events = self.events(name).get("items") or []
        except ServiceError:
            events = []

        pod = self._latest_pod(job.get("name") or name)
        waiting = _container_waiting(pod)
        return {
            "name": name,
            "status": status,
            "diagnosis": diagnose(
                status=status,
                reason=job.get("pod_status"),
                message=job.get("message"),
                log_tail=log_tail[-8000:],
                events=events,
                # 容器没起来时容器状态才是根因，事件里往往只有一句
                # "Back-off pulling image"，光靠它说不出镜像名。
                waiting_reason=waiting["detail"] if waiting else None,
                image=str(job.get("image") or _pod_image(pod) or ""),
            ),
        }

    def _latest_pod(self, name: str) -> dict[str, Any] | None:
        """取这个任务最新的 Pod，拿不到就返回 None。

        诊断要的是容器状态（waiting.reason），它只存在于 Pod 上。K8s 临时不
        可用时宁可退化成「没有容器状态」也不能让诊断接口整个 500——
        用户正是在任务出问题的时候才点诊断。
        """
        try:
            return self._select_pod(self.kubernetes.list_job_pods(name))
        except Exception:  # noqa: BLE001 - 诊断是尽力而为
            return None

    def job_metrics(self, name: str, limit: int = 500) -> dict[str, Any]:
        """训练指标（loss 曲线）。

        判定「任务是否曾经存在」时不能只看 K8s 对象：ttlSecondsAfterFinished
        到期回收后对象就没了，但指标文件还老老实实躺在磁盘上。
        只认 K8s 对象的话，历史训练曲线会在几小时后突然全部 404——
        而这恰恰是用户想回头看的那些数据。

        所以除了 K8s 对象和取消记录，还要认「历史里有提交记录」。
        """
        job = self.kubernetes.get_job(name)
        if (
            job is None
            and self.history.get_cancellation(name) is None
            and self.history.find_submission(name) is None
        ):
            raise ServiceError(404, f"job {name} not found")
        from .metrics import read_metrics  # 局部导入，保持模块加载轻量

        # 容器内路径：宿主机路径在这里读不到
        return read_metrics(self.settings.checkpoints_mount_path, name, limit=limit)

    def job_tensorboard(self, name: str, limit: int = 2000) -> dict[str, Any]:
        """TensorBoard 曲线（任意 tag）。

        与 ``job_metrics`` 同样的判定：K8s 对象被 TTL 回收后，events 文件仍在磁盘上，
        因此也要认取消记录和历史提交记录，否则历史曲线会突然 404。
        """
        from . import tensorboard as tensorboard_service  # 局部导入：tensorboard 是重依赖

        job = self.kubernetes.get_job(name)
        if job is None and self.history.get_cancellation(name) is None and self.history.find_submission(name) is None:
            raise ServiceError(404, f"job {name} not found")
        try:
            # 容器内路径：宿主机路径在这里读不到
            return tensorboard_service.read_scalars(self.settings.checkpoints_mount_path, name, limit=limit)
        except tensorboard_service.TensorboardUnavailable as exc:
            # 镜像里漏装 tensorboard 时，返回明确的 503 而不是 500 + traceback
            raise ServiceError(503, str(exc)) from exc

    def events(self, name: str) -> dict[str, Any]:
        job = self.kubernetes.get_job(name)
        if job is None:
            self._require_job_existed(name)
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
        """集群总览（带 2 秒 TTL 缓存）。

        总览页每 4 秒轮询、预检每次点击都会调一次；不打缓存的话一次提交前预检
        就可能触发 3-4 次 K8s API 调用。缓存时间远小于页面的轮询周期，
        用户感知不到延迟，但能把重复调用挡掉。
        """
        ttl = self.settings.cluster_summary_ttl
        if ttl > 0:
            with self._cluster_lock:
                cached = self._cluster_cache
                if cached is not None and (time.monotonic() - cached[0]) < ttl:
                    # 返回副本，避免调用方修改缓存内容
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

    def _annotate_queue(
        self,
        live: dict[str, dict[str, Any]],
        workloads: list[dict[str, Any]],
        cluster_queue: dict[str, Any] | None = None,
    ) -> None:
        """给每个任务补上 queue_position / queue_eta_seconds / queue_state。

        估算依赖三份数据，都在这一次调用里一次算好，避免 O(n^2)：
          - 同一个 ClusterQueue 里 Pending Workload 的排队序位
          - 正在运行任务的已运行时长（用于扣减）
          - 同配方任务的历史运行时长中位数（用于推断典型耗时）
        """
        durations: dict[str, float] = {}
        try:
            # 只用实时任务的 duration_seconds：历史提交记录里没有耗时字段
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
            # 排队中任务还要告诉用户「为什么排不上、能做什么」
            if info.get("queue_state") == "queued" and workload is not None:
                try:
                    # 传个人用量与配额，否则 user_quota 这条根因永远判定不出来
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

    def describe_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """把一个 PyTorchJob 对象整理成与列表/详情接口一致的响应。

        供通知等「只有一个 job 对象、不想为此再拉一遍全命名空间」的调用方
        使用。Pod 用单任务标签查询，Workload 找不到时按 None 处理——
        Kueue 尚未创建 Workload 是常态，不是错误。
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
            # 单机提交时用户不会填 gpus_per_node，返回 null 对前端没意义。
            # 回填实际值：多机用声明的值，单机就是 gpus 本身。
            "gpus_per_node": spec.gpus_per_node
            if spec.gpus_per_node is not None
            else (spec.gpus // spec.nodes if spec.nodes else None),
            "created_at": created_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": _duration_seconds(started_at, finished_at, status),
            "workload_status": workload_status,
            "pod_status": pod_phase,
            "message": message,
            "checkpoint_dir": f"{self.settings.checkpoints_mount_path.rstrip('/')}/{spec.name}",
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

    def _cancelled_response(self, record: dict[str, Any]) -> dict[str, Any]:
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
            # 单机提交时用户不会填 gpus_per_node，返回 null 对前端没意义。
            # 回填实际值：多机用声明的值，单机就是 gpus 本身。
            "gpus_per_node": spec.gpus_per_node
            if spec.gpus_per_node is not None
            else (spec.gpus // spec.nodes if spec.nodes else None),
            "created_at": created_at,
            "started_at": None,
            "finished_at": cancelled_at,
            "duration_seconds": None,
            "workload_status": "Cancelled" if status == "CANCELLED" else "Finished",
            "pod_status": None,
            "message": message,
            "checkpoint_dir": f"{self.settings.checkpoints_mount_path.rstrip('/')}/{spec.name}",
        }


def _whole_number(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _container_waiting(pod: dict[str, Any] | None) -> dict[str, str] | None:
    """Pod 里是否有容器停在 waiting 态。

    返回 ``{"state", "reason", "message", "detail"}``；容器都已启动（或没有
    容器状态）时返回 None。``state`` 是 waiting 之外的上层原因：容器根本没被
    创建过时容器状态是空的，但 Pod 自己会在 ``status.reason`` 里留下
    ImagePullBackOff / ErrImagePull，这两种都要认。
    """
    if not pod:
        return None
    status = _dict(pod.get("status"))
    waiting: dict[str, str] = {}
    for container_status in _list(status.get("container_statuses")):
        state = _dict(container_status.get("state"))
        waiting_state = _dict(state.get("waiting"))
        if waiting_state:
            waiting = waiting_state
            break
    if not waiting:
        # 容器状态还没被 kubelet 写进来时，Pod 级别的 reason 同样有信息量。
        pod_reason = status.get("reason")
        if isinstance(pod_reason, str) and _POD_LEVEL_WAITING.search(pod_reason):
            waiting = {"reason": pod_reason, "message": str(status.get("message") or "")}
        else:
            return None
    reason = str(waiting.get("reason") or "Waiting")
    message = str(waiting.get("message") or "")
    return {
        "state": "waiting",
        "reason": reason,
        "message": message,
        "detail": f"{reason}: {message}" if message else reason,
    }


# 容器都没创建出来时，K8s 把拉镜像失败记在 Pod 自己的 status.reason 上。
_POD_LEVEL_WAITING = re.compile(
    r"ImagePullBackOff|ErrImagePull|InvalidImageName|CreateContainerError|"
    r"CreateContainerConfigError|CrashLoopBackOff",
    re.I,
)


def _pod_image(pod: dict[str, Any] | None) -> str:
    """从 Pod spec 里取训练容器（name=pytorch）的镜像名。"""
    for container in _list(_dict(pod.get("spec")).get("containers")):
        if _dict(container).get("name") == "pytorch":
            return str(_dict(container).get("image") or "")
    return ""


def _waiting_log_message(waiting: dict[str, str], image: str) -> str:
    """把「容器在 waiting」翻译成用户能看懂的一句日志。

    这里刻意不抛错：读日志失败不是请求失败，用户要的是「为什么没有日志」。
    文案按 reason 分开写，因为 ImagePullBackOff（镜像不在节点上）和
    CrashLoopBackOff（起来了就挂）要做的修复动作完全不同。
    """
    reason = waiting.get("reason") or "Waiting"
    detail = waiting.get("message") or ""
    shown_image = image or "<未知镜像>"
    lines = [f"暂时没有日志：容器尚未启动（{reason}）。"]
    lowered = reason.lower()
    if "imagepull" in lowered or "errimagepull" in lowered or "invalidimagename" in lowered:
        lines.append(
            f"镜像拉取失败：镜像 {shown_image} 在训练节点（K3s/containerd）上不存在。"
            "buildah 里有这个镜像不代表 K3s 能用——导出并导入后再重新提交任务："
            f"`buildah push {shown_image} docker-archive:/tmp/buildah-export.tar`，"
            "`k3s ctr -n k8s.io images import /tmp/buildah-export.tar`。"
        )
    elif "crashloop" in lowered:
        lines.append(
            "容器启动后反复退出（CrashLoopBackOff）。Kubernetes 会一直退避重试，"
            "任务会持续占用 GPU 配额——先取消任务，再去「事件」里看退出原因或退出码。"
        )
    elif "createcontainer" in lowered:
        lines.append(
            f"容器创建失败（{reason}）。通常是挂载的 ConfigMap/Secret 不存在或权限不足，"
            "需要管理员核对任务模板。"
        )
    elif "containercreating" in lowered or "podinitializing" in lowered:
        lines.append(
            "容器正在创建中（拉镜像 / 挂载卷），稍等片刻再刷新日志。"
            "如果持续很久，看「事件」里的具体原因。"
        )
    else:
        lines.append(
            "容器还没有开始运行，因此没有日志可读。看「事件」和「时间线」了解它卡在哪一步。"
        )
    if detail:
        lines.append(f"Kubernetes 原文：{detail}")
    return "\n".join(lines)


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

# 列表接口裁剪掉的字段：详情页才需要
_DETAIL_ONLY_FIELDS = ("action_names", "state_names", "stats")


def _with_path_check(payload: dict[str, Any]) -> dict[str, Any]:
    """目录不在了就把状态改成 missing，并给一句说明。

    为什么必须做：数据集列表读的是 SQLite 缓存，缓存只记「上次扫描时是什么样」。
    宿主机目录被删、挂载盘掉了以后，缓存里仍然是 ``status: ready``，用户以为还能用，
    提交任务后才在容器里报 ``No such file or directory``——一次 GPU 调度就这么浪费掉。

    判定只看目录本身是否存在，不重新扫描（列表接口要快）。目录回来以后
    下一次扫描会把状态写回去，所以这里不改库，只改返回给前端的副本。
    """
    path = str(payload.get("path") or "")
    status = payload.get("status")
    # 扫描失败（status=error）优先保留：那条 issue 里带着失败原因，比「目录不存在」更有用。
    if not path or status == "error":
        return payload
    if Path(path).is_dir():
        return payload
    checked = dict(payload)
    checked["status"] = "missing"
    issues = list(payload.get("issues") or [])
    if not any(issue.get("code") == "path_missing" for issue in issues):
        issues.insert(
            0,
            {
                "level": "error",
                "code": "path_missing",
                "message": f"数据集目录已不存在：{path}",
            },
        )
    checked["issues"] = issues
    return checked


def _slim_dataset(payload: dict[str, Any]) -> dict[str, Any]:
    """列表用的数据集摘要。

    保留所有**判断与展示**需要的字段（状态、集数、维度、issue 列表），
    去掉每维名称与统计数组——那些只在详情页用得上。
    """
    slim = {key: value for key, value in payload.items() if key not in _DETAIL_ONLY_FIELDS}
    # 保留维度的「数量」信息，前端画徽标要用
    slim["action_dim"] = len(payload.get("action_names") or []) or payload.get("action_dim") or 0
    slim["state_dim"] = len(payload.get("state_names") or []) or payload.get("state_dim") or 0
    slim["has_stats"] = payload.get("stats") is not None
    return slim


def normalize_log_newlines(raw: str) -> str:
    """把日志里的回车符规整成换行。

    为什么要这么做：训练用的进度条（tqdm 那套）靠 `\r` 回车覆盖同一行
    来做原地刷新，终端里看着是动态进度条。但网页的 <pre> 里 `\r` 不换行，
    于是几十次进度刷新被拼成一条几万字符的超长行，
    真的 INFO 行全被挤到后面看不清。

    这里只做一件事：`\r\n` 和单独的 `\r` 都当成行结束。
    结果会比终端里看到的"长"——每个进度刷新各占一行——
    但至少能读，而且保留了完整信息（没有丢日志）。
    """
    if not raw:
        return raw
    return raw.replace("\r\n", "\n").replace("\r", "\n")

