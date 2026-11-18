"""机器人配置 API：用户端读取列表，管理员端增删改。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, status

from ..auth import AuthContext, require_admin, require_user, set_admin_actor
from ..models import RobotCreate, RobotUpdate
from ..services.platform import PlatformService
from .dependencies import get_service

router = APIRouter()
RobotId = Annotated[str, Path(min_length=1, max_length=48, pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")]


@router.get("/robots", dependencies=[Depends(require_user)])
def list_robots(service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    return {"items": service.robots.list_all()}


@router.post("/admin/robots", status_code=status.HTTP_201_CREATED)
def create_robot(
    payload: RobotCreate,
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    created = service.robots.create(payload.model_dump())
    service.audit.record("robot.create", actor=_admin.username, target=created["id"], detail={})
    return created


@router.put("/admin/robots/{robot_id}")
def update_robot(
    robot_id: RobotId,
    payload: RobotUpdate,
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    updated = service.robots.update(robot_id, payload.model_dump(exclude_unset=True))
    service.audit.record("robot.update", actor=_admin.username, target=robot_id, detail={})
    return updated


@router.delete("/admin/robots/{robot_id}")
def delete_robot(
    robot_id: RobotId,
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    res = service.robots.delete(robot_id)
    service.audit.record("robot.delete", actor=_admin.username, target=robot_id, detail={})
    return res
