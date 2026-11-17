from __future__ import annotations

import yaml
import copy
import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from ..config import ROLE_ADMIN, ROLE_USER, Settings
from ..history import HistoryStore, utc_now
from ..models import SubmitSpec, check_labels, check_payload_size
from ..params_templates import (
    BUILTIN_TEMPLATE,
    MAX_TEMPLATE_BYTES,
    parse_template,
    validate_image_name,
)
from ..recipes.loader import Recipe, RecipeError, RecipeStore, parse_recipe
from ..templates.pytorchjob import render_pytorch_job
from .audit import AuditStore
from .cluster import ClusterServiceMixin
from .datasets import DatasetServiceMixin
from .errors import ServiceError
from .job_views import (
    JobViewServiceMixin,
    _dict,
    _format_time,
    _list,
)
from .job_views import (
    _is_permanent_start_failure as _is_permanent_start_failure,
)
from .job_views import (
    _pod_waiting_reason as _pod_waiting_reason,
)
from .kubernetes import KubernetesClient
from .maintenance import MaintenanceServiceMixin
from .maintenance import _dir_usage as _dir_usage
from .quotas import ACTIVE_STATUSES, Quota, QuotaStore, Usage, check_quota, effective_quota
from .sessions import SessionStore
from .users import UserStore

if TYPE_CHECKING:
    from ..params_templates import ParamsTemplate
    from .runs import RunRequest


class PlatformService(
    JobViewServiceMixin,
    DatasetServiceMixin,
    ClusterServiceMixin,
    MaintenanceServiceMixin,
):
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
        self.sessions = sessions or SessionStore(settings.db_path, enabled=settings.auth_mode in ("ldap", "local"))
        self.quotas = quotas or QuotaStore(settings.db_path)
        self.users = UserStore(settings.db_path)
        # 审计日志：只记变更类操作，写失败绝不阻塞业务
        self.audit = AuditStore(settings.db_path)
        # 当前操作者，由 API 层在每个请求开始时设置（审计用）
        self._current_actor = "anonymous"
        # 集群摘要缓存：预检与总览页都会读它，频繁点击时不该每次都打 K8s API。
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
            # K8s 对象创建成功后才记录历史，失败创建不能留下幽灵任务。
            self.history.record_submission(spec, submitted_at)
        # 审计记操作者，与任务归属 owner 是两个概念。
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

        口径：K3s 实时任务 ∪ 历史中未取消的记录。取消或删除后记录变终态，额度立即释放。
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
        """合并活跃会话、配额覆盖和任务提交者，不额外维护用户表。"""
        from ..local_users import parse_local_users

        names: dict[str, dict[str, Any]] = dict(self.sessions.known_users())
        for uname, uinfo in self.users.list_all().items():
            names.setdefault(uname, uinfo)
            names[uname]["role"] = uinfo["role"]
            names[uname]["source"] = "local"
        for uname, uentry in parse_local_users(self.settings.local_users).entries.items():
            names.setdefault(
                uname,
                {"username": uname, "role": uentry.role, "source": "local", "last_seen_at": None},
            )
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
            # 从未登录过的账号角色未知，按最小权限展示为 user。
            names.setdefault(
                username,
                {"username": username, "role": ROLE_USER, "source": "ldap", "last_seen_at": record.get("updated_at")},
            )
        job_items = self._list_jobs(self.kubernetes.list_namespace_pods())["items"]
        if self.settings.auth_mode != "local":
            for item in job_items:
                username = item.get("owner") or ""
                if username == "anonymous" and self.settings.auth_mode != "off":
                    continue
                if username and username not in names:
                    names[username] = {"username": username, "role": ROLE_USER, "source": "ldap", "last_seen_at": None}
        items = [
            self.user_view(
                info["username"],
                info.get("role") or ROLE_USER,
                info.get("source") or "ldap",
                _format_time(info.get("last_seen_at")),
            )
            for info in sorted(names.values(), key=lambda item: item["username"])
        ]
        return {"items": items}

    def create_user(
        self,
        username: str,
        password: str,
        role: str = ROLE_USER,
        max_gpus: int | None = None,
        max_active_jobs: int | None = None,
    ) -> dict[str, Any]:
        from ..local_users import parse_local_users

        if parse_local_users(self.settings.local_users).get(username) is not None:
            raise ServiceError(409, "user_exists", f"用户 {username} 已在环境变量配置中存在")
        created = self.users.create(username, password, role)
        if max_gpus is not None or max_active_jobs is not None:
            self.set_quota(username, max_gpus, max_active_jobs)
        self.audit.record("user.create", actor=self._current_actor, target=username, detail={"role": role})
        return self.user_view(created.username, created.role, "local", _format_time(created.updated_at))

    def update_user(
        self,
        username: str,
        *,
        role: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        updated = self.users.update(username, role=role, password=password)
        self.audit.record("user.update", actor=self._current_actor, target=username, detail={"role": updated.role})
        return self.user_view(updated.username, updated.role, "local", _format_time(updated.updated_at))

    def delete_user(self, username: str) -> dict[str, Any]:
        self.users.delete(username)
        self.quotas.delete_override(username)
        self.sessions.delete_for_user(username)
        self.audit.record("user.delete", actor=self._current_actor, target=username, detail={})
        return {"deleted": username}

    # ------------------------------------------------------------------
    # 训练配方与一键训练
    # ------------------------------------------------------------------
    def _recipe_store(self) -> RecipeStore:
        store = getattr(self, "_recipes", None)
        if store is None:
            # 覆盖层来自历史库（管理员页面上保存的完整 YAML），惰性回调保证
            # 保存后下一次读取立即生效，不需要重启或清缓存。
            store = RecipeStore(
                overrides=lambda: {
                    recipe_id: record["yaml"]
                    for recipe_id, record in self.history.list_recipe_overrides().items()
                }
            )
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

    # --- 配方管理（仅 admin；v1.1 完整 YAML 编辑） --------------------------
    def admin_list_recipes(self) -> dict[str, Any]:
        """管理视图的配方列表：含隐藏的，并标注 默认/已覆盖 与覆盖元信息。"""
        meta = self.history.list_recipe_overrides()
        items = []
        for recipe in self._recipe_store().load_all():
            item = recipe.as_dict()
            record = meta.get(recipe.id)
            item["updated_at"] = record["updated_at"] if record else None
            item["updated_by"] = record["updated_by"] if record else None
            items.append(item)
        return {"items": items}

    def admin_recipe_detail(self, recipe_id: str) -> dict[str, Any]:
        """编辑器回填：生效中的原始 YAML（覆盖优先，否则内置文件原文）。"""
        raw = self._recipe_store().raw_text(recipe_id)
        if raw is None:
            raise ServiceError(404, f"recipe {recipe_id} not found")
        yaml_text, source = raw
        record = self.history.get_recipe_override(recipe_id)
        fields: dict[str, Any] | None = None
        try:
            doc = yaml.safe_load(yaml_text)
            if isinstance(doc, dict):
                tmpl = doc.get("template") if isinstance(doc.get("template"), dict) else {}
                raw_params = tmpl.get("params") if isinstance(tmpl.get("params"), list) else []
                defaults_map = doc.get("defaults") if isinstance(doc.get("defaults"), dict) else {}
                fields = {
                    "name": doc.get("name", ""),
                    "description": doc.get("description", ""),
                    "image": doc.get("image", ""),
                    "entrypoint": doc.get("entrypoint", ""),
                    "gpu_count": doc.get("gpu_count", 1),
                    "estimated_vram_gb": doc.get("estimated_vram_gb"),
                    "public": doc.get("public", True),
                    "order": doc.get("order", 100),
                    "params": raw_params,
                    "defaults": defaults_map,
                }
        except yaml.YAMLError:
            fields = None
        return {
            "id": recipe_id,
            "source": source,
            "yaml": yaml_text,
            "fields": fields,
            "updated_at": record["updated_at"] if record else None,
            "updated_by": record["updated_by"] if record else None,
        }

    def save_recipe_fields(self, recipe_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """根据管理员表单字段更新配方 YAML 并走统一校验落库。"""
        raw = self._recipe_store().raw_text(recipe_id)
        if raw is None:
            raise ServiceError(404, f"recipe {recipe_id} not found")
        doc = yaml.safe_load(raw[0])
        if not isinstance(doc, dict):
            raise ServiceError(422, "invalid_recipe_yaml", "当前配方 YAML 无法解析为映射")
        for key in ("name", "description", "image", "entrypoint", "gpu_count", "estimated_vram_gb", "public", "order"):
            if key in fields:
                if fields[key] is None and key in ("estimated_vram_gb",):
                    doc.pop(key, None)
                else:
                    doc[key] = fields[key]
        param_defaults = fields.get("param_defaults")
        if isinstance(param_defaults, dict):
            tmpl = doc.get("template")
            if isinstance(tmpl, dict) and isinstance(tmpl.get("params"), list):
                defaults_map = doc.get("defaults") if isinstance(doc.get("defaults"), dict) else {}
                for p in tmpl["params"]:
                    if not isinstance(p, dict):
                        continue
                    pkey = p.get("key")
                    if pkey in param_defaults:
                        val = param_defaults[pkey]
                        p["default"] = val
                        if p.get("kind") == "extra" and isinstance(tmpl.get("extra_args"), dict):
                            tmpl["extra_args"]["default"] = "" if val is None else str(val)
                        elif pkey in defaults_map or val is not None:
                            defaults_map[pkey] = val
                if defaults_map:
                    doc["defaults"] = defaults_map
        rendered_yaml = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
        return self.save_recipe_override(recipe_id, rendered_yaml)

    def save_recipe_override(self, recipe_id: str, yaml_text: str) -> dict[str, Any]:
        """保存完整 YAML 覆盖。校验全部通过才落库——非法输入 422，绝不写坏配置。"""
        recipe = self._validate_recipe_yaml(recipe_id, yaml_text)
        self.history.put_recipe_override(recipe_id, yaml_text, utc_now(), self._current_actor)
        self.audit.record(
            "recipe.override.put",
            actor=self._current_actor,
            target=recipe_id,
            detail={"image": recipe.image, "public": recipe.public},
        )
        return self.admin_recipe_detail(recipe_id)

    def restore_recipe_default(self, recipe_id: str) -> dict[str, Any]:
        """删除覆盖，恢复随镜像发布的内置默认。没有覆盖时返回 restored=false。"""
        self._require_builtin_recipe(recipe_id)
        restored = self.history.delete_recipe_override(
            recipe_id, utc_now(), self._current_actor, action="restore"
        )
        if restored:
            self.audit.record("recipe.override.restore", actor=self._current_actor, target=recipe_id)
        detail = self.admin_recipe_detail(recipe_id)
        detail["restored"] = restored
        return detail

    def recipe_override_history(self, recipe_id: str, limit: int = 20) -> dict[str, Any]:
        """覆盖的变更历史（谁在什么时候保存/恢复/回滚过）。不回传 YAML 全量，保持列表轻。"""
        self._require_builtin_recipe(recipe_id)
        rows = self.history.list_recipe_override_history(recipe_id, limit=limit)
        items = []
        for row in rows:
            item = {key: value for key, value in row.items() if key != "yaml"}
            item["has_yaml"] = row["yaml"] is not None
            items.append(item)
        return {"items": items}

    def rollback_recipe(self, recipe_id: str) -> dict[str, Any]:
        """回滚到上一个版本，连续回滚沿版本链逐步倒退（v2→v1→内置默认）。

        历史是只追加的审计日志，不能删行；因此把 save/restore 视为入栈、
        rollback 视为出栈，回放整个历史得出当前版本栈，栈顶下一层就是回滚目标。
        这样「回滚产生的记录」本身不会成为下一次回滚的目标，避免在两个版本间振荡。
        """
        self._require_builtin_recipe(recipe_id)
        rows = self.history.list_recipe_override_history(recipe_id, limit=1000)
        stack: list[str | None] = [None]  # None = 内置默认（无覆盖）
        for row in reversed(rows):  # 历史按时间正序回放
            if row["action"] == "rollback":
                if len(stack) > 1:
                    stack.pop()
            else:  # save / restore
                stack.append(row["yaml"])
        if len(stack) < 2:
            raise ServiceError(409, f"recipe {recipe_id} 没有可回滚的历史版本")
        target = stack[-2]
        if target is None:
            self.history.delete_recipe_override(
                recipe_id, utc_now(), self._current_actor, action="rollback"
            )
        else:
            # 历史内容也重新过一遍校验：校验规则可能在那次保存之后收紧过。
            self._validate_recipe_yaml(recipe_id, target)
            self.history.put_recipe_override(
                recipe_id, target, utc_now(), self._current_actor, action="rollback"
            )
        self.audit.record(
            "recipe.override.rollback",
            actor=self._current_actor,
            target=recipe_id,
            detail={"to": "builtin" if target is None else "previous-override"},
        )
        return self.admin_recipe_detail(recipe_id)

    def _require_builtin_recipe(self, recipe_id: str) -> None:
        """覆盖只允许作用于随镜像发布的内置配方，不能凭空新建 id。"""
        builtin = RecipeStore().get(recipe_id)
        if builtin is None or builtin.source == "error":
            raise ServiceError(404, f"recipe {recipe_id} not found")

    def _validate_recipe_yaml(self, recipe_id: str, yaml_text: str) -> Recipe:
        import yaml

        self._require_builtin_recipe(recipe_id)
        if len(yaml_text.encode("utf-8")) > MAX_TEMPLATE_BYTES:
            raise ServiceError(422, f"配方 YAML 不能超过 {MAX_TEMPLATE_BYTES} 字节")
        try:
            payload = yaml.safe_load(yaml_text)
        except yaml.YAMLError as exc:
            raise ServiceError(422, f"YAML 解析失败：{exc}") from exc
        try:
            recipe = parse_recipe(payload, source=f"override:{recipe_id}.yaml")
        except RecipeError as exc:
            raise ServiceError(422, str(exc)) from exc
        if recipe.id != recipe_id:
            raise ServiceError(422, f"YAML 里的 id 是 {recipe.id!r}，不能改成与 {recipe_id!r} 不同的值")
        if not validate_image_name(recipe.image):
            raise ServiceError(422, f"镜像名 {recipe.image!r} 不合法")
        try:
            template = self.recipe_template(recipe)
        except ServiceError as exc:
            raise ServiceError(422, f"参数模板无效：{exc.message}") from exc
        declared = {param.key for param in template.params}
        unknown = sorted(set(recipe.defaults) - declared)
        if unknown:
            raise ServiceError(422, f"defaults 含模板未声明的参数：{', '.join(unknown)}")
        return recipe

    def preflight_run(self, request: RunRequest) -> dict[str, Any]:
        """提交前预检：不创建任何对象，只检查能不能跑、会有什么问题。"""
        from .preflight import preflight as run_preflight

        recipe = self.get_recipe(request.recipe_id)
        ds_ids = request.resolved_dataset_ids()
        datasets_list = [self.get_dataset(d) for d in ds_ids]
        dataset = datasets_list[0]
        summary = self.cluster_summary()
        return run_preflight(
            dataset=dataset,
            recipe=recipe.as_dict(),
            # Web 查不到节点镜像，用历史镜像做可用性提示。
            incoming_image=str(recipe.as_dict().get("image") or self.settings.default_image),
            known_images=self.list_images(),
            gpu_total=int(summary.get("gpu_total") or 0),
            gpu_allocated=int(summary.get("gpu_allocated") or 0),
            requested_gpus=recipe.gpu_count,
        )

    def create_run(self, request: RunRequest, owner: str) -> dict[str, Any]:
        """一键训练：展开配方与数据集，然后走既有 submit 路径。

        查重名、生成编号和创建必须串行，避免并发提交生成同一个任务名。
        """
        recipe = self.get_recipe(request.recipe_id)
        ds_ids = request.resolved_dataset_ids()
        datasets_list = [self.get_dataset(d) for d in ds_ids]
        dataset = datasets_list[0]
        preflight_result = self.preflight_run(request)
        if not preflight_result["ok"]:
            errors = [check for check in preflight_result["checks"] if check["level"] == "error"]
            raise ServiceError(422, errors[0]["message"] if errors else "预检未通过")

        with self._run_create_lock:
            payload = self._build_run_payload(request, recipe, dataset, owner)
            spec = SubmitSpec.model_validate(payload)
            self.audit.record(
                "run.create",
                actor=self._current_actor,
                target=spec.name,
                detail={"recipe": recipe.id, "dataset": request.dataset_id, "datasets": request.resolved_dataset_ids(), "overrides": request.overrides},
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
        """从默认镜像及实时/历史任务生成镜像建议。

        Web Pod 无法查询节点 containerd，因此这里只推荐配置过或实际使用过的镜像。
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
            # 对象被回收后日志不可取，但任务存在过，不应报 404。
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
            # waiting 时调 K8s 日志接口只会得到 400，直接返回容器状态更有用。
            return {
                "name": name,
                "pod_name": pod_name,
                "logs": _waiting_log_message(waiting, _pod_image(pod)),
                "container_waiting": waiting["state"],
                "container_reason": waiting["reason"],
            }
        try:
            raw = self.kubernetes.read_pod_log(pod_name, tail_lines)
        except ServiceError as exc:
            if "400 Bad Request" in str(exc) or pod.get("status", {}).get("phase") == "Pending":
                return {
                    "name": name,
                    "pod_name": pod_name,
                    "logs": "训练容器正在初始化或启动中，暂未产生日志，请稍后刷新。",
                    "container_waiting": "waiting",
                    "container_reason": "PodInitializing",
                }
            raise
        return {
            "name": name,
            "pod_name": pod_name,
            "logs": normalize_log_newlines(raw),
        }

    def job_checkpoints(self, name: str, spec_owner: str | None = None) -> dict[str, Any]:
        """列出某个任务写出的 checkpoint 文件。"""
        del spec_owner  # 归属校验由路由层统一做
        from .checkpoints import list_checkpoints

        result = list_checkpoints(self.settings.checkpoints_mount_path, name)
        return {"name": name, **result}

    def job_timeline(self, name: str) -> dict[str, Any]:
        """作业阶段时间轴：已提交 → 已入队 → 已准入 → 拉镜像 → 启动 → 训练 → 结束。"""
        from .timeline import build_timeline

        job = self.kubernetes.get_job(name)
        if job is None:
            self._require_job_existed(name)
            snap = self.history.get_snapshot(name)
            if snap and snap.get("timeline"):
                return {"name": name, **snap["timeline"]}
            return {"name": name, "current_stage": "finished", "items": []}
        pods = self.kubernetes.list_job_pods(name)
        workload = self._matching_workload(job, self.kubernetes.list_workloads(), pods)
        status = str(self._job_response(job, workload, pods)["status"])
        pod = self._select_pod(pods)
        result = build_timeline(job=job, workload=workload, pod=pod, status=status)
        payload = {"status": status, **result}
        self.history.save_snapshot(name, timeline=payload)
        return {"name": name, **payload}

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
                # 容器未启动时 waiting detail 比笼统事件更能说明根因。
                waiting_reason=waiting["detail"] if waiting else None,
                image=str(job.get("image") or _pod_image(pod) or ""),
            ),
        }

    def _latest_pod(self, name: str) -> dict[str, Any] | None:
        """诊断尽力读取最新 Pod；K8s 临时不可用时退化为无容器状态。"""
        try:
            return self._select_pod(self.kubernetes.list_job_pods(name))
        except Exception:  # noqa: BLE001 - 诊断是尽力而为
            return None

    def job_metrics(self, name: str, limit: int = 500) -> dict[str, Any]:
        """读取训练指标；对象被 TTL 回收后仍认可历史提交记录。"""
        job = self.kubernetes.get_job(name)
        if (
            job is None
            and self.history.get_cancellation(name) is None
            and self.history.find_submission(name) is None
        ):
            raise ServiceError(404, f"job {name} not found")
        from .metrics import read_metrics  # 局部导入，保持模块加载轻量

        return read_metrics(self.settings.checkpoints_mount_path, name, limit=limit)

    def job_tensorboard(self, name: str, limit: int = 2000) -> dict[str, Any]:
        """读取 TensorBoard 曲线；对象被 TTL 回收后仍认可历史提交记录。"""
        from . import tensorboard as tensorboard_service  # 局部导入：tensorboard 是重依赖

        job = self.kubernetes.get_job(name)
        if job is None and self.history.get_cancellation(name) is None and self.history.find_submission(name) is None:
            raise ServiceError(404, f"job {name} not found")
        try:
            return tensorboard_service.read_scalars(self.settings.checkpoints_mount_path, name, limit=limit)
        except tensorboard_service.TensorboardUnavailable as exc:
            raise ServiceError(503, str(exc)) from exc

    def events(self, name: str) -> dict[str, Any]:
        job = self.kubernetes.get_job(name)
        if job is None:
            self._require_job_existed(name)
            snap = self.history.get_snapshot(name)
            if snap and snap.get("events") is not None:
                return {"name": name, "items": snap["events"]}
            return {"name": name, "items": []}
        pod_names = [
            pod.get("metadata", {}).get("name", "")
            for pod in self.kubernetes.list_job_pods(name)
        ]
        raw = self.kubernetes.list_events([name, *pod_names])
        items = [_event_item(event) for event in raw]
        items.sort(key=lambda item: item.get("last_seen") or "", reverse=True)
        if items:
            self.history.save_snapshot(name, events=items)
        return {"name": name, "items": items}

    def gpu_metrics(self) -> dict[str, Any]:
        """从 vla-gpu-metrics DaemonSet 读取实时 GPU 指标。

        K8s 分配量看不到集群外进程，因此利用率和显存来自 nvidia-smi。服务不可用时
        降级为 available=false；短 TTL 和锁避免并发请求拖住单连接指标服务。
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


def _whole_number(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _container_waiting(pod: dict[str, Any] | None) -> dict[str, str] | None:
    """返回容器 waiting 状态；容器未创建时也识别 Pod 级别的 reason。"""
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


# 容器未创建时，K8s 会把拉镜像失败记在 Pod 自己的 status.reason 上。
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
    """把容器 waiting 状态翻译成可操作的日志说明，而不是抛出 K8s 400。"""
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


def normalize_log_newlines(raw: str) -> str:
    """将 tqdm 等进度条产生的 CRLF 和单独 CR 规整为网页可读的换行。"""
    if not raw:
        return raw
    return raw.replace("\r\n", "\n").replace("\r", "\n")
