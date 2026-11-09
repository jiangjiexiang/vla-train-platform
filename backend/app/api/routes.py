from __future__ import annotations

from enum import Enum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status

from ..auth import (
    AuthContext,
    get_settings,
    job_access_denied,
    may_access_job,
    require_admin,
    require_user,
)
from ..config import Settings
from ..models import CloneRequest, LoginRequest, QuotaUpdate, SubmitRequest
from ..services import ldap_auth
from ..services.errors import ServiceError
from ..services.platform import PlatformService


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


JOB_NAME_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
JobName = Annotated[str, Path(min_length=1, max_length=50, pattern=JOB_NAME_PATTERN)]
# 用户名出现在 URL 路径里；先做格式限制，再交给服务层处理。
USERNAME_PATTERN = r"^[A-Za-z0-9._@-]{1,64}$"
Username = Annotated[str, Path(min_length=1, max_length=64, pattern=USERNAME_PATTERN)]

router = APIRouter(prefix="/api/v1")


def get_service(request: Request) -> PlatformService:
    return request.app.state.service


# --- 健康与配置 -----------------------------------------------------------


@router.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict[str, str | bool]:
    # /health 是登录前唯一的免鉴权端点，前端必须靠它判断该显示哪种登录页，
    # 所以除 v0.8 的 status / auth_required 外再补一个 auth_mode（只增不改，旧客户端不受影响）。
    return {
        "status": "ok",
        "auth_required": settings.auth_enabled,
        "auth_mode": settings.auth_mode,
    }


@router.get("/config", dependencies=[Depends(require_user)])
def platform_config(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return {
        "allowed_gpus": list(settings.allowed_gpus),
        "default_image": settings.default_image,
        "default_command": settings.default_command,
        "default_queue": settings.queue,
        "priorities": ["low", "normal", "high"],
        "dataset_path": "/datasets",
        "checkpoint_root": "/checkpoints",
        # v0.9：前端据此决定是否显示多机选项（契约 3.5）。
        "auth_mode": settings.auth_mode,
        "max_nodes": settings.max_nodes,
    }


# --- 登录 / 当前用户 ------------------------------------------------------


@router.post("/auth/login")
def login(
    request_model: LoginRequest,
    service: PlatformService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """LDAP 登录换平台会话令牌；token/off 模式不支持账号密码登录。"""
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
    return {
        "token": session["token"],
        "expires_at": session["expires_at"],
        "user": {"username": user.username, "role": user.role, "source": "ldap"},
    }


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


# --- 用户与配额（仅 admin） ----------------------------------------------


@router.get("/users")
def list_users(
    _admin: AuthContext = Depends(require_admin),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    return service.list_users()


@router.put("/users/{username}/quota")
def put_quota(
    username: Username,
    request_model: QuotaUpdate,
    _admin: AuthContext = Depends(require_admin),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    return service.set_quota(username, request_model.max_gpus, request_model.max_active_jobs)


# --- 任务 ----------------------------------------------------------------


@router.post("/jobs", status_code=201)
def submit_job(
    request_model: SubmitRequest,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    spec = request_model.with_defaults(settings.default_image, settings.default_command)
    return service.submit(spec, owner=user.username)


@router.get("/jobs")
def list_jobs(
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Annotated[JobStatus | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    return service.list_jobs(
        limit=limit,
        offset=offset,
        status=status.value if status is not None else None,
        q=q,
        # admin 看全部；普通用户只看自己提交的（契约 1.5）。
        owner=None if user.is_admin else user.username,
    )


@router.get("/jobs/{name}")
def get_job(
    name: JobName,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    job = service.get_job(name)
    _ensure_visible(user, job)
    return job


@router.delete("/jobs/{name}")
def cancel_job(
    name: JobName,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    _ensure_visible(user, service.get_job(name))
    return service.cancel(name)


@router.post("/jobs/{name}/clone", status_code=201)
def clone_job(
    request_model: CloneRequest,
    name: JobName,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    _ensure_visible(user, service.get_job(name))
    return service.clone(name, request_model.name, owner=user.username)


@router.get("/jobs/{name}/logs")
def job_logs(
    name: JobName,
    tail_lines: Annotated[int | None, Query(ge=1, le=10000)] = None,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _ensure_visible(user, service.get_job(name))
    return service.logs(name, tail_lines or settings.log_tail_default)


@router.get("/jobs/{name}/events")
def job_events(
    name: JobName,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    _ensure_visible(user, service.get_job(name))
    return service.events(name)


@router.get("/cluster/summary", dependencies=[Depends(require_user)])
def cluster_summary(service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    # 集群容量是共享信息，不做按用户过滤；任务计数仍是全集群口径。
    return service.cluster_summary()


@router.get("/queues", dependencies=[Depends(require_user)])
def queues(service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    # 排队情况对所有用户可见，便于判断"还要等多久"。
    return service.queues()


def _ensure_visible(user: AuthContext, job: dict[str, Any]) -> None:
    if not may_access_job(user, str(job.get("owner") or "")):
        raise job_access_denied(f"job {job.get('name')} not found")