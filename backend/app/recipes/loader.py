"""训练配方（Recipe）。

配方是企业级平台「一键训练」的核心抽象：把「用什么镜像、支持什么数据集格式、
有哪些参数、默认值是什么」打包成一个命名单元。用户只需选数据集 + 选配方。

存储：容器内 ``/app/recipes/*.yaml``，随镜像发布。管理员改配方 = 改文件 + 重新部署，
v1.0 不提供网页端编辑（降低复杂度，也避免误改导致线上训练失败）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

RECIPE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
# backend/app/recipes/loader.py -> backend/app/recipes -> backend/app -> backend -> 仓库根
DEFAULT_RECIPES_DIR = Path(__file__).resolve().parents[3] / "recipes"


class RecipeError(ValueError):
    """配方定义错误。错误信息直接面向配方作者，要具体。"""


@dataclass
class Recipe:
    id: str
    name: str
    description: str
    image: str
    entrypoint: str | None
    # 支持的数据集格式，如 ["lerobot-v2.0", "lerobot-v2.1"]
    dataset_formats: list[str]
    # 引用已登记的参数模板镜像名；为空则用内置通用模板
    params_template: str | None = None
    # 内联参数模板（与 params_template 二选一）
    template: dict[str, Any] | None = None
    # 默认参数取值，覆盖模板里的 default
    defaults: dict[str, Any] = field(default_factory=dict)
    gpu_count: int = 1
    estimated_vram_gb: float | None = None
    # 数据集外还需要的环境变量（如 DATA_CONFIG）
    env: dict[str, str] = field(default_factory=dict)
    # 展示顺序，小的在前
    order: int = 100
    # 是否对普通用户可见（false 只在高级提交页可选）
    public: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "image": self.image,
            "entrypoint": self.entrypoint,
            "dataset_formats": list(self.dataset_formats),
            "params_template": self.params_template,
            "defaults": dict(self.defaults),
            "gpu_count": self.gpu_count,
            "estimated_vram_gb": self.estimated_vram_gb,
            "env": dict(self.env),
            "order": self.order,
            "public": self.public,
            "has_custom_template": self.template is not None,
        }


def parse_recipe(payload: Any, source: str = "<memory>") -> Recipe:
    """校验并构造一个 Recipe。错误信息带文件名，便于定位。"""
    if not isinstance(payload, dict):
        raise RecipeError(f"{source}: 配方必须是 YAML 映射")

    missing = [key for key in ("id", "name", "image") if not payload.get(key)]
    if missing:
        raise RecipeError(f"{source}: 缺少必填字段 {', '.join(missing)}")

    recipe_id = str(payload["id"])
    if not RECIPE_ID_PATTERN.fullmatch(recipe_id):
        raise RecipeError(f"{source}: id 须匹配 {RECIPE_ID_PATTERN.pattern}，收到 {recipe_id!r}")

    formats = payload.get("dataset_formats") or []
    if not isinstance(formats, list) or not formats:
        raise RecipeError(f"{source}: dataset_formats 必须是非空列表，例如 [lerobot-v2.1]")

    gpu_count = payload.get("gpu_count", 1)
    if not isinstance(gpu_count, int) or gpu_count < 1:
        raise RecipeError(f"{source}: gpu_count 必须是正整数")

    defaults = payload.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise RecipeError(f"{source}: defaults 必须是映射")

    env = payload.get("env") or {}
    if not isinstance(env, dict):
        raise RecipeError(f"{source}: env 必须是映射")

    template = payload.get("template")
    params_template = payload.get("params_template")
    if template is not None and params_template is not None:
        raise RecipeError(f"{source}: template 与 params_template 只能二选一")
    if template is not None and not isinstance(template, dict):
        raise RecipeError(f"{source}: template 必须是映射")

    return Recipe(
        id=recipe_id,
        name=str(payload["name"]),
        description=str(payload.get("description") or ""),
        image=str(payload["image"]),
        entrypoint=(str(payload["entrypoint"]) if payload.get("entrypoint") else None),
        dataset_formats=[str(item) for item in formats],
        params_template=(str(params_template) if params_template else None),
        template=template,
        defaults={str(key): value for key, value in defaults.items()},
        gpu_count=gpu_count,
        estimated_vram_gb=(
            float(payload["estimated_vram_gb"]) if payload.get("estimated_vram_gb") is not None else None
        ),
        env={str(key): str(value) for key, value in env.items()},
        order=int(payload.get("order", 100)),
        public=bool(payload.get("public", True)),
    )


class RecipeStore:
    """从目录加载配方。目录不存在时返回空集，不报错（允许不配配方的最小部署）。"""

    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory or os.environ.get("PLATFORM_RECIPES_DIR") or DEFAULT_RECIPES_DIR)

    def load_all(self) -> list[Recipe]:
        recipes: list[Recipe] = []
        if not self.directory.is_dir():
            return recipes
        for path in sorted(self.directory.glob("*.yaml")):
            try:
                payload = yaml.safe_load(path.read_text(encoding="utf-8"))
                recipes.append(parse_recipe(payload, source=path.name))
            except (OSError, yaml.YAMLError, RecipeError) as exc:
                # 单个配方写错不应该让整个平台起不来；启动日志里会看到具体错误
                recipes.append(
                    Recipe(
                        id=path.stem,
                        name=f"{path.stem}（配置错误）",
                        description=f"配方文件解析失败：{exc}",
                        image="",
                        entrypoint=None,
                        dataset_formats=[],
                        public=False,
                        order=9999,
                    )
                )
        recipes.sort(key=lambda item: (item.order, item.id))
        return recipes

    def get(self, recipe_id: str) -> Recipe | None:
        for recipe in self.load_all():
            if recipe.id == recipe_id:
                return recipe
        return None


def check_compatibility(recipe: Recipe, dataset_format: str) -> str | None:
    """返回不兼容原因，兼容时返回 None。"""
    if dataset_format in recipe.dataset_formats:
        return None
    return (
        f"配方 {recipe.name} 不支持数据集格式 {dataset_format}，"
        f"可用格式：{', '.join(recipe.dataset_formats)}"
    )