from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from ..auth import AuthContext, get_settings, require_admin, set_admin_actor
from ..config import Settings
from ..services.platform import PlatformService
from .dependencies import get_service

router = APIRouter()


@router.post("/maintenance/cleanup/preview", dependencies=[Depends(require_admin)])
def preview_cleanup(
    older_than_days: int = Query(default=7, ge=1, le=3650),
    keep_latest: int = Query(default=20, ge=0, le=10000),
    include_checkpoints: bool = Query(default=False),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """预览清理范围，不执行任何删除。

    清理不可逆，先让管理员看到「会删哪些、保留哪些」再决定。
    """
    return service.plan_cleanup(
        older_than_days=older_than_days,
        keep_latest=keep_latest,
        include_checkpoints=include_checkpoints,
    )


@router.post("/maintenance/cleanup")
def run_cleanup(
    older_than_days: int = Query(default=7, ge=1, le=3650),
    keep_latest: int = Query(default=20, ge=0, le=10000),
    include_checkpoints: bool = Query(default=False),
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """执行清理。只删终态任务，运行中的一律保留。"""
    return service.cleanup_jobs(
        older_than_days=older_than_days,
        keep_latest=keep_latest,
        include_checkpoints=include_checkpoints,
    )


@router.get("/jobs/failures/summary", dependencies=[Depends(require_admin)])
def failure_summary(
    service: PlatformService = Depends(get_service),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """失败原因分布：按诊断码聚合最近失败的任务。

    比看单个任务的诊断更有用的是知道「最近都在挂什么」——
    十次失败里七次同一个原因，通常说明是平台配置问题而不是用户误用。

    仅管理员可见：聚合结果会暴露他人的失败情况。
    """
    return service.failure_summary(limit=limit)


@router.get("/maintenance/checkpoints", dependencies=[Depends(require_admin)])
def checkpoint_usage(service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    """每个任务的产物占用，按大小倒序。

    清理前先看这个，比挨个 du 快得多；也能一眼看出哪些任务吃掉了磁盘。
    """
    return service.checkpoint_usage()


@router.get("/maintenance/backups", dependencies=[Depends(require_admin)])
def list_backups(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """已有备份列表。"""
    from pathlib import Path

    from ..services.backup import list_backups as _list

    items = _list(Path(settings.backup_dir))
    return {"items": items, "total": len(items), "dir": settings.backup_dir}


@router.post("/maintenance/backups", dependencies=[Depends(require_admin)])
def create_backup(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """立刻做一次备份。

    用 sqlite3 的在线备份 API，不停服务、不需要锁库；
    直接 cp 文件在并发写入时可能拿到半个事务，那样的备份看着有、实际打不开。
    """
    from pathlib import Path

    from ..services.backup import create_backup as _create
    from ..services.backup import prune_backups as _prune

    result = _create(Path(settings.db_path), Path(settings.backup_dir))
    if result.get("ok"):
        _prune(Path(settings.backup_dir), settings.backup_keep)
    return result


@router.get("/audit")
def list_audit(
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    action: Annotated[str | None, Query(max_length=64)] = None,
    actor: Annotated[str | None, Query(max_length=64)] = None,
    target: Annotated[str | None, Query(max_length=100)] = None,
    _admin: AuthContext = Depends(require_admin),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """审计日志（仅 admin）：谁在什么时候对什么做了什么。"""
    result = service.audit.list(limit=limit, offset=offset, action=action, actor=actor, target=target)
    result["actions"] = service.audit.actions()
    return result
