"""多用户令牌测试。

这是安全相关功能，测试重点不只是「能登录」，更是：
谁是谁能被正确区分、配错会不会静默生效、以及老部署会不会被破坏。
"""

from __future__ import annotations

import pytest
from app.user_tokens import TokenConfigError, parse_user_tokens


def test_parses_multiple_users():
    registry = parse_user_tokens("alice:admin:tok-a,bob:user:tok-b")

    assert len(registry.entries) == 2
    assert registry.lookup("tok-a").username == "alice"
    assert registry.lookup("tok-a").role == "admin"
    assert registry.lookup("tok-b").role == "user"


def test_unknown_token_returns_none():
    registry = parse_user_tokens("alice:admin:tok-a")

    assert registry.lookup("tok-unknown") is None


def test_empty_config_gives_empty_registry():
    assert not parse_user_tokens(None)
    assert not parse_user_tokens("")
    assert not parse_user_tokens("   ")


def test_token_may_contain_colons():
    """令牌本身可能含冒号，只按前两个冒号切分。"""
    registry = parse_user_tokens("alice:admin:ab:cd:ef")

    matched = registry.lookup("ab:cd:ef")
    assert matched is not None
    assert matched.username == "alice"


@pytest.mark.parametrize(
    "raw",
    [
        "alice-admin-tok",          # 缺少分隔
        "alice:admin",              # 缺少令牌
        "alice:superuser:tok",      # 角色非法
        ":admin:tok",               # 用户名为空
        "alice:admin:",             # 令牌为空
    ],
)
def test_malformed_entries_raise(raw):
    """配错必须启动即报错，而不是静默跳过。

    静默跳过的后果是运维以为某个令牌生效了，实际那个人根本进不来；
    或者更糟——回退到别的身份。
    """
    with pytest.raises(TokenConfigError):
        parse_user_tokens(raw)


def test_duplicate_username_rejected():
    """同名会让两个人共用一个身份，审计就失去意义。"""
    with pytest.raises(TokenConfigError, match="用户名重复"):
        parse_user_tokens("alice:admin:tok-1,alice:user:tok-2")


def test_duplicate_token_rejected():
    with pytest.raises(TokenConfigError, match="令牌重复"):
        parse_user_tokens("alice:admin:same,bob:user:same")


def test_whitespace_tolerated():
    registry = parse_user_tokens(" alice : admin : tok-a , bob : user : tok-b ")

    assert registry.lookup("tok-a").username == "alice"
    assert registry.lookup("tok-b").role == "user"


def test_legacy_single_token_still_works():
    """老部署只配 PLATFORM_API_TOKEN 时，注册表为空，走回退分支。"""
    registry = parse_user_tokens("")

    assert not registry
    assert registry.lookup("anything") is None
