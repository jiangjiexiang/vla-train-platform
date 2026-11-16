"""本地用户存储（SQLite）：支持管理员在平台界面增删改本地账号。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import ROLE_ADMIN, ROLE_USER
from ..local_users import LocalUser, _dummy_hash, _verify_hash, hash_password
from .errors import ServiceError
from .sessions import utc_now

VALID_ROLES = (ROLE_USER, ROLE_ADMIN)


@dataclass(frozen=True)
class StoredUser:
    username: str
    role: str
    password_hash: str
    created_at: str
    updated_at: str

    def as_local_user(self) -> LocalUser:
        return LocalUser(username=self.username, role=self.role, password_hash=self.password_hash)

    def as_summary(self) -> dict[str, Any]:
        return {
            username: self.username,
            role: self.role,
            source: local,
            created_at: self.created_at,
            updated_at: self.updated_at,
        }


class UserStore:
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
                CREATE TABLE IF NOT EXISTS local_users (
                    username TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def list_all(self) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT username, role, created_at, updated_at FROM local_users ORDER BY username"
            ).fetchall()
        return {
            row["username"]: {
                "username": row["username"],
                "role": row["role"],
                "source": "local",
                "last_seen_at": row["updated_at"],
            }
            for row in rows
        }

    def get(self, username: str) -> StoredUser | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT username, role, password_hash, created_at, updated_at FROM local_users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return StoredUser(
            username=row["username"],
            role=row["role"],
            password_hash=row["password_hash"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create(self, username: str, password: str, role: str = ROLE_USER) -> StoredUser:
        if role not in VALID_ROLES:
            raise ServiceError(422, "invalid_role", f"角色只能是 admin 或 user，实际为 {role!r}")
        if not password:
            raise ServiceError(422, "invalid_password", "密码不能为空")
        now = utc_now().isoformat()
        password_hash = hash_password(password)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM local_users WHERE username = ?", (username,)
            ).fetchone()
            if existing is not None:
                raise ServiceError(409, "user_exists", f"用户 {username} 已存在")
            connection.execute(
                """
                INSERT INTO local_users (username, role, password_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username, role, password_hash, now, now),
            )
        return StoredUser(
            username=username,
            role=role,
            password_hash=password_hash,
            created_at=now,
            updated_at=now,
        )

    def update(self, username: str, *, role: str | None = None, password: str | None = None) -> StoredUser:
        current = self.get(username)
        if current is None:
            raise ServiceError(404, "user_not_found", f"本地用户 {username} 不存在")
        new_role = role if role is not None else current.role
        if new_role not in VALID_ROLES:
            raise ServiceError(422, "invalid_role", f"角色只能是 admin 或 user，实际为 {new_role!r}")
        if password is not None:
            if not password:
                raise ServiceError(422, "invalid_password", "密码不能为空")
            new_hash = hash_password(password)
        else:
            new_hash = current.password_hash
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE local_users SET role = ?, password_hash = ?, updated_at = ? WHERE username = ?",
                (new_role, new_hash, now, username),
            )
        return StoredUser(
            username=username,
            role=new_role,
            password_hash=new_hash,
            created_at=current.created_at,
            updated_at=now,
        )

    def delete(self, username: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM local_users WHERE username = ?", (username,))
            if cursor.rowcount == 0:
                raise ServiceError(404, "user_not_found", f"本地用户 {username} 不存在")

    def verify(self, username: str, password: str) -> LocalUser | None:
        user = self.get(username)
        if user is None:
            _verify_hash(password, _dummy_hash())
            return None
        return user.as_local_user() if _verify_hash(password, user.password_hash) else None
