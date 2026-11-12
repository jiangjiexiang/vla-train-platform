from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import Response

from .config import Settings
from .services.tensorboard_proxy import PATH_PREFIX as TB_PATH_PREFIX
from .services.tensorboard_proxy import TensorboardProcess, proxy_request

# 值就是现有的会话令牌本身，会话被 revoke 后 Cookie 立刻失效。
TENSORBOARD_COOKIE = "vla_tb_session"


def set_tensorboard_cookie(response: Response, token: str, settings: Settings) -> None:
    """种下 TensorBoard 反代用的 HttpOnly Cookie，仅限 /tensorboard 路径携带。"""
    response.set_cookie(
        key=TENSORBOARD_COOKIE,
        value=token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        path="/tensorboard",
    )


def register_tensorboard(app: FastAPI, settings: Settings) -> None:
    # 一个进程服务所有任务：logdir 指向 checkpoints 根目录，每个任务子目录自动成为 run。
    # 必须用容器内路径，宿主机的 checkpoint_host_path 在 Web 容器里不存在。
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
    async def tensorboard_fonts(rest: str, request: Request) -> Response:
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
    async def tensorboard_proxy(rest: str = "", request: Request = None) -> Response:  # type: ignore[assignment]
        """TensorBoard 反向代理，必须先于 SPA 兜底注册。

        鉴权只能靠 Cookie：iframe 发出的请求不会带 Authorization 头。
        这里明确不接受 Authorization，避免出现两套并存的认证路径。
        """
        service = app.state.service
        current_settings = app.state.settings

        if current_settings.auth_mode != "off":
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
        # 不透传 Accept-Encoding：上游若压缩，urllib 解压后会留下错误的编码和长度头。
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
        # content-length 必须丢掉，由 Response 按实际 payload 重新计算。
        keep = {"content-type", "cache-control", "expires", "etag", "last-modified"}
        passthrough = {k: v for k, v in headers if k.lower() in keep}
        return Response(
            content=payload,
            status_code=status,
            headers=passthrough,
        )
