"""按用户配额：读取 / 覆盖 / 校验。

口径（契约 1.5）：
- 单用户覆盖存 `user_quotas` 表；没有覆盖时取配置里的默认值；
- GPU 用量 = 该用户所有非终态任务（QUEUED + RUNNING）的 gpus 之和；
  实时任务来自 K3s，历史里只有「取消/删除后」的终态记录，因此两者天然不重复；
- 取消后记录变为终态，立刻释放额度；
- admin 同样受约束，只能通过显式调高自己的配额绕过。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from .errors import ServiceError
from .sessions import to_iso, utc_now

ACTIVE_STATUSES = ("QUEUED", "RUNNING")


@dataclass(frozen=True)
class Quota:
    max_gpus: int | None = None
    max_active_jobs: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        """只有上限，用于 GET /users 里每行的 quota 字段。"""
        return {"max_gpus": self.max_gpus, "max_active_jobs": self.max_active_jobs}

    def render(self, usage: Usage) -> dict[str, int | None]:
        """上限 + 实时用量：/auth/me 与 PUT（quota）的 quotas 对象统一用这个形状。"""
        return {**self.as_dict(), **usage.as_dict()}


@dataclass(frozen=True)
class Usage:
    active_jobs: int = 0
    gpu_in_use: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"active_jobs": self.active_jobs, "gpu_in_use": self.gpu_in_use}


class QuotaStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_quotas (
                    username TEXT PRIMARY KEY,
                    max_gpus INTEGER,
                    max_active_jobs INTEGER,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def get_override(self, username: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT username, max_gpus, max_active_jobs, updated_at FROM user_quotas WHERE username = ?",
                (username,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_overrides(self) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT username, max_gpus, max_active_jobs, updated_at FROM user_quotas"
            ).fetchall()
        return {row["username"]: dict(row) for row in rows}

    def set_override(self, username: str, max_gpus: int | None, max_active_jobs: int | None) -> dict[str, Any]:
        timestamp = to_iso(utc_now())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_quotas(username, max_gpus, max_active_jobs, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    max_gpus = excluded.max_gpus,
                    max_active_jobs = excluded.max_active_jobs,
                    updated_at = excluded.updated_at
                """,
                (username, max_gpus, max_active_jobs, timestamp),
            )
        return {
            "username": username,
            "quota": {"max_gpus": max_gpus, "max_active_jobs": max_active_jobs},
            "updated_at": timestamp,
        }


def effective_quota(settings: Settings, override: dict[str, Any] | None) -> Quota:
    if override is None:
        return Quota(
            max_gpus=settings.default_max_gpus,
            max_active_jobs=settings.default_max_active_jobs,
        )
    return Quota(
        max_gpus=override.get("max_gpus"),
        max_active_jobs=override.get("max_active_jobs"),
    )


def check_quota(quota: Quota, usage: Usage, gpus: int) -> None:
    """提交前校验。detail 文案由契约 1.5 规定，前端/文档依赖该格式。"""
    if quota.max_gpus is not None:
        requested = usage.gpu_in_use + max(0, gpus)
        if requested > quota.max_gpus:
            raise ServiceError(422, f"GPU quota exceeded: {usage.gpu_in_use} used of {quota.max_gpus} allowed")
    if quota.max_active_jobs is not None:
        if usage.active_jobs + 1 > quota.max_active_jobs:
            raise ServiceError(
                422,
                f"Active job quota exceeded: {usage.active_jobs} of {quota.max_active_jobs} allowed",
            )