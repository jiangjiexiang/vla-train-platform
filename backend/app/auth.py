"""鉴权：三种模式（off / token / ldap）与角色解析。

v0.9 之前的 `require_token` 只区分「有令牌/没令牌」，现在改为解析出一个
`AuthContext`（用户 + 角色），再由 `require_user` / `require_admin` 做授权。
401 的 JSON 形状（`{"detail": "Bearer token required"}` 等）保持不变，
以免前端和 CLI 依赖的错误处理失效。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import ROLE_ADMIN, ROLE_USER, Settings
from .services.sessions import SessionStore

bearer = HTTPBearer(auto_error=False)

ANONYMOUS_USERNAME = "anonymous"
TOKEN_USERNAME = "token"


@dataclass(frozen=True)
class AuthContext:
    """当前请求的身份。`role` 只有 admin / user 两种。"""

    username: str
    role: str
    source: str

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def as_dict(self) -> dict[str, str]:
        return {"username": self.username, "role": self.role, "source": self.source}


def anonymous_context() -> AuthContext:
    return AuthContext(username=ANONYMOUS_USERNAME, role=ROLE_ADMIN, source="off")


def token_context() -> AuthContext:
    return AuthContext(username=TOKEN_USERNAME, role=ROLE_ADMIN, source="token")


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_sessions(request: Request) -> SessionStore:
    return request.app.state.service.sessions


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def resolve_context(
    credentials: HTTPAuthorizationCredentials | None,
    settings: Settings,
    sessions: SessionStore,
) -> AuthContext:
    """按契约 1.3 的顺序解析：先查会话表，再回落到静态令牌。"""
    if not settings.auth_enabled:
        return anonymous_context()

    token = ""
    if credentials is not None and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    if not token:
        raise _unauthorized("Bearer token required")

    session = sessions.lookup(token)
    if session is not None:
        return _context_for_session(session, settings, sessions, token)

    if settings.auth_mode == "token" and settings.api_token:
        if hmac.compare_digest(token.encode("utf-8"), settings.api_token.encode("utf-8")):
            return token_context()

    raise _unauthorized("Invalid bearer token")


def _context_for_session(
    session: dict[str, Any],
    settings: Settings,
    sessions: SessionStore,
    token: str,
) -> AuthContext:
    """把会话换成当前身份。

    LDAP 账号被禁用/删除后平台侧无法收到通知，因此对"曾经成功绑定"的会话
    每个请求都复查一次 LDAP：角色变化即时生效，账号消失立即 401。
    复查时 LDAP 不可用不会把已有会话打成 401，保留会话、按已存角色放行，
    否则 LDAP 抖动会让所有人掉线；只有明确的"凭据无效"才结束会话。
    """
    from .services import ldap_auth

    username = session["username"]
    role = session["role"]
    if settings.auth_mode == "ldap" and settings.ldap_url:
        try:
            user = ldap_auth.verify(username, settings)
        except ldap_auth.InvalidCredentials as exc:
            # 账号已不存在或被禁用：撤销会话，令牌立即失效。
            sessions.revoke(token)
            raise _unauthorized("Invalid bearer token") from exc
        except ldap_auth.LDAPUnavailable:
            # 服务端故障：不清会话、不 401，保持可用性。
            return AuthContext(username=username, role=role, source="ldap")
        else:
            role = user.role
    return AuthContext(username=username, role=role, source="ldap")


def require_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
    sessions: SessionStore = Depends(get_sessions),
) -> AuthContext:
    return resolve_context(credentials, settings, sessions)


def require_token(user: AuthContext = Depends(require_user)) -> AuthContext:
    """v0.8 名称的兼容别名：语义从「校验令牌」变为「解析用户」。"""
    return user


def require_admin(user: AuthContext = Depends(require_user)) -> AuthContext:
    if not user.is_admin:
        # 文案与文档分区对齐：admin privileges required。
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin privileges required")
    return user


def may_access_job(user: AuthContext, owner: str) -> bool:
    """admin 可访问全部任务；user 只能访问自己的。

    owner 为空串代表 v0.8 老任务（没有提交者信息）。这些任务对 user 不可见，
    只有 admin 能看/能取消，避免把历史任务误判成别人的。管理员模式（off/token）
    下所有人都是 admin，因此老任务在 v0.8 行为下依然完全可见。
    """
    if user.is_admin:
        return True
    return bool(owner) and owner == user.username


def job_access_denied(detail: str) -> HTTPException:
    # 404 而不是 403：不向普通用户泄露别人任务名是否存在。
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def session_response_ttl(settings: Settings) -> int:
    return settings.session_ttl_hours


def user_record(
    username: str,
    role: str,
    source: str,
    quotas: dict[str, Any],
    usage: dict[str, Any],
    last_seen_at: datetime | None,
) -> dict[str, Any]:
    """组装 GET /users 与 GET /auth/me 共用的用户视图。"""
    if not role:
        role = ROLE_USER
    return {
        "username": username,
        "role": role,
        "source": source,
        "last_seen_at": format_time(last_seen_at),
        "active_jobs": usage.get("active_jobs", 0),
        "gpu_in_use": usage.get("gpu_in_use", 0),
        "quota": quotas,
    }


def format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(tz=None).isoformat(timespec="seconds").replace("+00:00", "Z")


def set_actor(
    request: Request,
    user: AuthContext = Depends(require_user),
) -> AuthContext:
    """把当前用户写进 PlatformService，供审计日志记「谁做的」。

    做成依赖而不是在每个路由里手动赋值：漏一个路由就会少一条审计，
    而审计的价值恰恰在于完整。

    注意：本依赖只做**身份解析**，不校验 admin。需要 admin 的路由必须用
    ``set_admin_actor``，否则会把管理员权限放开——这是曾经踩过的坑。
    """
    service = getattr(request.app.state, "service", None)
    if service is not None:
        service.current_actor = user.username
    return user


def set_admin_actor(
    request: Request,
    admin: AuthContext = Depends(require_admin),
) -> AuthContext:
    """admin 专用：先校验管理员身份，再记录操作者。"""
    service = getattr(request.app.state, "service", None)
    if service is not None:
        service.current_actor = admin.username
    return admin
