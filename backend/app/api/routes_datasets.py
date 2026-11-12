"""数据集 API。

数据集的「事实来源」是宿主机 ``PLATFORM_DATASETS_HOST_PATH`` 目录（容器内 ``/datasets``），
SQLite 只缓存扫描结果。所有读接口都先返回缓存，需要时再触发扫描，
避免大目录阻塞请求。

写接口（注册 / 重扫）只对 admin 开放，且注册路径必须落在允许的根目录内，
详见 ``services/dataset_register.py``。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import require_user, set_admin_actor
from ..models import DatasetRegisterRequest, DatasetScanRequest
from ..services.dataset_register import UnsafeDatasetPath
from ..services.platform import PlatformService
from .dependencies import get_service

router = APIRouter(prefix="/api/v1")


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


@router.post("/datasets", status_code=status.HTTP_201_CREATED)
def register_dataset(
    payload: DatasetRegisterRequest,
    _admin=Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """注册一个已存在的 LeRobot 数据集目录（仅 admin）。

    文件仍由用户自己放到服务器上（或在允许的根目录里准备好），这里只负责
    「让它出现在平台列表里」。路径校验见 ``services/dataset_register.py``：
    允许列表为空时**只接受**数据集根目录的直接子目录，
    与目录扫描发现的数据集完全等价，不会把任意路径带进平台。
    """
    try:
        meta = service.register_dataset(payload.path, payload.id)
    except UnsafeDatasetPath as exc:
        # ServiceError 由 main.py 的统一异常处理器转成 {"detail": ...} + 对应状态码，
        # 这里只兜住路径类错误（400）。
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return meta.as_dict()


@router.get("/datasets/{dataset_id}", dependencies=[Depends(require_user)])
def get_dataset(
    dataset_id: str,
    deep: bool = False,
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """数据集详情。``deep=true`` 触发深扫描（会读 parquet 与视频目录）。"""
    return service.get_dataset(dataset_id, deep=deep)


@router.post("/datasets/{dataset_id}/scan")
def scan_dataset(
    dataset_id: str,
    deep: bool = False,
    admin=Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """重新扫描单个数据集（仅 admin）。

    按库里记录的实际路径重扫——数据集不一定在扫描根下（见
    ``PLATFORM_DATASET_ALLOWED_ROOTS``）。未注册或目录已被删除返回 404。
    """
    result = service.scan_dataset(dataset_id, deep=deep)
    return {"scanned": 1, "items": [result.as_dict()]}


@router.post("/datasets/scan")
def scan_datasets(
    payload: DatasetScanRequest,
    admin=Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """手动触发扫描（仅 admin）。不指定 id 时扫描全部。

    原先这里用 ``require_admin`` 做鉴权；改成 ``set_admin_actor`` 后行为不变，
    只是顺带把操作者写进审计。未注册或目录已被删除的 id 统一 404。
    """
    return service.scan_datasets(payload.id, deep=payload.deep)


