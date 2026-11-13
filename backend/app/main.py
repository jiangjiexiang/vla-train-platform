from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api.routes import router
from .api.routes_datasets import router as datasets_router
from .api.routes_recipes_admin import router as recipes_admin_router
from .api.routes_runs import router as runs_router
from .config import Settings
from .history import HistoryStore
from .lifecycle import register_lifecycle
from .services.errors import ServiceError
from .services.kubernetes import KubernetesClient
from .services.platform import PlatformService
from .spa import register_spa
from .tensorboard import TENSORBOARD_COOKIE as TENSORBOARD_COOKIE
from .tensorboard import register_tensorboard

logger = logging.getLogger("vla-platform")


_VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"


def _read_version() -> str:
    try:
        return _VERSION_FILE.read_text().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="训练平台", version=_read_version())
    app.state.settings = settings
    history = HistoryStore(settings.db_path)
    kubernetes = KubernetesClient(settings)
    service = PlatformService(settings, history, kubernetes)
    app.state.service = service

    register_lifecycle(app, service, settings)

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
    app.include_router(recipes_admin_router)
    register_tensorboard(app, settings)
    # SPA 兜底必须最后注册，避免吞掉 API 与 TensorBoard 路径。
    register_spa(app, settings)
    return app


# 模块级实例供 `uvicorn app.main:app` 使用。
# 但它会在导入时就读环境变量——测试里 import 这个模块会连带执行 create_app()，
# 没配 PLATFORM_API_TOKEN 就直接炸，整个测试文件收集失败。
# 改成惰性构造：真正需要时（ASGI 服务器访问 app 对象）才创建。
_app: FastAPI | None = None


def __getattr__(name: str):
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
