from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .api.routes_datasets import router as datasets_router
from .config import Settings
from .history import HistoryStore
from .services.errors import ServiceError
from .services.kubernetes import KubernetesClient
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
    def purge_expired_sessions() -> None:
        # 会话持久化在 SQLite 里；重启时顺手清掉过期令牌，避免表无限增长。
        removed = service.sessions.purge_expired()
        if removed:
            logger.info("purged %s expired sessions", removed)

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
