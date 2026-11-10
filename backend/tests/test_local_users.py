"""本地账号密码测试。

这个模式存在的理由很实际：原来只有 LDAP 或 64 位令牌，
前者要有目录服务，后者没人记得住——每次登录都得翻文件找那串十六进制。
"""

from __future__ import annotations

import pytest
from app.local_users import (
    ALGORITHM,
    LocalUserError,
    hash_password,
    parse_local_users,
)


def test_hash_and_verify():
    h = hash_password("VlaPlatform2026")

    assert h.startswith(f"{ALGORITHM}$")
    # 同一密码两次哈希结果不同（salt 随机）
    assert h != hash_password("VlaPlatform2026")


def test_registry_verifies_correct_password():
    h = hash_password("secret123")
    registry = parse_local_users(f"admin:admin:{h}")

    user = registry.verify("admin", "secret123")

    assert user is not None
    assert user.role == "admin"


def test_registry_rejects_wrong_password():
    h = hash_password("secret123")
    registry = parse_local_users(f"admin:admin:{h}")

    assert registry.verify("admin", "wrong") is None
    assert registry.verify("admin", "") is None


def test_unknown_user_returns_none():
    registry = parse_local_users(f"admin:admin:{hash_password('x')}")

    assert registry.verify("nobody", "x") is None


def test_multiple_users():
    raw = f"a:admin:{hash_password('pw1')},b:user:{hash_password('pw2')}"

    registry = parse_local_users(raw)

    assert registry.verify("a", "pw1").role == "admin"
    assert registry.verify("b", "pw2").role == "user"
    # 不能串号
    assert registry.verify("a", "pw2") is None


@pytest.mark.parametrize(
    "raw",
    [
        "admin-admin-hash",          # 缺分隔
        "admin:admin",               # 缺哈希
        "admin:root:hash",           # 角色非法
        ":admin:hash",               # 用户名为空
        "admin:admin:",              # 哈希为空
        "admin:admin:plaintext",     # 不是合法哈希格式
    ],
)
def test_malformed_config_rejected(raw):
    """配错必须启动即报错。

    静默跳过的后果是运维以为账号建好了，实际谁都登不进去。
    """
    with pytest.raises(LocalUserError):
        parse_local_users(raw)


def test_duplicate_username_rejected():
    h = hash_password("x")

    with pytest.raises(LocalUserError, match="重复"):
        parse_local_users(f"admin:admin:{h},admin:user:{h}")


def test_empty_config():
    assert not parse_local_users("")
    assert not parse_local_users(None)


def test_hash_rejects_empty_password():
    with pytest.raises(LocalUserError):
        hash_password("")


def test_password_not_in_config():
    """配置里不能出现明文密码。"""
    h = hash_password("MySuperSecret123")

    assert "MySuperSecret123" not in h
