from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..auth import get_settings, require_user
from ..config import Settings

router = APIRouter()


@router.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict[str, str | bool]:
    # /health 是登录前唯一的免鉴权端点，前端必须靠它判断该显示哪种登录页，
    # 所以除 v0.8 的 status / auth_required 外再补一个 auth_mode（只增不改，旧客户端不受影响）。
    return {
        "status": "ok",
        "auth_required": settings.auth_enabled,
        "auth_mode": settings.auth_mode,
    }


@router.get("/config", dependencies=[Depends(require_user)])
def platform_config(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return {
        "allowed_gpus": list(settings.allowed_gpus),
        "default_image": settings.default_image,
        "default_command": settings.default_command,
        "default_queue": settings.queue,
        "priorities": ["low", "normal", "high"],
        # 这两个是「容器内路径」，前端拿它拼展示用的产物目录；
        # 宿主机路径不该出现在任何接口里。
        "dataset_path": settings.datasets_mount_path,
        "checkpoint_root": settings.checkpoints_mount_path,
        # v0.9：前端据此决定是否显示多机选项（契约 3.5）。
        "auth_mode": settings.auth_mode,
        "max_nodes": settings.max_nodes,
    }
