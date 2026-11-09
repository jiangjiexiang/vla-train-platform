from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import SubmitSpec


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

    @staticmethod
    def _ensure_status_column(connection: sqlite3.Connection) -> None:
        """Older databases predate the status column; add it in place."""
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(cancellations)")}
        if "status" not in columns:
            connection.execute("ALTER TABLE cancellations ADD COLUMN status TEXT")

    def record_submission(self, spec: SubmitSpec, submitted_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO submissions(name, spec_json, submitted_at) VALUES (?, ?, ?)",
                (spec.name, _spec_json(spec), submitted_at),
            )

    def latest_submission(self, name: str) -> tuple[SubmitSpec, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT spec_json, submitted_at FROM submissions WHERE name = ? ORDER BY id DESC LIMIT 1",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return SubmitSpec.model_validate_json(row["spec_json"]), row["submitted_at"]

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
                INSERT INTO cancellations(name, spec_json, created_at, cancelled_at, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    spec_json = excluded.spec_json,
                    created_at = excluded.created_at,
                    cancelled_at = excluded.cancelled_at,
                    status = excluded.status
                """,
                (spec.name, _spec_json(spec), created_at, timestamp, status),
            )
        return self.get_cancellation(spec.name)  # type: ignore[return-value]

    def get_cancellation(self, name: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT name, spec_json, created_at, cancelled_at, status FROM cancellations WHERE name = ?",
                (name,),
            ).fetchone()
        return _cancellation_dict(row) if row is not None else None

    def list_cancellations(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name, spec_json, created_at, cancelled_at, status FROM cancellations"
            ).fetchall()
        return [_cancellation_dict(row) for row in rows]


def _spec_json(spec: SubmitSpec) -> str:
    return spec.model_dump_json(exclude_none=True)


def _cancellation_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": row["name"],
        "spec": SubmitSpec.model_validate_json(row["spec_json"]),
        "created_at": row["created_at"],
        "cancelled_at": row["cancelled_at"],
        "status": row["status"] or "CANCELLED",
    }
