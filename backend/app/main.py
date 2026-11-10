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
from .services.tensorboard_proxy import PATH_PREFIX as TB_PATH_PREFIX
from .services.tensorboard_proxy import TensorboardProcess, proxy_request

logger = logging.getLogger("vla-platform")

# TensorBoard 反代用 Cookie 鉴权。值就是现有的会话令牌本身——
# 不引入新的签名密钥，会话被 revoke 后 Cookie 立刻失效。
# 前端登录成功后把它种上，iframe 会自动携带。
TENSORBOARD_COOKIE = "vla_tb_session"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="训练平台", version="1.0.0")
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

    # --- TensorBoard 原生界面 -------------------------------------------
    # 起一个 tensorboard 子进程，用反代暴露到 /tensorboard/。
    # 一个进程服务所有任务：--logdir 指向 checkpoints 根目录，
    # 每个任务子目录自动成为一个 run。
    # 必须用容器内路径：tensorboard 是 Web 容器里的子进程，
    # checkpoint_host_path（/home/jjx/...）在容器里根本不存在。
    tensorboard = TensorboardProcess(
        logdir=settings.checkpoints_mount_path,
        port=settings.tensorboard_port,
    )
    app.state.tensorboard = tensorboard

    if settings.tensorboard_enabled:

        @app.on_event("startup")
        def start_tensorboard() -> None:
            # 失败不抛异常：这是附加功能，挂掉不该让平台起不来。
            tensorboard.start()

        @app.on_event("shutdown")
        def stop_tensorboard() -> None:
            tensorboard.stop()

    @app.api_route(
        TB_PATH_PREFIX,
        methods=["GET", "POST", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    # TensorBoard 的静态资源里有一部分走绝对路径 /font-roboto/*，
    # 不在 /tensorboard/ 前缀下，会被 SPA 兜底返回 index.html，
    # 浏览器解析字体时就会报 invalid sfntVersion。单独转一道。
    @app.api_route("/font-roboto/{rest:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def tensorboard_fonts(rest: str, request: Request):
        from fastapi.responses import Response

        current_settings = app.state.settings
        if current_settings.auth_mode != "off":
            if not request.cookies.get(TENSORBOARD_COOKIE):
                return Response(status_code=401)
        status, headers, payload = proxy_request(
            base_url=app.state.tensorboard.base_url,
            path="font-roboto/" + rest,
            method=request.method,
            headers={k: v for k, v in request.headers.items() if k.lower() != "accept-encoding"},
            body=None,
        )
        keep = {"content-type", "cache-control"}
        return Response(
            content=payload,
            status_code=status,
            headers={k: v for k, v in headers if k.lower() in keep},
        )

    @app.api_route(
        TB_PATH_PREFIX + "/{rest:path}",
        methods=["GET", "POST", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    async def tensorboard_proxy(rest: str = "", request: Request = None):  # type: ignore[assignment]
        """TensorBoard 反向代理。

        **必须注册在 spa_fallback 之前**：SPA 兜底会把任何未匹配路径都返回
        index.html 并带上 200，那样未登录访问 /tensorboard/ 看起来是成功的，
        实际拿到的是平台自己的首页——既骗用户也骗测试。

        鉴权只能靠 Cookie：iframe 发出的请求不会带 Authorization 头。
        这里明确不接受 Authorization，避免出现两套并存的认证路径。
        """
        from fastapi.responses import Response

        service = app.state.service
        current_settings = app.state.settings

        if current_settings.auth_mode == "off":
            pass  # 免登录部署，不校验
        else:
            cookie = request.cookies.get(TENSORBOARD_COOKIE)
            if not cookie:
                return Response(
                    content="需要登录后访问 TensorBoard",
                    status_code=401,
                    media_type="text/plain; charset=utf-8",
                )
            session = service.sessions.lookup(cookie)
            if session is None:
                return Response(
                    content="登录已失效，请重新登录",
                    status_code=401,
                    media_type="text/plain; charset=utf-8",
                )

        body = await request.body()
        # 关键：不要把客户端的 Accept-Encoding 传下去。
        # TensorBoard 看到 gzip 会压缩响应，urllib 又自动解压，
        # 结果是我们拿到的是解压后的内容、却顶着 gzip 的头往下发，
        # 顺便 content-length 也对不上——浏览器拿到截断的 JS，
        # 报 "Invalid or unexpected token"，页面一片空白。
        # 我们自己不做压缩，让器自己协商就好，这里统一要求明文。
        forward_headers = {
            k: v for k, v in request.headers.items() if k.lower() != "accept-encoding"
        }
        status, headers, payload = proxy_request(
            base_url=app.state.tensorboard.base_url,
            path=rest,
            method=request.method,
            headers=forward_headers,
            body=body if body else None,
        )
        # 只保留安全的白名单头。
        # content-length 必须丢掉：Response 会按实际 payload 重新算，
        # 照抄上游的值只会写出错误的长度。
        keep = {"content-type", "cache-control", "expires", "etag", "last-modified"}
        passthrough = {k: v for k, v in headers if k.lower() in keep}
        return Response(
            content=payload,
            status_code=status,
            headers=passthrough,
        )

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
