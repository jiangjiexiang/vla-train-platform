"""审计日志。

企业内部平台必须能回答「谁在什么时候对什么做了什么」。训练平台的典型场景：

- 一个任务突然被取消，是谁取消的？
- 某个镜像的参数模板被改过，改之前是什么？
- 某个用户的配额被调低，什么时候调的？

本模块只记**变更类**操作（提交、取消、克隆、改配额、改模板、删模板），
查询类操作不记，否则表会被轮询接口淹没。

设计原则：
- 写入必须**永不阻塞业务**：审计失败只打日志，不能让提交任务失败；
- 只存必要信息，参数里的敏感值（如密码）不进审计；
- 每次写入带时间戳与操作者，操作者缺失时记为 ``anonymous``。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("vla-platform.audit")

# 审计动作白名单：只记这些，避免表无限膨胀
ACTIONS = {
    "job.submit",
    "job.cancel",
    "job.clone",
    "run.create",
    "jobs.cleanup",
    "dataset.scan",
    "user.quota.update",
    "image.params.put",
    "image.params.delete",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class AuditStore:
    """审计日志表，与历史库共用同一个 SQLite 文件。"""

    def __init__(self, db_path: str):
        self.path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT,
                    detail_json TEXT
                );
                CREATE INDEX IF NOT EXISTS audit_log_at ON audit_log(at DESC);
                CREATE INDEX IF NOT EXISTS audit_log_action ON audit_log(action, at DESC);
                """
            )

    def record(
        self,
        action: str,
        *,
        actor: str,
        target: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """写一条审计记录。任何异常都只记日志，不向上抛。"""
        if action not in ACTIONS:
            logger.warning("audit: unknown action %s, ignored", action)
            return
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO audit_log(at, actor, action, target, detail_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        utc_now(),
                        actor or "anonymous",
                        action,
                        target,
                        json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None,
                    ),
                )
        except Exception:  # noqa: BLE001 - 审计失败绝不能影响业务
            logger.exception("audit: failed to record %s on %s", action, target)

    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        action: str | None = None,
        actor: str | None = None,
        target: str | None = None,
    ) -> dict[str, Any]:
        """按时间倒序查询。条件都可组合。"""
        clauses: list[str] = []
        params: list[Any] = []
        if action:
            clauses.append("action = ?")
            params.append(action)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if target:
            clauses.append("target = ?")
            params.append(target)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS n FROM audit_log {where}", params  # noqa: S608 - 条件由白名单字段拼出
            ).fetchone()["n"]
            rows = connection.execute(
                f"SELECT id, at, actor, action, target, detail_json FROM audit_log {where}"  # noqa: S608
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, max(1, min(limit, 500)), max(0, offset)],
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json")) if item.get("detail_json") else None
            items.append(item)
        return {"items": items, "total": total}

    def actions(self) -> list[str]:
        """已出现过的动作类型，供前端做筛选下拉。"""
        with self._connect() as connection:
            rows = connection.execute("SELECT DISTINCT action FROM audit_log ORDER BY action").fetchall()
        return [row["action"] for row in rows]