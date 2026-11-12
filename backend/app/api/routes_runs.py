"""训练配方与一键训练 API。

配方是只读的（来自镜像内 ``/app/recipes/*.yaml``），这里提供浏览；
``POST /runs`` 是「选数据集 + 选配方 → 加入队列」的入口。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..auth import AuthContext, require_user, set_actor
from ..models import RunRequest
from ..services.platform import PlatformService
from .dependencies import get_service

router = APIRouter(prefix="/api/v1")


def _recipe_summary(recipe: Any, service: PlatformService) -> dict[str, Any]:
    """配方列表项：附上展开后的参数（前端据此渲染表单，不必再请求一次）。"""
    item = recipe.as_dict()
    if recipe.public and recipe.image:
        template = service.recipe_template(recipe)
        item["params"] = [param.model_dump(exclude_none=True) for param in template.params]
        item["extra_args"] = (
            template.extra_args.model_dump(exclude_none=True) if template.extra_args else None
        )
    else:
        item["params"] = []
        item["extra_args"] = None
    return item


@router.get("/recipes", dependencies=[Depends(require_user)])
def list_recipes(
    include_hidden: bool = False,
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """列出训练配方。默认只返回 public 的，高级提交页可要 include_hidden。"""
    recipes = [recipe for recipe in service.list_recipes() if include_hidden or recipe.public]
    return {"items": [_recipe_summary(recipe, service) for recipe in recipes]}


@router.get("/recipes/{recipe_id}", dependencies=[Depends(require_user)])
def get_recipe(recipe_id: str, service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    recipe = service.get_recipe(recipe_id)
    return _recipe_summary(recipe, service)


@router.post("/runs/estimate")
def estimate_run(
    payload: dict[str, Any],
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """预估训练时长。填完参数就能知道要等多久，不用提交后干猜。"""
    return service.estimate_run(RunRequest(**payload))


@router.post("/runs/preflight")
def preflight_run(
    payload: RunRequest,
    _user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """提交前预检：返回能否提交、有哪些警告，不创建任务。

    前端点「加入队列」前先调这个，把问题展示出来，而不是让任务进队列后失败。
    """
    return service.preflight_run(payload)


@router.post("/runs", status_code=201)
def create_run(
    payload: RunRequest,
    user: AuthContext = Depends(set_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """一键开始训练：选数据集 + 选配方即可，其余由平台填充。"""
    return service.create_run(payload, owner=user.username)