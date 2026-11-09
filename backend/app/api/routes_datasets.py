"""数据集 API。

数据集的「事实来源」是宿主机 ``PLATFORM_DATASETS_HOST_PATH`` 目录（容器内 ``/datasets``），
SQLite 只缓存扫描结果。所有读接口都先返回缓存，需要时再触发扫描，
避免大目录阻塞请求。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..auth import require_admin, require_user
from ..models import DatasetScanRequest
from ..services.platform import PlatformService

router = APIRouter(prefix="/api/v1")


def get_service(request: Request) -> PlatformService:
    return request.app.state.service


@router.get("/datasets", dependencies=[Depends(require_user)])
def list_datasets(
    refresh: bool = False,
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """列出所有数据集。

    ``refresh=true`` 时先同步扫描变化过的数据集再返回（列表页手动刷新用）。
    默认只返回缓存，由前端在页面加载时调用一次 refresh，避免每次轮询都扫盘。
    """
    return service.list_datasets(refresh=refresh)


@router.get("/datasets/{dataset_id}", dependencies=[Depends(require_user)])
def get_dataset(
    dataset_id: str,
    deep: bool = False,
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """数据集详情。``deep=true`` 触发深扫描（会读 parquet 与视频目录）。"""
    return service.get_dataset(dataset_id, deep=deep)


@router.post("/datasets/scan", dependencies=[Depends(require_admin)])
def scan_datasets(
    payload: DatasetScanRequest,
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """手动触发扫描（仅 admin）。不指定 id 时扫描全部。"""
    return service.scan_datasets(payload.id, deep=payload.deep)