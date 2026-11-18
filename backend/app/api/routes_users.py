from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status

from ..auth import AuthContext, require_admin, set_admin_actor
from ..models import QuotaUpdate, UserCreate, UserUpdate
from ..services.platform import PlatformService
from .dependencies import Username, get_service

router = APIRouter()


@router.get("/users")
def list_users(
    _admin: AuthContext = Depends(require_admin),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    return service.list_users()


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(
    request_model: UserCreate,
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    return service.create_user(
        request_model.username,
        request_model.password,
        request_model.role,
        request_model.max_gpus,
        request_model.max_active_jobs,
    )


@router.patch("/users/{username}")
def update_user(
    username: Username,
    request_model: UserUpdate,
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    return service.update_user(
        username,
        role=request_model.role,
        password=request_model.password,
    )


@router.delete("/users/{username}")
def delete_user(
    username: Username,
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    return service.delete_user(username)


@router.put("/users/{username}/quota")
def put_quota(
    username: Username,
    request_model: QuotaUpdate,
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    return service.set_quota(username, request_model.max_gpus, request_model.max_active_jobs)
