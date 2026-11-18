from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from ..auth import AuthContext, get_settings, require_user
from ..config import Settings
from ..models import LoginRequest
from ..services import ldap_auth
from ..services.errors import ServiceError
from ..services.platform import PlatformService
from ..tensorboard import set_tensorboard_cookie
from .dependencies import get_service

import threading
import time
from collections import defaultdict

_LOGIN_LOCK = threading.Lock()
_LOGIN_FAILURES: dict[tuple[str, str], list[float]] = defaultdict(list)
_LOGIN_WINDOW_SECONDS = 60.0
_LOGIN_MAX_ATTEMPTS = 5


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if fwd:
        return fwd
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str, username: str) -> None:
    now = time.monotonic()
    key = (ip, username)
    with _LOGIN_LOCK:
        recent = [t for t in _LOGIN_FAILURES.get(key, []) if now - t < _LOGIN_WINDOW_SECONDS]
        _LOGIN_FAILURES[key] = recent
        if len(recent) >= _LOGIN_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed login attempts, please try again later",
            )


def _record_login_failure(ip: str, username: str) -> None:
    now = time.monotonic()
    key = (ip, username)
    with _LOGIN_LOCK:
        recent = [t for t in _LOGIN_FAILURES.get(key, []) if now - t < _LOGIN_WINDOW_SECONDS]
        recent.append(now)
        _LOGIN_FAILURES[key] = recent


def _clear_login_failures(ip: str, username: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILURES.pop((ip, username), None)


router = APIRouter()


@router.post("/auth/login")
def login(
    request: Request,
    request_model: LoginRequest,
    service: PlatformService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """账号密码登录换平台会话令牌。

    支持两种账号来源：
    - ldap：公司目录账号
    - local：平台自己维护的账号（配置在 PLATFORM_LOCAL_USERS 里）

    token/off 模式不支持账号密码登录——token 模式本来就是给机器用的。
    """
    client_ip = _client_ip(request)
    _check_rate_limit(client_ip, request_model.username)
    if settings.auth_mode == "local":
        from ..local_users import LocalUserRegistry, parse_local_users

        user = service.users.verify(request_model.username, request_model.password)
        if user is None:
            registry: LocalUserRegistry = parse_local_users(settings.local_users)
            user = registry.verify(request_model.username, request_model.password)
        if user is None:
            _record_login_failure(client_ip, request_model.username)
            # 与 LDAP 分支统一：不区分「用户不存在」和「密码错误」
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        _clear_login_failures(client_ip, request_model.username)
        session = service.sessions.issue(
            user.username, user.role, settings.session_ttl_hours, source="local", verified=False
        )
        response = JSONResponse(
            {
                "token": session["token"],
                "expires_at": session["expires_at"],
                "user": {"username": user.username, "role": user.role, "source": "local"},
            }
        )
        # iframe 带不了 Authorization 头，只能靠 Cookie 过 TensorBoard 反代的鉴权。
        set_tensorboard_cookie(response, session["token"], settings)
        return response

    if settings.auth_mode != "ldap":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"login is not available in auth mode {settings.auth_mode}",
        )
    try:
        user = ldap_auth.authenticate(request_model.username, request_model.password, settings)
    except ldap_auth.InvalidCredentials as exc:
        # 统一报错，不区分「用户不存在」与「密码错误」。
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except ldap_auth.LDAPUnavailable as exc:
        # 连接失败是服务端问题：503，绝不能吞成 401。
        message = str(exc) or "unknown error"
        if "unavailable" not in message.lower():
            message = f"LDAP service unavailable: {message}"
        raise ServiceError(503, message) from exc
    # authenticate() 刚校验过密码，签发的会话标记为已验证，鉴权时不再重复绑定。
    session = service.sessions.issue(
        user.username, user.role, settings.session_ttl_hours, source="ldap", verified=False
    )
    response = JSONResponse(
        {
            "token": session["token"],
            "expires_at": session["expires_at"],
            "user": {"username": user.username, "role": user.role, "source": "ldap"},
        }
    )
    set_tensorboard_cookie(response, session["token"], settings)
    return response


@router.post("/auth/logout", status_code=204)
def logout(
    request: Request,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> Response:
    """撤销当前令牌。静态令牌（token 模式）没有服务端状态，登出是幂等的无操作。"""
    token = _bearer_token(request)
    if token:
        service.sessions.revoke(token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/me")
def me(
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    usage = service.usage_for(user.username)
    return {
        **user.as_dict(),
        "auth_mode": service.settings.auth_mode,
        # 口径已定：quotas 同时给出上限与实时用量。
        "quotas": service.quota_for(user.username).render(usage),
    }


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()
