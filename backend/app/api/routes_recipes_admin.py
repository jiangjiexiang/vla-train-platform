"""配方管理 API（仅 admin）：完整 YAML 编辑、恢复默认、历史与回滚。

内置配方随镜像发布且容器内可能只读，管理员的修改以覆盖层形式落在平台
SQLite（recipe_overrides 表），生效顺序是 覆盖 > 内置。普通用户侧的
``/recipes`` 只读接口在 routes_runs.py。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query

from ..auth import AuthContext, require_admin, set_admin_actor
from ..models import RecipeYamlUpdate
from ..recipes.loader import RECIPE_ID_PATTERN
from ..services.platform import PlatformService
from .dependencies import get_service

router = APIRouter(prefix="/api/v1/admin")

# 配方 id 出现在 URL 路径里，格式与 loader 的校验保持一致。
RecipeId = Annotated[str, Path(min_length=1, max_length=48, pattern=RECIPE_ID_PATTERN.pattern)]


@router.get("/recipes", dependencies=[Depends(require_admin)])
def admin_list_recipes(service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    """全部配方（含隐藏的），标注来源（builtin / override / error）与覆盖元信息。"""
    return service.admin_list_recipes()


@router.get("/recipes/{recipe_id}", dependencies=[Depends(require_admin)])
def admin_get_recipe(
    recipe_id: RecipeId,
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """编辑器回填：生效中的原始 YAML（覆盖优先，否则内置文件原文）。"""
    return service.admin_recipe_detail(recipe_id)


@router.put("/recipes/{recipe_id}")
def admin_save_recipe(
    recipe_id: RecipeId,
    payload: RecipeYamlUpdate,
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """保存完整 YAML 覆盖。校验不过返回 422，不落库。"""
    if payload.yaml is not None:
        return service.save_recipe_override(recipe_id, payload.yaml)
    return service.save_recipe_fields(recipe_id, payload.fields or {})


@router.delete("/recipes/{recipe_id}")
def admin_restore_recipe(
    recipe_id: RecipeId,
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """恢复内置默认：删除覆盖层。"""
    return service.restore_recipe_default(recipe_id)


@router.get("/recipes/{recipe_id}/history", dependencies=[Depends(require_admin)])
def admin_recipe_history(
    recipe_id: RecipeId,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """覆盖的变更历史：保存/恢复/回滚，含操作者与时间。"""
    return service.recipe_override_history(recipe_id, limit=limit)


@router.post("/recipes/{recipe_id}/rollback")
def admin_rollback_recipe(
    recipe_id: RecipeId,
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """回滚到上一个版本。没有更早的历史时返回 409。"""
    return service.rollback_recipe(recipe_id)
