"""多用户令牌支持。

token 模式原本只有一个静态令牌，用的人都是固定 admin——这在内网自用可以，
一旦多人共用一个平台就没法用了：分不清谁是谁，也没法限制某些人只能看自己的任务。

这里支持 ``PLATFORM_USER_TOKENS`` 配置，格式是逗号分隔的
``用户名:角色:令牌``，例如：

    PLATFORM_USER_TOKENS="alice:admin:tok-a1,bob:user:tok-b2"

语义约定（都是为了避免踩坑）：

* 令牌比较用 hmac.compare_digest，避免时序侧信道。
* 用户名重复或令牌重复时**启动即报错**，而不是静默取第一个——
  否则运维会以为配置生效了，实际有人在用别人的身份。
* 角色只接受 admin/user，写错就报错，不做"猜你想要"。
* 旧的单一 ``PLATFORM_API_TOKEN`` 仍然有效，等价于一个 admin，
  保证升级不破坏已有部署。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field

VALID_ROLES = ("admin", "user")


class TokenConfigError(ValueError):
    """令牌配置写错。启动阶段就抛出，而不是等到有人访问才发现。"""


@dataclass
class UserToken:
    username: str
    role: str
    token: str


@dataclass
class TokenRegistry:
    """令牌表。查询按令牌值精确匹配。"""

    entries: list[UserToken] = field(default_factory=list)

    def lookup(self, token: str) -> UserToken | None:
        # 逐个比较而不是建字典：比较要用常量时间，避免按长度/前缀泄露信息。
        found: UserToken | None = None
        for entry in self.entries:
            if hmac.compare_digest(entry.token.encode("utf-8"), token.encode("utf-8")):
                found = entry
        return found

    def __bool__(self) -> bool:
        return bool(self.entries)


def parse_user_tokens(raw: str | None) -> TokenRegistry:
    """解析 ``用户名:角色:令牌`` 列表。

    令牌本身可能含冒号，所以只按前两个冒号切分。
    """
    if not raw or not raw.strip():
        return TokenRegistry()

    registry = TokenRegistry()
    seen_names: set[str] = set()
    seen_tokens: set[str] = set()

    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 2)
        if len(parts) != 3:
            raise TokenConfigError(
                f"PLATFORM_USER_TOKENS 条目格式应为 用户名:角色:令牌，实际是 {item!r}"
            )
        username, role, token = (part.strip() for part in parts)
        if not username or not token:
            raise TokenConfigError(f"用户名和令牌都不能为空：{item!r}")
        if role not in VALID_ROLES:
            raise TokenConfigError(f"角色只能是 {'/'.join(VALID_ROLES)}，实际是 {role!r}")
        if username in seen_names:
            raise TokenConfigError(f"用户名重复：{username}。同名会让两人共用一个身份")
        if token in seen_tokens:
            raise TokenConfigError(f"令牌重复（用户名 {username}）。重复令牌无法区分身份")

        seen_names.add(username)
        seen_tokens.add(token)
        registry.entries.append(UserToken(username=username, role=role, token=token))

    return registry
