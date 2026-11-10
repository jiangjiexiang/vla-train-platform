from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .api.routes_datasets import router as datasets_router
from .api.routes_runs import router as runs_router
from .config import Settings
from .history import HistoryStore
from .services.errors import ServiceError
from .services.job_watcher import start_job_watcher
from .services.kubernetes import KubernetesClient
from .services.notifier import Notifier
from .services.platform import PlatformService

logger = logging.getLogger("vla-platform")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="高性能分布系统", version="1.0.0")
    app.state.settings = settings
    history = HistoryStore(settings.db_path)
    kubernetes = KubernetesClient(settings)
    service = PlatformService(settings, history, kubernetes)
    app.state.service = service

    @app.on_event("startup")
    def start_auto_backup() -> None:
        """起一个后台线程定期备份数据库。

        只提供手动接口是不够的——没人会每天点一次。
        而数据库里是审计和提交历史，真丢了无法重建。

        interval 为 0 时不起线程（测试或不需要备份的部署）。
        """
        interval = settings.backup_interval_seconds
        if interval <= 0:
            return

        import threading
        from pathlib import Path as _Path

        from .services.backup import create_backup as _create
        from .services.backup import prune_backups as _prune

        stop = threading.Event()
        app.state.backup_stop = stop

        def loop() -> None:
            # 启动后先等一会儿再备第一次，避开启动时的其它初始化 IO。
            # 但等待时长不能超过正常间隔，否则 interval 设得比 300 秒小时，
            # 首次备份会被延后到 300 秒——实测 interval=2 时备份间隔变成了 6 秒，
            # 因为这里 wait(300 的 min) 之后循环内又 wait 了一次。
            first_delay = min(interval, 300)
            if stop.wait(first_delay):
                return
            while True:
                try:
                    result = _create(_Path(settings.db_path), _Path(settings.backup_dir))
                    if result.get("ok"):
                        _prune(_Path(settings.backup_dir), settings.backup_keep)
                    else:
                        print(f"[backup] 备份失败：{result.get('error')}", flush=True)
                except Exception as exc:  # noqa: BLE001 - 备份失败绝不能影响服务
                    print(f"[backup] 备份异常：{exc}", flush=True)
                # 一次备份做完再等下一轮，间隔就是 interval；
                # 之前写成 while wait() + 内部再 wait()，等于等了两遍。
                if stop.wait(interval):
                    return

        thread = threading.Thread(target=loop, daemon=True, name="db-backup")
        thread.start()
        app.state.backup_thread = thread

    @app.on_event("startup")
    def start_job_notifications() -> None:
        """任务跑到终态时推送到钉钉/企微/通用 webhook。

        没配任何 webhook 时这里什么都不做：不起线程、不建表、不碰 K8s，
        与加这个功能之前的启动流程完全一致。
        """
        notifier = Notifier(settings)
        app.state.notifier = notifier
        if not notifier.enabled:
            return
        watcher = start_job_watcher(app, service, notifier, settings)
        if watcher is not None:
            logger.info(
                "job notifications enabled (interval=%ss, channels=%s)",
                settings.notify_interval_seconds,
                ", ".join(
                    name
                    for name, url in (
                        ("dingtalk", notifier.dingtalk_webhook),
                        ("wecom", notifier.wecom_webhook),
                        ("generic", notifier.generic_webhook),
                    )
                    if url
                ),
            )

    @app.on_event("startup")
    def purge_expired_sessions() -> None:
        # 会话持久化在 SQLite 里；重启时顺手清掉过期令牌，避免表无限增长。
        removed = service.sessions.purge_expired()
        if removed:
            logger.info("purged %s expired sessions", removed)

    @app.on_event("shutdown")
    def stop_job_notifications() -> None:
        # 与备份线程一致的停止方式：set 事件，线程在下一轮等待时退出。
        stop = getattr(app.state, "notify_stop", None)
        if stop is not None:
            stop.set()

    @app.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        # Every documented error uses the {"detail": ...} shape, so an unexpected
        # failure must not fall through to uvicorn's plain-text 500 either.
        logger.exception("unhandled error", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    app.include_router(router)
    app.include_router(datasets_router)
    app.include_router(runs_router)

    static_root = Path(settings.static_dir).resolve()
    assets = static_root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    @app.head("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        if full_path:
            candidate = (static_root / full_path).resolve()
            if candidate.is_relative_to(static_root) and candidate.is_file():
                return FileResponse(candidate)
        index = static_root / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="Frontend build not found")

    return app


app = create_app()
