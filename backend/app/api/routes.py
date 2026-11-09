from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request

from ..auth import get_settings, require_token
from ..config import Settings
from ..models import CloneRequest, SubmitRequest
from ..services.platform import PlatformService


JOB_NAME_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
JobName = Annotated[str, Path(min_length=1, max_length=50, pattern=JOB_NAME_PATTERN)]

router = APIRouter(prefix="/api/v1")


def get_service(request: Request) -> PlatformService:
    return request.app.state.service


@router.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict[str, str | bool]:
    return {"status": "ok", "auth_required": settings.auth_required}


@router.get("/config", dependencies=[Depends(require_token)])
def platform_config(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return {
        "allowed_gpus": list(settings.allowed_gpus),
        "default_image": settings.default_image,
        "default_command": settings.default_command,
        "default_queue": settings.queue,
        "priorities": ["low", "normal", "high"],
        "dataset_path": "/datasets",
        "checkpoint_root": "/checkpoints",
    }


@router.post("/jobs", status_code=201, dependencies=[Depends(require_token)])
def submit_job(
    request_model: SubmitRequest,
    service: PlatformService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    spec = request_model.with_defaults(settings.default_image, settings.default_command)
    return service.submit(spec)


@router.get("/jobs", dependencies=[Depends(require_token)])
def list_jobs(service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    return service.list_jobs()


@router.get("/jobs/{name}", dependencies=[Depends(require_token)])
def get_job(name: JobName, service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    return service.get_job(name)


@router.delete("/jobs/{name}", dependencies=[Depends(require_token)])
def cancel_job(name: JobName, service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    return service.cancel(name)


@router.post("/jobs/{name}/clone", status_code=201, dependencies=[Depends(require_token)])
def clone_job(
    request_model: CloneRequest,
    name: JobName,
    service: PlatformService = Depends(get_service),
) -> dict[str, Any]:
    return service.clone(name, request_model.name)


@router.get("/jobs/{name}/logs", dependencies=[Depends(require_token)])
def job_logs(
    name: JobName,
    tail_lines: Annotated[int | None, Query(ge=1, le=10000)] = None,
    service: PlatformService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return service.logs(name, tail_lines or settings.log_tail_default)


@router.get("/jobs/{name}/events", dependencies=[Depends(require_token)])
def job_events(name: JobName, service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    return service.events(name)


@router.get("/cluster/summary", dependencies=[Depends(require_token)])
def cluster_summary(service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    return service.cluster_summary()


@router.get("/queues", dependencies=[Depends(require_token)])
def queues(service: PlatformService = Depends(get_service)) -> dict[str, Any]:
    return service.queues()
