from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..auth import require_user
from ..services.platform import PlatformService
from .dependencies import get_service

router = APIRouter()


@router.get("/cluster/summary", dependencies=[Depends(require_user)])
def cluster_summary(service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    # 集群容量是共享信息，不做按用户过滤；任务计数仍是全集群口径。
    return service.cluster_summary()


@router.get("/queues", dependencies=[Depends(require_user)])
def queues(service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    # 排队情况对所有用户可见，便于判断"还要等多久"。
    return service.queues()
