"""一键训练（Run）。

用户只选「数据集 + 配方」，平台负责把其余部分拼好：镜像、命令行、环境变量、
任务名、参数默认值。最终仍然走 ``PlatformService.submit``，所以配额、排队、
owner 归属、Kueue 全部复用既有逻辑，不另起一套。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..models import RunRequest
from ..params_templates import BUILTIN_TEMPLATE, ParamsTemplate, parse_template
from ..recipes.loader import Recipe, check_compatibility
from .params_render import build_command

if TYPE_CHECKING:  # 避免与 platform.py 循环导入（platform 反过来要 import 本模块）
    from .platform import PlatformService
from .errors import ServiceError

MAX_NAME_LENGTH = 50


def resolve_template(recipe: Recipe, service: PlatformService) -> ParamsTemplate:
    """取配方生效的参数模板：内联 > 引用已登记的 > 内置通用模板。"""
    if recipe.template is not None:
        try:
            return parse_template(recipe.template)
        except ValueError as exc:
            raise ServiceError(500, f"配方 {recipe.id} 的内联模板有误：{exc}") from exc
    if recipe.params_template:
        try:
            payload = service.get_params_template(recipe.params_template)
        except ServiceError:
            return parse_template(BUILTIN_TEMPLATE)
        source = payload.get("source")
        if source == "builtin":
            # 配方声明的模板还没登记：退回内置，但不报错，保证一键训练可用
            return parse_template(BUILTIN_TEMPLATE)
        try:
            return parse_template(payload["template"])
        except (KeyError, ValueError):
            return parse_template(BUILTIN_TEMPLATE)
    return parse_template(BUILTIN_TEMPLATE)


def merge_values(
    template: ParamsTemplate, defaults: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
    """参数取值优先级：overrides > defaults > 模板里的 default。

    defaults 只在模板声明了该参数时生效，避免配方写错键名时偷偷注入无效参数。
    """
    declared = {param.key for param in template.params}
    unknown = set(overrides) - declared
    if unknown:
        raise ServiceError(422, f"未知的参数：{', '.join(sorted(unknown))}")
    values: dict[str, Any] = {}
    for key, value in defaults.items():
        if key in declared:
            values[key] = value
    values.update(overrides)
    return values


def generated_name(recipe: Recipe, dataset_id: str, existing: set[str]) -> str:
    """生成 <recipe 短名>-<dataset>-<序号>，长度超限时截断并保证唯一。"""
    short = recipe.id.split("-")[0][:12]
    dataset_part = re.sub(r"[^a-z0-9-]+", "-", dataset_id.lower()).strip("-")[:16]
    prefix = "-".join(part for part in (short, dataset_part) if part) or "run"
    prefix = prefix[: MAX_NAME_LENGTH - 4]
    for index in range(1, 1000):
        candidate = f"{prefix}-{index:03d}"
        if candidate not in existing:
            return candidate
    raise ServiceError(409, "自动生成任务名失败，请手动指定 name")


def _label_value(raw: str) -> str:
    """把任意字符串压成合法的 Kubernetes 标签值。

    K8s 标签值只允许 [a-z0-9A-Z._-]，且须字母数字开头结尾、最长 63 字符。
    数据集名常带下划线（如 1028pick_3.0），直接把原名当标签会让整个提交被
    pydantic 校验拦下，因此这里做一次规范化，而不是指望用户改名。
    规范化后为空则丢弃该标签（返回空串，由调用方过滤）。
    """
    slug = re.sub(r"[^a-z0-9.\-]+", "-", raw.lower()).strip("-._")
    slug = re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", slug)
    return slug[:63]


def build_run(
    request: RunRequest,
    *,
    recipe: Recipe,
    dataset: dict[str, Any],
    service: PlatformService,
    owner: str,
    existing_names: set[str],
) -> dict[str, Any]:
    """把一次「一键训练」请求展开成 SubmitSpec 的字典。"""
    # 先判数据集本身能不能用：一个坏数据集报「格式不兼容」是误导，用户会去换配方，
    # 但换什么配方都跑不了。所以可用性检查必须在前。
    status = dataset.get("status")
    if status in ("unusable", "error"):
        errors = [issue["message"] for issue in dataset.get("issues", []) if issue.get("level") == "error"]
        detail = "；".join(errors[:3]) or "数据集不可用"
        raise ServiceError(422, f"数据集 {request.dataset_id} 不可用：{detail}")

    compatibility = check_compatibility(recipe, str(dataset.get("format", "")))
    if compatibility:
        raise ServiceError(422, compatibility)

    name = request.name or generated_name(recipe, request.dataset_id, existing_names)
    if name in existing_names:
        raise ServiceError(409, f"job {name} already exists")

    template = resolve_template(recipe, service)
    values = merge_values(template, recipe.defaults, request.overrides)
    # 基础命令优先级：配方 entrypoint > 参数模板 entrypoint > 平台默认命令。
    # 把配方 entrypoint 塞进模板副本，让 build_command 统一处理，避免两处各写一遍。
    effective = template.model_copy(update={"entrypoint": recipe.entrypoint or template.entrypoint})
    command = build_command(
        effective,
        values,
        user_command="",
        default_command=service.settings.default_command,
    )

    all_ids = request.resolved_dataset_ids()
    env = {
        "DATASET_ROOT": f"/datasets/{all_ids[0]}",
        "DATASET_ROOTS": ",".join(f"/datasets/{d}" for d in all_ids),
    }
    env.update(recipe.env)
    env.update(request.env)

    return {
        "name": name,
        "image": recipe.image,
        "gpus": recipe.gpu_count,
        "queue": service.settings.queue,
        "priority": request.priority,
        "command": command,
        "env": env,
        "host_paths": [item.model_dump() for item in recipe.host_paths],
        "max_retries": 0,
        "termination_grace_period_seconds": 30,
        "owner": owner,
        "labels": {
            key: value
            for key, value in (
                ("recipe", _label_value(recipe.id)),
                ("dataset", _label_value(request.dataset_id)),
            )
            if value
        },
        "nodes": 1,
    }