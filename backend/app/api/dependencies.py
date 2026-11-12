from __future__ import annotations

from typing import Annotated, Any

from fastapi import Path, Request

from ..auth import AuthContext, job_access_denied, may_access_job
from ..services.platform import PlatformService

JOB_NAME_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
JobName = Annotated[str, Path(min_length=1, max_length=50, pattern=JOB_NAME_PATTERN)]
# 用户名出现在 URL 路径里；先做格式限制，再交给服务层处理。
USERNAME_PATTERN = r"^[A-Za-z0-9._@-]{1,64}$"
Username = Annotated[str, Path(min_length=1, max_length=64, pattern=USERNAME_PATTERN)]


def get_service(request: Request) -> PlatformService:
    return request.app.state.service


def ensure_visible(user: AuthContext, job: dict[str, Any]) -> None:
    if not may_access_job(user, str(job.get("owner") or "")):
        raise job_access_denied(f"job {job.get('name')} not found")
