"""平台会话令牌：签发 / 校验 / 撤销 / 过期清理。

令牌本身是 32 字节随机串（URL 安全 Base64），库里只存 SHA-256 哈希，
即使 SQLite 文件泄露也无法直接复用。表和 user_quotas 一样建在
`PLATFORM_DB_PATH` 指向的同一个文件里，重启后会话仍然有效。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionStore:
    """复用历史库同一个 SQLite 文件，因此不单独开连接池。

    `enabled=False` 时（token / off 模式）所有查询直接短路，不读写库：
    会话令牌只在 ldap 模式下签发，其它模式即使库里有残留会话也不应被接受。
    """

    def __init__(self, path: str, enabled: bool = True):
        self.path = path
        self.enabled = enabled
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    role TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'ldap',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT
                );
                CREATE INDEX IF NOT EXISTS sessions_expires_at ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS sessions_username ON sessions(username);
                """
            )

    # --- 签发 / 校验 -------------------------------------------------------
    def issue(
        self,
        username: str,
        role: str,
        ttl_hours: int,
        source: str = "ldap",
        verified: bool = True,
    ) -> dict[str, Any]:
        """签发令牌。

        `verified=False` 用于调用方已经自己做过 LDAP 校验（authenticate 之后），
        避免绑定两次；直接调用 issue 的测试/工具则默认标记为已验证。
        """
        self.enabled = True  # 一旦签发了会话，后续 lookup 必须真正查库
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expires_at = now + timedelta(hours=ttl_hours)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions(token_hash, username, role, source, created_at, expires_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (hash_token(token), username, role, source, to_iso(now), to_iso(expires_at), to_iso(now)),
            )
        return {
            "token": token,
            "expires_at": to_iso(expires_at),
            "username": username,
            "role": role,
            "verified": verified,
        }

    def lookup(self, token: str, now: datetime | None = None) -> dict[str, Any] | None:
        """返回有效会话；过期会话视为不存在（并顺手删除）。"""
        if not token or not self.enabled:
            return None
        moment = now or utc_now()
        digest = hash_token(token)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT token_hash, username, role, source, expires_at FROM sessions WHERE token_hash = ?",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            if from_iso(row["expires_at"]) <= moment:
                connection.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
                return None
            connection.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                (to_iso(moment), digest),
            )
        return {
            "username": row["username"],
            "role": row["role"],
            "source": row["source"],
            "expires_at": row["expires_at"],
        }

    def revoke(self, token: str) -> bool:
        if not self.enabled:
            return False
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))
        return cursor.rowcount > 0

    def purge_expired(self, now: datetime | None = None) -> int:
        moment = now or utc_now()
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (to_iso(moment),))
        return cursor.rowcount

    # --- 用户视图 ---------------------------------------------------------
    def known_users(self) -> dict[str, dict[str, Any]]:
        """从活跃会话推导「见过哪些用户」，供 GET /users 使用。"""
        users: dict[str, dict[str, Any]] = {}
        if not self.enabled:
            return users
        moment = to_iso(utc_now())
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT username, role, source, last_seen_at FROM sessions WHERE expires_at > ?",
                (moment,),
            ).fetchall()
        for row in rows:
            current = users.get(row["username"])
            if current is None or (row["last_seen_at"] or "") > (current.get("last_seen_at") or ""):
                users[row["username"]] = {
                    "username": row["username"],
                    "role": row["role"],
                    "source": row["source"],
                    "last_seen_at": row["last_seen_at"],
                }
        return users

    def delete_for_user(self, username: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE username = ?", (username,))
