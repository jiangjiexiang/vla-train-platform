"""LDAP 认证（契约 1.3 / 2.3）：用 monkeypatch 假 server，不连真 LDAP。

核心断言是**错误分流**：连不上 → 503，凭据错 → 401，且两者都不泄露
"用户是否存在"。另外覆盖多地址回退、组→角色映射与配置校验。
"""

from __future__ import annotations

import os

import pytest
from app.config import Settings
from app.services import ldap_auth

LDAP_ENV = {
    "PLATFORM_AUTH_MODE": "ldap",
    "PLATFORM_LDAP_URL": "ldap://ldap.corp:389",
    "PLATFORM_LDAP_BASE_DN": "dc=corp,dc=com",
}


class FakeServer:
    """替代 ldap3.Server：只记录参数，不做任何网络操作。"""

    def __init__(self, host, **kwargs):
        self.host = host
        self.kwargs = kwargs


class FakeConnection:
    """替代 ldap3.Connection：按脚本决定 bind 成功/失败/抛异常。"""

    behaviour: dict[str, object] = {}
    calls: list[dict] = []

    def __init__(self, server, user=None, password=None, **kwargs):
        self.server = server
        self.user = user
        self.password = password
        self.kwargs = kwargs
        self.response = FakeConnection.behaviour.get("response", [])
        FakeConnection.calls.append({"server": server.host, "user": user, "sasl": None})
        outcome = FakeConnection.behaviour.get("outcome", "ok")
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "invalid":
            from ldap3.core.exceptions import LDAPBindError

            raise LDAPBindError("invalidCredentials")
        self.bound = True

    def search(self, base, filter, attributes=None, size_limit=None):  # noqa: A002 - 与 ldap3 签名一致
        self.response = FakeConnection.behaviour.get("response", [])
        return True

    def unbind(self):
        return True


class FakeLdap3:
    Server = FakeServer
    Connection = FakeConnection
    NONE = "NO_INFO"
    SIMPLE = "SIMPLE"
    ANONYMOUS = "ANONYMOUS"
    AUTO_BIND_NO_TLS = "NO_TLS"


@pytest.fixture
def fake_ldap(monkeypatch):
    FakeConnection.behaviour = {"outcome": "ok", "response": []}
    FakeConnection.calls = []

    def fake_ldap3():
        return FakeLdap3

    monkeypatch.setattr(ldap_auth, "_ldap3", fake_ldap3)
    return FakeConnection


def make_settings(tmp_path, **overrides) -> Settings:
    values = {"api_token": "test-token", "db_path": str(tmp_path / "ldap.db"), "gpu_metrics_url": ""}
    values.update(overrides)
    return Settings(**values)


def ldap_settings(tmp_path, **overrides) -> Settings:
    values = {
        "auth_mode": "ldap",
        "auth_required": True,
        "ldap_url": "ldap://ldap.corp:389",
        "ldap_base_dn": "dc=corp,dc=com",
        "ldap_user_template": "uid={username},ou=people,dc=corp,dc=com",
    }
    values.update(overrides)
    return make_settings(tmp_path, **values)


# --- 直接调用 authenticate ------------------------------------------------


def test_authenticate_builds_dn_from_template(tmp_path, fake_ldap):
    settings = ldap_settings(tmp_path)
    user = ldap_auth.authenticate("alice", "secret", settings)
    assert user.username == "alice"
    assert user.role == "user"
    assert fake_ldap.calls[0]["user"] == "uid=alice,ou=people,dc=corp,dc=com"
    assert fake_ldap.calls[0]["server"] == "ldap.corp"


def test_authenticate_rejects_wrong_password(tmp_path, fake_ldap):
    fake_ldap.behaviour["outcome"] = "invalid"
    with pytest.raises(ldap_auth.InvalidCredentials):
        ldap_auth.authenticate("alice", "bad", ldap_settings(tmp_path))


def test_authenticate_rejects_empty_password_without_binding(tmp_path, fake_ldap):
    """空密码在 LDAP 里是匿名 bind，必须在本地拦掉。"""
    with pytest.raises(ldap_auth.InvalidCredentials):
        ldap_auth.authenticate("alice", "", ldap_settings(tmp_path))
    assert fake_ldap.calls == []


def test_authenticate_unreachable_server_raises_unavailable(tmp_path, fake_ldap):
    from ldap3.core.exceptions import LDAPSocketOpenError

    fake_ldap.behaviour["outcome"] = LDAPSocketOpenError("socket connection failed")
    with pytest.raises(ldap_auth.LDAPUnavailable):
        ldap_auth.authenticate("alice", "secret", ldap_settings(tmp_path))


def install_down_hosts(monkeypatch, down: tuple[str, ...]) -> list[str]:
    """让指定主机的 Server 构造直接抛 LDAPSocketOpenError，其余主机正常工作。"""
    from ldap3.core.exceptions import LDAPSocketOpenError

    attempts: list[str] = []

    class TrackingServer(FakeServer):
        def __init__(self, host, **kwargs):
            super().__init__(host, **kwargs)
            attempts.append(host)
            if host in down:
                raise LDAPSocketOpenError("connection refused")

    class TrackingLdap3(FakeLdap3):
        Server = TrackingServer

    monkeypatch.setattr(ldap_auth, "_ldap3", lambda: TrackingLdap3)
    return attempts


def test_authenticate_tries_each_server_then_fails(tmp_path, fake_ldap, monkeypatch):
    attempts = install_down_hosts(monkeypatch, down=("a", "b"))
    settings = ldap_settings(tmp_path, ldap_url="ldap://a:389,ldap://b:389")

    with pytest.raises(ldap_auth.LDAPUnavailable):
        ldap_auth.authenticate("alice", "secret", settings)
    assert attempts == ["a", "b"], "应依次尝试每个地址"


def test_authenticate_falls_back_to_second_server(tmp_path, fake_ldap, monkeypatch):
    attempts = install_down_hosts(monkeypatch, down=("down",))
    settings = ldap_settings(tmp_path, ldap_url="ldap://down:389,ldap://up:389")

    user = ldap_auth.authenticate("alice", "secret", settings)
    assert user.username == "alice"
    assert attempts == ["down", "up"]


def test_group_membership_grants_admin(tmp_path, fake_ldap):
    fake_ldap.behaviour["response"] = [
        {
            "dn": "uid=alice,ou=people,dc=corp,dc=com",
            "attributes": {"memberOf": ["cn=vla-admins,ou=groups,dc=corp,dc=com"]},
        }
    ]
    settings = ldap_settings(tmp_path, ldap_group_dn_admin="CN=vla-admins,OU=groups,DC=corp,DC=com")
    assert ldap_auth.authenticate("alice", "secret", settings).role == "admin"


def test_missing_group_keeps_user_role(tmp_path, fake_ldap):
    fake_ldap.behaviour["response"] = []
    settings = ldap_settings(tmp_path, ldap_group_dn_admin="cn=vla-admins,dc=corp,dc=com")
    assert ldap_auth.authenticate("alice", "secret", settings).role == "user"


def test_whitelist_maps_admin(tmp_path, fake_ldap):
    settings = ldap_settings(tmp_path, ldap_user_whitelist=("alice",))
    assert ldap_auth.authenticate("alice", "secret", settings).role == "admin"
    assert ldap_auth.authenticate("bob", "secret", settings).role == "user"


def test_authenticate_without_configuration_is_unavailable(tmp_path, fake_ldap):
    settings = ldap_settings(tmp_path, ldap_url="")
    with pytest.raises(ldap_auth.LDAPUnavailable):
        ldap_auth.authenticate("alice", "secret", settings)


@pytest.mark.parametrize(
    ("url", "host", "port", "use_ssl"),
    [
        ("ldap://h", "h", 389, False),
        ("ldap://h:1389", "h", 1389, False),
        ("ldaps://h", "h", 636, True),
        ("ldaps://h:1636", "h", 1636, True),
        ("h", "h", 389, False),
    ],
)
def test_parse_url(url, host, port, use_ssl):
    assert ldap_auth._parse_url(url) == (host, port, use_ssl)


def test_parse_url_rejects_other_schemes():
    with pytest.raises(ValueError):
        ldap_auth._parse_url("http://h")


# --- HTTP 层：401 vs 503 --------------------------------------------------


def login(client, username="alice", password="secret"):
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


def test_login_success_returns_platform_token(tmp_path, app_client, fake_ldap):
    client, _service = app_client(ldap_settings(tmp_path))
    with client:
        response = login(client)
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"token", "expires_at", "user"}
        assert body["user"] == {"username": "alice", "role": "user", "source": "ldap"}
        assert body["expires_at"].endswith("Z")


def test_login_bad_password_returns_401_unified(tmp_path, app_client, fake_ldap):
    fake_ldap.behaviour["outcome"] = "invalid"
    client, _service = app_client(ldap_settings(tmp_path))
    with client:
        response = login(client, password="nope")
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid username or password"}


def test_login_unknown_user_is_indistinguishable_from_bad_password(tmp_path, app_client, fake_ldap):
    """用户不存在时 ldap3 同样 bind 失败，报错文案必须一致。"""
    fake_ldap.behaviour["outcome"] = "invalid"
    client, _service = app_client(ldap_settings(tmp_path))
    with client:
        # 未知用户名的结果与已知用户名的错误密码必须完全一致（同一异常路径）。
        unknown = login(client, username="ghost")
        assert unknown.status_code == 401
        assert unknown.json() == {"detail": "Invalid username or password"}
        assert unknown.content == login(client, username="alice", password="wrong").content


def test_login_ldap_down_returns_503_not_401(tmp_path, app_client, fake_ldap):
    from ldap3.core.exceptions import LDAPSocketOpenError

    fake_ldap.behaviour["outcome"] = LDAPSocketOpenError("socket connection failed")
    client, _service = app_client(ldap_settings(tmp_path))
    with client:
        response = login(client)
        assert response.status_code == 503, response.text
        assert "LDAP service unavailable" in response.json()["detail"]


def test_login_timeout_returns_503(tmp_path, app_client, fake_ldap):
    from ldap3.core.exceptions import LDAPResponseTimeoutError

    fake_ldap.behaviour["outcome"] = LDAPResponseTimeoutError("timeout")
    client, _service = app_client(ldap_settings(tmp_path))
    with client:
        assert login(client).status_code == 503


def test_login_when_ldap3_missing_returns_503(tmp_path, app_client, monkeypatch):
    def missing():
        raise ldap_auth.LDAPUnavailable("ldap3 is not installed")

    monkeypatch.setattr(ldap_auth, "_ldap3", missing)
    client, _service = app_client(ldap_settings(tmp_path))
    with client:
        response = login(client)
        assert response.status_code == 503
        assert "ldap3 is not installed" in response.json()["detail"]


def test_login_in_off_mode_returns_400(tmp_path, app_client):
    client, _service = app_client(make_settings(tmp_path, auth_mode="off", auth_required=False))
    with client:
        response = login(client)
        assert response.status_code == 400
        assert "off" in response.json()["detail"]


# --- from_env 与 URL 校验 -------------------------------------------------


def test_from_env_rejects_template_without_placeholder(monkeypatch):
    for key in list(os.environ):
        if key.startswith("PLATFORM_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in LDAP_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PLATFORM_LDAP_USER_TEMPLATE", "uid=alice,dc=corp,dc=com")
    with pytest.raises(RuntimeError, match="username"):
        Settings.from_env()


def test_from_env_ldap_settings(monkeypatch):
    for key in list(os.environ):
        if key.startswith("PLATFORM_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in LDAP_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PLATFORM_LDAP_GROUP_DN_ADMIN", "cn=vla-admins,dc=corp,dc=com")
    settings = Settings.from_env()
    assert settings.auth_mode == "ldap"
    assert settings.ldap_base_dn == "dc=corp,dc=com"
    assert settings.ldap_user_template == "uid={username},{base_dn}"
    assert settings.ldap_group_dn_admin == "cn=vla-admins,dc=corp,dc=com"


def test_login_session_is_rechecked_against_ldap(tmp_path, app_client, fake_ldap):
    """会话令牌每个请求都复查 LDAP：账号被删立刻 401，组变更立刻生效。"""
    monkeypatch_settings = ldap_settings(tmp_path, ldap_group_dn_admin="")
    fake_ldap.behaviour["response"] = []
    client, service = app_client(monkeypatch_settings)
    with client:
        login = client.post("/api/v1/auth/login", json={"username": "alice", "password": "pw"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['token']}"}

        # 复查路径用匿名/服务账号绑定：当前 FakeConnection 的行为对结果无影响。
        fake_ldap.behaviour["response"] = [{"dn": "uid=alice,dc=corp,dc=com", "attributes": {"memberOf": []}}]
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

        # 账号在 LDAP 里不存在了 -> 复用同一连接对象，下一次搜索返回空。
        fake_ldap.behaviour["response"] = []
        response = client.get("/api/v1/auth/me", headers=headers)
        assert response.status_code == 401
        # 会话已被清除，再用同一令牌还是 401。
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
        assert service.sessions.lookup(login.json()["token"]) is None


def test_ldap_outage_does_not_log_everyone_out(tmp_path, app_client, fake_ldap):
    from ldap3.core.exceptions import LDAPSocketOpenError

    fake_ldap.behaviour["response"] = [{"dn": "uid=alice,dc=corp,dc=com", "attributes": {"memberOf": []}}]
    client, _service = app_client(ldap_settings(tmp_path))
    with client:
        login = client.post("/api/v1/auth/login", json={"username": "alice", "password": "pw"}).json()
        headers = {"Authorization": f"Bearer {login['token']}"}
        fake_ldap.behaviour["outcome"] = LDAPSocketOpenError("down")
        me = client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200, "LDAP 抖动不应把已登录用户踢下线"
        assert me.json()["username"] == "alice"


def test_verify_uses_service_account_and_read_only_search(tmp_path, fake_ldap):
    settings = ldap_settings(tmp_path, ldap_bind_dn="cn=svc,dc=corp,dc=com", ldap_bind_password="svcpw")
    fake_ldap.calls.clear()
    fake_ldap.behaviour["response"] = [{"dn": "uid=alice,dc=corp,dc=com", "attributes": {"memberOf": []}}]
    user = ldap_auth.verify("alice", settings)
    assert user.username == "alice"
    assert fake_ldap.calls[0]["user"] == "cn=svc,dc=corp,dc=com", "复查必须用服务账号，不能用用户密码重新绑定"


def test_verify_raises_invalid_credentials_when_user_missing(tmp_path, fake_ldap):
    fake_ldap.behaviour["response"] = []
    with pytest.raises(ldap_auth.InvalidCredentials):
        ldap_auth.verify("ghost", ldap_settings(tmp_path))


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [("a*b", "a\\2ab"), ("a(b)", "a\\28b\\29"), ("a\\b", "a\\5cb")],
)
def test_escape_filter(raw, escaped):
    assert ldap_auth.escape_filter(raw) == escaped


def test_default_user_template_uses_base_dn(tmp_path):
    settings = ldap_settings(tmp_path, ldap_user_template="")
    assert ldap_auth.user_dn("alice", settings) == "uid=alice,dc=corp,dc=com"


def test_role_for_normalizes_dn_case(tmp_path):
    settings = ldap_settings(tmp_path, ldap_group_dn_admin="CN=Admins, DC=corp, DC=com")
    groups = ("cn=admins, dc=corp, dc=com",)
    assert ldap_auth.role_for("alice", groups, settings) == "admin"