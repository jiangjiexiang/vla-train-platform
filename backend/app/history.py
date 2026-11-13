from __future__ import annotations

import json
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
                -- v0.9.1：镜像的训练参数模板。平台无法直接读 containerd，
                -- 由管理员通过 PUT /images/{image}/params 登记，这里负责持久化。
                CREATE TABLE IF NOT EXISTS image_params (
                    image TEXT PRIMARY KEY,
                    template_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                -- v1.1：管理员在页面上编辑的完整配方 YAML。内置配方随镜像只读，
                -- 覆盖层落库才能跨重启生效；同 id 覆盖内置，删除覆盖即恢复默认。
                CREATE TABLE IF NOT EXISTS recipe_overrides (
                    recipe_id TEXT PRIMARY KEY,
                    yaml TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL
                );
                -- 每次保存/恢复/回滚追加一行，yaml 存全量内容（配方只有几 KB），
                -- 这样任何历史版本都能直接回滚，不用做 diff 回放。
                CREATE TABLE IF NOT EXISTS recipe_override_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipe_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    yaml TEXT,
                    changed_at TEXT NOT NULL,
                    changed_by TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS recipe_override_history_recipe
                    ON recipe_override_history(recipe_id, id DESC);
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

    def get_image_params(self, image: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT image, template_json, updated_at FROM image_params WHERE image = ?",
                (image,),
            ).fetchone()
        if row is None:
            return None
        return {
            "image": row["image"],
            "template": json.loads(row["template_json"]),
            "updated_at": row["updated_at"],
        }

    def put_image_params(self, image: str, template_json: str, updated_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO image_params(image, template_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(image) DO UPDATE SET
                    template_json = excluded.template_json,
                    updated_at = excluded.updated_at
                """,
                (image, template_json, updated_at),
            )

    def forget_job(self, name: str) -> bool:
        """从历史库里彻底删除一个任务的两类记录。

        清理历史任务时用。只删数据库记录，不动 K8s 对象也不动 checkpoint——
        那两件事由 PlatformService 编排，因为它们有各自的失败可能，
        需要分别报告结果。
        """
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM submissions WHERE name = ?", (name,))
            conn.execute("DELETE FROM cancellations WHERE name = ?", (name,))
            conn.commit()
            return cursor.rowcount > 0

    def delete_image_params(self, image: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM image_params WHERE image = ?", (image,))
        return cursor.rowcount > 0

    def list_image_params(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT image, template_json, updated_at FROM image_params ORDER BY image"
            ).fetchall()
        return [
            {"image": row["image"], "template": json.loads(row["template_json"]), "updated_at": row["updated_at"]}
            for row in rows
        ]

    # --- 配方覆盖（v1.1） ---------------------------------------------------
    def get_recipe_override(self, recipe_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT recipe_id, yaml, updated_at, updated_by FROM recipe_overrides WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "recipe_id": row["recipe_id"],
            "yaml": row["yaml"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }

    def list_recipe_overrides(self) -> dict[str, dict[str, Any]]:
        """recipe_id → 覆盖记录（yaml + 更新时间/操作者），供列表展示与覆盖层合并。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT recipe_id, yaml, updated_at, updated_by FROM recipe_overrides"
            ).fetchall()
        return {
            row["recipe_id"]: {
                "yaml": row["yaml"],
                "updated_at": row["updated_at"],
                "updated_by": row["updated_by"],
            }
            for row in rows
        }

    def put_recipe_override(
        self, recipe_id: str, yaml_text: str, updated_at: str, updated_by: str, action: str = "save"
    ) -> None:
        """保存覆盖并追加历史。覆盖表与历史表必须在同一事务里，
        否则崩溃后会留下「生效了但查不到来历」的配置。"""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO recipe_overrides(recipe_id, yaml, updated_at, updated_by) VALUES (?, ?, ?, ?)
                ON CONFLICT(recipe_id) DO UPDATE SET
                    yaml = excluded.yaml,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (recipe_id, yaml_text, updated_at, updated_by),
            )
            connection.execute(
                "INSERT INTO recipe_override_history(recipe_id, action, yaml, changed_at, changed_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (recipe_id, action, yaml_text, updated_at, updated_by),
            )

    def delete_recipe_override(
        self, recipe_id: str, changed_at: str, changed_by: str, action: str = "restore"
    ) -> bool:
        """删除覆盖（恢复内置默认），同样追加历史。返回是否真的有覆盖被删。"""
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM recipe_overrides WHERE recipe_id = ?", (recipe_id,))
            connection.execute(
                "INSERT INTO recipe_override_history(recipe_id, action, yaml, changed_at, changed_by) "
                "VALUES (?, ?, NULL, ?, ?)",
                (recipe_id, action, changed_at, changed_by),
            )
        return cursor.rowcount > 0

    def list_recipe_override_history(self, recipe_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, action, yaml, changed_at, changed_by FROM recipe_override_history "
                "WHERE recipe_id = ? ORDER BY id DESC LIMIT ?",
                (recipe_id, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "action": row["action"],
                "yaml": row["yaml"],
                "changed_at": row["changed_at"],
                "changed_by": row["changed_by"],
            }
            for row in rows
        ]

    def find_submission(self, name: str) -> tuple[SubmitSpec, str] | None:
        """按任务名取最近一次提交记录。

        用于 K8s 对象已经消失、但用户还想知道「这个任务当初提交了什么」的场景：
        ttlSecondsAfterFinished 到期回收、节点重启、或有人手工删除都会让对象消失，
        而 history 里的提交记录还在。不给用户一个 404 就完事，
        否则任务凭空不见、没有任何解释，排障无从下手。
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT spec_json, submitted_at FROM submissions WHERE name = ? ORDER BY id DESC LIMIT 1",
                (name,),
            ).fetchone()
        if row is None:
            return None
        try:
            import json as _json

            spec = SubmitSpec(**_json.loads(row["spec_json"]))
        except Exception:  # noqa: BLE001 - 坏记录当作不存在，不要让整页报错
            return None
        return spec, row["submitted_at"]

    def iter_submissions(self) -> list[tuple[SubmitSpec, str]]:
        """Every submission record, newest first, de-duplicated by job name.

        Used to suggest image names the operator has used before. Old rows whose
        stored spec predates a field still parse because ``SubmitSpec`` fills
        defaults, and a corrupted row is skipped rather than breaking the list.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name, spec_json, submitted_at FROM submissions ORDER BY id DESC"
            ).fetchall()
        seen_names: set[str] = set()
        result: list[tuple[SubmitSpec, str]] = []
        for row in rows:
            if row["name"] in seen_names:
                continue
            seen_names.add(row["name"])
            try:
                result.append((SubmitSpec.model_validate_json(row["spec_json"]), row["submitted_at"]))
            except Exception:  # noqa: BLE001 - a bad row must not break the listing
                continue
        return result

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