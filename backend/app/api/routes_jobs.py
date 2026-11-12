from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response

from ..auth import AuthContext, get_settings, require_user, set_actor, set_admin_actor
from ..config import Settings
from ..models import CloneRequest, JobStatus, SubmitRequest
from ..services.platform import PlatformService
from .dependencies import JobName, ensure_visible, get_service

router = APIRouter()


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
    ensure_visible(user, job)
    return job


@router.delete("/jobs/{name}")
def cancel_job(
    name: JobName,
    user: AuthContext = Depends(set_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    ensure_visible(user, service.get_job(name))
    return service.cancel(name)


@router.post("/jobs/{name}/clone", status_code=201)
def clone_job(
    request_model: CloneRequest,
    name: JobName,
    user: AuthContext = Depends(set_actor),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    ensure_visible(user, service.get_job(name))
    return service.clone(name, request_model.name, owner=user.username)


@router.get("/jobs/{name}/logs")
def job_logs(
    name: JobName,
    tail_lines: Annotated[int | None, Query(ge=1, le=10000)] = None,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ensure_visible(user, service.get_job(name))
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
    ensure_visible(user, service.get_job(name))
    return service.job_checkpoints(name)


@router.get("/jobs/{name}/timeline")
def job_timeline(
    name: JobName,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    """作业阶段时间轴，用于看清任务卡在哪一步。"""
    ensure_visible(user, service.get_job(name))
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
    ensure_visible(user, job)
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
    ensure_visible(user, service.get_job(name))
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
    ensure_visible(user, service.get_job(name))
    return service.job_tensorboard(name, limit=limit)


@router.get("/jobs/{name}/events")
def job_events(
    name: JobName,
    user: AuthContext = Depends(require_user),
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    ensure_visible(user, service.get_job(name))
    return service.events(name)
