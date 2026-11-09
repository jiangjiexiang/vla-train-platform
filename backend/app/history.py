from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import SubmitSpec, spec_from_stored_json


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class HistoryStore:
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
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    submitted_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS submissions_name_id
                    ON submissions(name, id DESC);
                -- 表名沿用历史命名：既存用户取消，也存"删除已完成任务"的记录，
                -- 后者用 status 字段保留真实终态（SUCCEEDED / FAILED）。
                CREATE TABLE IF NOT EXISTS cancellations (
                    name TEXT PRIMARY KEY,
                    spec_json TEXT NOT NULL,
                    created_at TEXT,
                    cancelled_at TEXT NOT NULL,
                    status TEXT
                );
                """
            )
            self._ensure_status_column(connection)
            self._ensure_owner_column(connection)
            self._ensure_submissions_owner_column(connection)

    @staticmethod
    def _ensure_status_column(connection: sqlite3.Connection) -> None:
        """Older databases predate the status column; add it in place."""
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(cancellations)")}
        if "status" not in columns:
            connection.execute("ALTER TABLE cancellations ADD COLUMN status TEXT")

    @staticmethod
    def _ensure_owner_column(connection: sqlite3.Connection) -> None:
        """v0.9 新增 owner 列。老库幂等补列，老行保持 NULL。

        列与 spec_json 里的 owner 重复，是为了能在 SQL 层按提交者过滤，
        不必把所有历史反序列化一遍。NULL = v0.8 老记录（无提交者）。
        """
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(cancellations)")}
        if "owner" not in columns:
            connection.execute("ALTER TABLE cancellations ADD COLUMN owner TEXT")

    @staticmethod
    def _ensure_submissions_owner_column(connection: sqlite3.Connection) -> None:
        """submissions 同样记录 owner，便于按提交者追溯谁提交过这个名字。

        v0.9 之前创建的数据库没有该列，补上；老行保持 NULL。
        """
        if not _has_column(connection, "submissions", "owner"):
            connection.execute("ALTER TABLE submissions ADD COLUMN owner TEXT")

    def record_submission(self, spec: SubmitSpec, submitted_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO submissions(name, spec_json, submitted_at, owner) VALUES (?, ?, ?, ?)",
                (spec.name, _spec_json(spec), submitted_at, spec.owner),
            )

    def latest_submission(self, name: str) -> tuple[SubmitSpec, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT spec_json, submitted_at FROM submissions WHERE name = ? ORDER BY id DESC LIMIT 1",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return spec_from_stored_json(row["spec_json"]), row["submitted_at"]

    def mark_cancelled(
        self,
        spec: SubmitSpec,
        created_at: str | None,
        cancelled_at: str | None = None,
        status: str = "CANCELLED",
    ) -> dict[str, Any]:
        timestamp = cancelled_at or utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cancellations(name, spec_json, created_at, cancelled_at, status, owner)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    spec_json = excluded.spec_json,
                    created_at = excluded.created_at,
                    cancelled_at = excluded.cancelled_at,
                    status = excluded.status,
                    owner = excluded.owner
                """,
                (spec.name, _spec_json(spec), created_at, timestamp, status, spec.owner),
            )
        return self.get_cancellation(spec.name)  # type: ignore[return-value]

    def get_cancellation(self, name: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT name, spec_json, created_at, cancelled_at, status, owner FROM cancellations WHERE name = ?",
                (name,),
            ).fetchone()
        return _cancellation_dict(row) if row is not None else None

    def list_cancellations(self, owner: str | None = None) -> list[dict[str, Any]]:
        """列出终态记录；传 owner 时只返回该用户名下的（admin 不传）。"""
        with self._connect() as connection:
            if owner is None:
                rows = connection.execute(
                    "SELECT name, spec_json, created_at, cancelled_at, status, owner FROM cancellations"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT name, spec_json, created_at, cancelled_at, status, owner "
                    "FROM cancellations WHERE owner = ?",
                    (owner,),
                ).fetchall()
        return [_cancellation_dict(row) for row in rows]

    def owners_by_name(self) -> dict[str, str]:
        """任务名 → 提交者，供普通用户过滤实时任务（老任务不在字典里）。"""
        owners: dict[str, str] = {}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name, owner FROM submissions WHERE owner IS NOT NULL AND owner != '' "
                "ORDER BY id"
            ).fetchall()
        for row in rows:
            owners[row["name"]] = row["owner"]
        return owners


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return column in {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _spec_json(spec: SubmitSpec) -> str:
    return spec.model_dump_json(exclude_none=True)


def _cancellation_dict(row: sqlite3.Row) -> dict[str, Any]:
    spec = spec_from_stored_json(row["spec_json"])
    owner = row["owner"] if "owner" in row.keys() and row["owner"] is not None else spec.owner
    if not spec.owner and owner:
        # 列比 spec_json 新：以列为准，保证列表过滤与响应里的 owner 一致。
        spec = spec.model_copy(update={"owner": owner})
    return {
        "name": row["name"],
        "spec": spec,
        "owner": spec.owner,
        "created_at": row["created_at"],
        "cancelled_at": row["cancelled_at"],
        "status": row["status"] or "CANCELLED",
    }