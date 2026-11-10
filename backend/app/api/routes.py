from __future__ import annotations

from enum import Enum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse

from ..auth import (
    AuthContext,
    get_settings,
    job_access_denied,
    may_access_job,
    require_admin,
    require_user,
    set_actor,
    set_admin_actor,
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
        # 这两个是「容器内路径」，前端拿它拼展示用的产物目录；
        # 宿主机路径不该出现在任何接口里。
        "dataset_path": settings.datasets_mount_path,
        "checkpoint_root": settings.checkpoints_mount_path,
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
    """账号密码登录换平台会话令牌。

    支持两种账号来源：
    - ldap：公司目录账号
    - local：平台自己维护的账号（配置在 PLATFORM_LOCAL_USERS 里）

    token/off 模式不支持账号密码登录——token 模式本来就是给机器用的。
    """
    if settings.auth_mode == "local":
        from ..local_users import LocalUserRegistry, parse_local_users

        registry: LocalUserRegistry = parse_local_users(settings.local_users)
        user = registry.verify(request_model.username, request_model.password)
        if user is None:
            # 与 LDAP 分支统一：不区分「用户不存在」和「密码错误」
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
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
        # 额外种一个 Cookie：TensorBoard 的 iframe 带不了 Authorization 头，
        # 只能靠 Cookie 过反代的鉴权。值就是会话令牌本身，revoke 后立即失效。
        _set_tensorboard_cookie(response, session["token"], settings)
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
    _set_tensorboard_cookie(response, session["token"], settings)
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
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    return service.set_quota(username, request_model.max_gpus, request_model.max_active_jobs)


# --- 任务 ----------------------------------------------------------------


@router.get("/images/{image:path}/params", dependencies=[Depends(require_user)])
def get_image_params(image: str, service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    """镜像的训练参数模板；未登记时回退内置通用模板（source=builtin）。"""
    return service.get_params_template(image)


@router.put("/images/{image:path}/params")
def put_image_params(
    image: str,
    payload: dict[str, Any],
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """登记/更新镜像的参数模板（仅 admin）。"""
    return service.put_params_template(image, payload)


@router.delete("/images/{image:path}/params", status_code=204)
def delete_image_params(
    image: str,
    _admin: AuthContext = Depends(set_admin_actor),
    service: PlatformService = Depends(get_service),
) -> Response:
    """删除登记，之后回退内置模板（仅 admin）。"""
    service.delete_params_template(image)
    # 204 不能带响应体，FastAPI 要求显式返回 Response 而不是 None
    return Response(status_code=204)


@router.get("/images", dependencies=[Depends(require_user)])
def list_images(service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    """镜像候选列表，供提交页下拉选择。默认镜像排在最前。"""
    return {"items": service.list_images()}


@router.post("/jobs", status_code=201)
def submit_job(
    request_model: SubmitRequest,
    user: AuthContext = Depends(set_actor),
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
    user: AuthContext = Depends(set_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    _ensure_visible(user, service.get_job(name))
    return service.cancel(name)


@router.post("/jobs/{name}/clone", status_code=201)
def clone_job(
    request_model: CloneRequest,
    name: JobName,
    user: AuthContext = Depends(set_actor),
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


@router.get("/jobs/{name}/checkpoints")
def job_checkpoints(
    name: JobName,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """训练产出的 checkpoint 文件列表。

    任务可能已从集群清理但产物还在，因此这里只校验任务可见性（历史记录），
    不要求 K8s 对象仍存在。
    """
    _ensure_visible(user, service.get_job(name))
    return service.job_checkpoints(name)


@router.get("/jobs/{name}/timeline")
def job_timeline(
    name: JobName,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """作业阶段时间轴，用于看清任务卡在哪一步。"""
    _ensure_visible(user, service.get_job(name))
    return service.job_timeline(name)


@router.get("/jobs/{name}/diagnosis")
def job_diagnosis(
    name: JobName,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """失败诊断：把事件与日志尾部归一到「发生了什么 + 怎么办」。

    只做只读推断，不改变任务状态。任务没失败时返回 ``diagnosis: null``。
    """
    job = service.get_job(name)
    _ensure_visible(user, job)
    return service.job_diagnosis(name)


@router.get("/jobs/{name}/metrics")
def job_metrics(
    name: JobName,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """训练指标（loss 曲线）。数据来自 /checkpoints/<job>/metrics.jsonl。"""
    # 与 events/logs 一致：普通用户不能看别人任务的训练曲线
    _ensure_visible(user, service.get_job(name))
    return service.job_metrics(name, limit=limit)


@router.get("/jobs/{name}/tensorboard")
def job_tensorboard(
    name: JobName,
    limit: Annotated[int, Query(ge=1, le=100000)] = 2000,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """TensorBoard 曲线。数据来自 /checkpoints/<job>/tensorboard/events.out.tfevents.*。

    不另起常驻的 tensorboard 服务：这里直接解析 events 文件，按需返回 JSON。
    只读取白名单里的 tag；文件不存在时返回 ``path_exists: false`` 而不是 404。
    """
    # 与 metrics / events / logs 一致：普通用户不能看别人任务的训练曲线
    _ensure_visible(user, service.get_job(name))
    return service.job_tensorboard(name, limit=limit)


@router.get("/jobs/{name}/events")
def job_events(
    name: JobName,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    _ensure_visible(user, service.get_job(name))
    return service.events(name)


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
    from pathlib import Path as _Path

    from ..services.backup import list_backups as _list

    items = _list(_Path(settings.backup_dir))
    return {"items": items, "total": len(items), "dir": settings.backup_dir}


@router.post("/maintenance/backups", dependencies=[Depends(require_admin)])
def create_backup(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """立刻做一次备份。

    用 sqlite3 的在线备份 API，不停服务、不需要锁库；
    直接 cp 文件在并发写入时可能拿到半个事务，那样的备份看着有、实际打不开。
    """
    from pathlib import Path as _Path

    from ..services.backup import create_backup as _create
    from ..services.backup import prune_backups as _prune

    result = _create(_Path(settings.db_path), _Path(settings.backup_dir))
    if result.get("ok"):
        _prune(_Path(settings.backup_dir), settings.backup_keep)
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


def _set_tensorboard_cookie(response, token: str, settings) -> None:
    """种下 TensorBoard 反代用的 Cookie。

    iframe 发出的请求不会带 Authorization 头（那是 fetch/XHR 的机制），
    所以 TensorBoard 的鉴权只能走 Cookie。

    HttpOnly：脚本读不到，降低 XSS 窃取风险。
    SameSite=Lax：同站导航会带上，跨站请求不带。
    path=/tensorboard：只有访问 TensorBoard 时才携带，不污染其他请求。
    """
    response.set_cookie(
        key="vla_tb_session",
        value=token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        path="/tensorboard",
    )
