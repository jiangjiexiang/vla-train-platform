"""LDAP / AD 认证。

只做一件事：给定用户名与密码，判断凭据是否有效、属于哪个组。
调用方（routes）负责把结果换成平台会话令牌。

**错误区分是契约硬要求**：LDAP 服务器连不上（网络、证书、服务端挂了）
必须抛 `LDAPUnavailable` → HTTP 503；只有「服务器明确回答凭据不对」才是 401。
把 503 吞成 401 会让用户反复输密码而运维看不到故障。

所有 ldap3 调用集中在本模块，方便测试用 monkeypatch 替换，不连真 LDAP。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import ROLE_ADMIN, ROLE_USER, Settings

logger = logging.getLogger("vla-platform.ldap")

# 这些异常表示「连不上/协议/加密出错」，属于服务端问题，映射为 503。
# 用异常类名而不是 import 具体类型判断，避免 ldap3 小版本更名导致导入失败。
_CONNECTION_ERRORS = (
    "LDAPSocketOpenError",
    "LDAPSocketReceiveError",
    "LDAPSocketSendError",
    "LDAPServerPoolExhaustedError",
    "LDAPStartTLSError",
    "LDAPResponseTimeoutError",
    "LDAPSessionTerminatedByServerError",
    "LDAPUnknownResponseError",
    "LDAPInvalidServerError",
    "LDAPAutoBindError",
    "LDAPCommunicationError",
    "LDAPCertificateError",
)


class LDAPUnavailable(Exception):
    """LDAP 服务不可用 —— 必须暴露成 503，而不是认证失败。"""


class LDAPNotConfigured(LDAPUnavailable):
    """ldap 模式下缺少必要配置。"""


class InvalidCredentials(Exception):
    """用户名或密码错误（统一报错，不区分用户不存在/密码错）。"""


@dataclass(frozen=True)
class LDAPUser:
    username: str
    role: str
    groups: tuple[str, ...] = field(default_factory=tuple)


def servers(settings: Settings) -> list[str]:
    return [item.strip() for item in (settings.ldap_url or "").split(",") if item.strip()]


def user_dn(username: str, settings: Settings) -> str:
    template = settings.ldap_user_template or "uid={username},{base_dn}"
    return template.format(username=username, base_dn=settings.ldap_base_dn)


def escape_filter(value: str) -> str:
    """RFC 4515 转义：用户名来自用户输入，直接拼进过滤器会被注入。"""
    return (
        str(value)
        .replace("\\", "\\5c")
        .replace("*", "\\2a")
        .replace("(", "\\28")
        .replace(")", "\\29")
        .replace("\x00", "\\00")
    )


def role_for(username: str, groups: tuple[str, ...], settings: Settings) -> str:
    """角色映射：admin 组成员 → admin；配置了白名单则只认白名单内为 admin。"""
    if settings.ldap_group_dn_admin:
        wanted = _normalize_dn(settings.ldap_group_dn_admin)
        if any(_normalize_dn(group) == wanted for group in groups):
            return ROLE_ADMIN
    if settings.ldap_user_whitelist:
        if username in settings.ldap_user_whitelist:
            return ROLE_ADMIN
        return ROLE_USER
    # 未配置 admin 组时，登录成功即普通用户（最小权限）。
    return ROLE_USER


def authenticate(username: str, password: str, settings: Settings) -> LDAPUser:
    """验证凭据。成功返回 LDAPUser；失败抛 InvalidCredentials 或 LDAPUnavailable。"""
    if not username or not password:
        # 空密码在 LDAP 里等价于匿名 bind（会"成功"），必须提前拒绝。
        raise InvalidCredentials
    _ldap3()  # 依赖缺失时统一报 503，而不是落成 401
    if not servers(settings) or not settings.ldap_base_dn:
        raise LDAPNotConfigured("LDAP is not configured")

    last_error: Exception | None = None
    for url in servers(settings):
        try:
            return _authenticate_one(url, username, password, settings)
        except InvalidCredentials:
            # 凭据错误是服务器的明确回答，换下一个地址也没用。
            raise
        except LDAPUnavailable as exc:
            last_error = exc
            logger.warning("LDAP server %s unavailable: %s", url, exc)
            continue

    raise LDAPUnavailable(f"LDAP service unavailable: {last_error}" if last_error else "LDAP service unavailable")


def verify(username: str, settings: Settings) -> LDAPUser:
    """复查一个已有会话的主人：服务账号只读查询，不接触用户密码。

    用途是让禁用/删除的账号即时失效、组变更即时生效。走 bind 时密码必然错误
    会有账号锁定风险，所以这里一律用服务账号（或匿名）查询：
    - 用户查不到  -> InvalidCredentials（账号不存在/被删）
    - 查不到组    -> 按组判定的用户降级为 user（最小权限）
    连接失败仍然是 LDAPUnavailable -> 503（或由调用方决定保持会话）。
    """
    if not username:
        raise InvalidCredentials
    if not servers(settings) or not settings.ldap_base_dn:
        raise LDAPNotConfigured("LDAP is not configured")

    last_error: Exception | None = None
    for url in servers(settings):
        try:
            return _verify_one(url, username, settings)
        except InvalidCredentials:
            raise
        except LDAPUnavailable as exc:
            last_error = exc
            logger.warning("LDAP server %s unavailable while re-checking %s: %s", url, username, exc)
            continue
    raise LDAPUnavailable(f"LDAP service unavailable: {last_error}" if last_error else "LDAP service unavailable")


def _verify_one(url: str, username: str, settings: Settings) -> LDAPUser:
    ldap3 = _ldap3()
    bind_dn, bind_password = settings.ldap_bind_dn, settings.ldap_bind_password
    host, port, use_ssl = _parse_url(url)
    try:
        server = ldap3.Server(
            host,
            port=port,
            use_ssl=use_ssl,
            get_info=ldap3.NONE,
            connect_timeout=settings.ldap_timeout_seconds,
        )
        connection = ldap3.Connection(
            server,
            user=bind_dn or None,
            password=bind_password or None,
            authentication=ldap3.SIMPLE if bind_dn else getattr(ldap3, "ANONYMOUS", "ANONYMOUS"),
            auto_bind=ldap3.AUTO_BIND_NO_TLS,
            receive_timeout=settings.ldap_timeout_seconds,
            raise_exceptions=False,
        )
    except ValueError as exc:
        raise LDAPUnavailable(f"invalid LDAP URL {url}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise _classify(exc) from exc
    if not connection.bound:
        # 服务账号自身绑定失败属于配置/服务问题，不是用户凭据问题。
        raise LDAPUnavailable(f"LDAP bind account rejected by {url}")

    entries: list[dict[str, Any]] = []
    try:
        connection.search(
            settings.ldap_base_dn,
            f"({_user_attribute(settings)}={escape_filter(username)})",
            attributes=["memberOf"],
            size_limit=10,
        )
        entries = list(connection.response or [])
    except Exception as exc:  # noqa: BLE001
        _raise_if_unavailable(exc)
    groups: list[str] = []
    for entry in entries:
        value = (entry.get("attributes") or {}).get("memberOf")
        if isinstance(value, str):
            groups.append(value)
        elif isinstance(value, (list, tuple)):
            groups.extend(str(item) for item in value)
    try:
        connection.unbind()
    except Exception:  # noqa: BLE001
        pass
    if not entries:
        raise InvalidCredentials
    return LDAPUser(username=username, role=role_for(username, tuple(groups), settings), groups=tuple(groups))


def _authenticate_one(url: str, username: str, password: str, settings: Settings) -> LDAPUser:
    """连一个 LDAP 地址做 bind。

    Server/Connection 的构造属于网络阶段：连不上时 ldap3 抛的异常必须映射成
    503，而不是被下面那段"URL 解析错误"的 ValueError 分支误判。
    """
    ldap3 = _ldap3()
    host, port, use_ssl = _parse_url(url)
    try:
        server = ldap3.Server(
            host,
            port=port,
            use_ssl=use_ssl,
            get_info=ldap3.NONE,
            connect_timeout=settings.ldap_timeout_seconds,
        )
        connection = ldap3.Connection(
            server,
            user=user_dn(username, settings),
            password=password,
            authentication=ldap3.SIMPLE,
            auto_bind=ldap3.AUTO_BIND_NO_TLS,
            receive_timeout=settings.ldap_timeout_seconds,
            raise_exceptions=False,
        )
    except ValueError as exc:
        raise LDAPUnavailable(f"invalid LDAP URL {url}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - 按异常类型分流，见 _classify
        raise _classify(exc) from exc

    if not connection.bound:
        raise InvalidCredentials
    groups = _groups(connection, username, settings)
    try:
        connection.unbind()
    except Exception:  # noqa: BLE001 - 断开失败不影响认证结论
        pass
    return LDAPUser(username=username, role=role_for(username, groups, settings), groups=groups)


def _groups(connection: Any, username: str, settings: Settings) -> tuple[str, ...]:
    """读取用户所属组 DN。AD 用 memberOf；OpenLDAP 的 groupOfNames 再补一次搜索。

    组信息拿不到时退化为普通用户（空元组），但连接类异常仍然上抛 503。
    """
    _ldap3()  # 确保依赖存在：缺失时抛 LDAPUnavailable
    groups: list[str] = []
    try:
        connection.search(
            settings.ldap_base_dn,
            f"({_user_attribute(settings)}={escape_filter(username)})",
            attributes=["memberOf"],
            size_limit=10,
        )
        for entry in connection.response or []:
            attributes = entry.get("attributes") or entry.get("raw_attributes") or {}
            value = attributes.get("memberOf") if isinstance(attributes, dict) else None
            if isinstance(value, str):
                groups.append(value)
            elif isinstance(value, (list, tuple)):
                groups.extend(str(item) for item in value)
    except Exception as exc:  # noqa: BLE001
        _raise_if_unavailable(exc)  # 连接问题 -> 503；其它（如无 memberOf 属性）忽略
    wanted = _normalize_dn(settings.ldap_group_dn_admin) if settings.ldap_group_dn_admin else ""
    if wanted and wanted not in {_normalize_dn(group) for group in groups}:
        try:
            connection.search(
                settings.ldap_base_dn,
                f"(&(objectClass=groupOfNames)(member={user_dn(username, settings)}))",
                attributes=["cn"],
                size_limit=50,
            )
            for entry in connection.response or []:
                if entry.get("dn"):
                    groups.append(entry["dn"])
        except Exception as exc:  # noqa: BLE001
            _raise_if_unavailable(exc)
    return tuple(dict.fromkeys(groups))


def _raise_if_unavailable(exc: Exception) -> None:
    """搜索阶段的异常：连接类上抛 503，其余（属性不存在等）忽略。"""
    classified = _classify(exc)
    if isinstance(classified, LDAPUnavailable):
        raise classified from exc


def _user_attribute(settings: Settings) -> str:
    """从 user template 里取属性名（uid / sAMAccountName / cn ...），搜索时复用。"""
    template = settings.ldap_user_template or "uid={username},{base_dn}"
    head = template.split("{username}", 1)[0].strip().rstrip(",").split(",")[-1]
    attribute, _, _ = head.partition("=")
    return attribute.strip() or "uid"


def _classify(exc: Exception) -> Exception:
    """把 ldap3 异常分流为 401（凭据错）或 503（服务不可用）。

    ldap3 的 LDAPBindError 同时覆盖两种情况：密码错（异常信息里是
    invalidCredentials）和服务器不可达（套接字/超时）。因此必须看信息内容，
    不能一律当成密码错，否则运维看不到 503。
    """
    name = type(exc).__name__
    message = str(exc).lower()
    if name in _CONNECTION_ERRORS:
        return LDAPUnavailable(f"{name}: {exc}")
    if name == "LDAPBindError" and not any(
        hint in message for hint in ("socket", "timed out", "timeout", "connection", "refused", "unreachable")
    ):
        return InvalidCredentials()
    if "invalidcredentials" in message:
        return InvalidCredentials()
    return LDAPUnavailable(f"{name}: {exc}")


def _parse_url(url: str) -> tuple[str, int, bool]:
    """支持 ldap://host:389 / ldaps://host:636 / host（默认 389）。"""
    raw = url.strip()
    use_ssl = False
    lowered = raw.lower()
    if lowered.startswith("ldaps://"):
        raw, use_ssl = raw[len("ldaps://") :], True
    elif lowered.startswith("ldap://"):
        raw = raw[len("ldap://") :]
    elif "://" in raw:
        raise ValueError("only ldap:// and ldaps:// are supported")
    host, _, port = raw.partition("/")
    host, _, port_text = host.partition(":")
    if not host:
        raise ValueError("missing host")
    if port_text:
        port = int(port_text)
    else:
        port = 636 if use_ssl else 389
    return host, port, use_ssl


def _normalize_dn(dn: str) -> str:
    return ",".join(part.strip().lower() for part in (dn or "").split(","))


_LDAP3: Any = None


def _ldap3() -> Any:
    """延迟导入：off/token 模式完全不需要 ldap3 也能启动。"""
    global _LDAP3
    if _LDAP3 is None:
        try:
            import ldap3  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - 依赖已写入 requirements.txt
            raise LDAPUnavailable("ldap3 is not installed") from exc
        _LDAP3 = ldap3
    return _LDAP3