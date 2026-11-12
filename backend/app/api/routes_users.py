from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..auth import AuthContext, require_admin, set_admin_actor
from ..models import QuotaUpdate
from ..services.platform import PlatformService
from .dependencies import Username, get_service

router = APIRouter()


@router.get("/users")
def list_users(
    _admin: AuthContext = Depends(require_admin),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    return service.list_users()


@router.put("/users/{username}/quota")
def put_quota(
    username: Username,
    request_model: QuotaUpdate,
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    return service.set_quota(username, request_model.max_gpus, request_model.max_active_jobs)
