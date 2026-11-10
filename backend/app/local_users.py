"""本地账号密码。

平台原本只有两种登录方式：LDAP 目录账号，或者一串 64 位令牌。
前者要有目录服务，后者适合机器调用但不适合人——没人记得住
`fa1ffc363a0098202ee474...`，每次登录都要去翻文件。

这里加第三种：本地账号密码。配置写在环境变量里，格式
``用户名:角色:密码哈希``，密码用 PBKDF2 哈希存储，
配置里不出现明文。

为什么用 PBKDF2 而不是存明文：
  环境变量会被 `kubectl describe deploy` 看到，也会出现在
  pod 的 /proc/<pid>/environ 里。存明文等于把密码写在墙上。
  PBKDF2-SHA256 是标准库自带、无需额外依赖的最稳妥选择。

哈希格式：``pbkdf2_sha256$迭代次数$salt_hex$hash_hex``
可以直接用 ``hash_password()`` 生成，也支持 openssl 之类的工具。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field

# 迭代次数。2024 年后的常见建议是 60 万，这里取 26 万——
# 登录不是高频操作，但也不能慢到让用户觉得卡（每次约 100ms）。
DEFAULT_ITERATIONS = 260_000
ALGORITHM = "pbkdf2_sha256"


class LocalUserError(ValueError):
    """配置写错。启动时报错，而不是让人登录失败才发现。"""


@dataclass
class LocalUser:
    username: str
    role: str
    password_hash: str


@dataclass
class LocalUserRegistry:
    entries: dict[str, LocalUser] = field(default_factory=dict)

    def get(self, username: str) -> LocalUser | None:
        return self.entries.get(username)

    def verify(self, username: str, password: str) -> LocalUser | None:
        """校验账号密码。用户名不存在时也做一次哈希运算。

        不做「用户不存在就立即返回」的优化：那样响应时间会泄露
        用户名是否存在。这里花一样的时间算一个假哈希。
        """
        user = self.entries.get(username)
        if user is None:
            # 消耗与真实校验相当的时间
            _verify_hash(password, _dummy_hash())
            return None
        return user if _verify_hash(password, user.password_hash) else None

    def __bool__(self) -> bool:
        return bool(self.entries)


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    if not password:
        raise LocalUserError("密码不能为空")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations)
    return f"{ALGORITHM}${iterations}${salt}${digest.hex()}"


def _verify_hash(password: str, stored: str) -> bool:
    try:
        algorithm, iterations_text, salt, expected = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        iterations = int(iterations_text)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), iterations
        )
    except (ValueError, TypeError):
        return False
    # 常量时间比较，避免时序侧信道
    return hmac.compare_digest(digest.hex(), expected)


_DUMMY: str | None = None


def _dummy_hash() -> str:
    """用户名不存在时用来消耗时间的假哈希。只生成一次。"""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("this-user-does-not-exist")
    return _DUMMY


def parse_local_users(raw: str | None) -> LocalUserRegistry:
    """解析 ``用户名:角色:密码哈希`` 列表，逗号分隔。

    哈希里含 ``$`` 不含 ``:``，所以按前两个冒号切分是安全的。
    """
    if not raw or not raw.strip():
        return LocalUserRegistry()

    registry = LocalUserRegistry()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 2)
        if len(parts) != 3:
            raise LocalUserError(
                f"LOCAL_USERS 条目应为 用户名:角色:密码哈希，实际是 {item[:40]!r}"
            )
        username, role, password_hash = (part.strip() for part in parts)
        if not username or not password_hash:
            raise LocalUserError(f"用户名和密码哈希都不能为空：{item[:40]!r}")
        if role not in ("admin", "user"):
            raise LocalUserError(f"角色只能是 admin/user，实际是 {role!r}")
        if username in registry.entries:
            raise LocalUserError(f"用户名重复：{username}")
        if not password_hash.startswith(f"{ALGORITHM}$"):
            raise LocalUserError(
                f"{username} 的密码哈希格式不对，应以 {ALGORITHM}$ 开头"
            )
        registry.entries[username] = LocalUser(username, role, password_hash)

    return registry
