"""鉴权：三种模式 × 令牌解析 / 过期 / 撤销（契约 1.1–1.4）。"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from app.auth import ANONYMOUS_USERNAME, TOKEN_USERNAME, AuthContext, may_access_job
from app.config import Settings
from app.services.sessions import SessionStore, hash_token, utc_now
from fastapi import HTTPException


def auth_settings(tmp_path, name: str = "case.db", **overrides) -> Settings:
    """各模式在同一个用例里可能同时用到，用不同库文件名避免串味。"""
    return Settings(
        api_token="test-token",
        db_path=str(tmp_path / name),
        gpu_metrics_url="",
        **overrides,
    )


def submit(client, name="auth-job", headers=None):
    return client.post(
        "/api/v1/jobs",
        json={"name": name, "gpus": 1, "command": "python /app/train.py"},
        headers=headers or {},
    )


# --- 模式 off -------------------------------------------------------------


def test_off_mode_anonymous_is_admin(app_client, tmp_path):
    settings = auth_settings(tmp_path, auth_mode="off", auth_required=False)
    client, _service = app_client(settings)
    with client:
        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["username"] == ANONYMOUS_USERNAME
        assert me.json()["role"] == "admin"
        assert me.json()["source"] == "off"
        # 免登录模式下不带任何令牌也能提交，任务归属于匿名身份。
        created = submit(client)
        assert created.status_code == 201, created.text
        assert created.json()["owner"] == ANONYMOUS_USERNAME


def test_auth_required_false_implies_off_mode():
    """v0.8 的 PLATFORM_AUTH_REQUIRED=false 必须等价于 off。"""
    settings = Settings(api_token="", auth_required=False)
    assert settings.auth_mode == "off"
    assert settings.auth_enabled is False


def test_off_mode_ignores_wrong_token(app_client, tmp_path):
    settings = auth_settings(tmp_path, auth_mode="off", auth_required=False)
    client, _service = app_client(settings)
    with client:
        assert client.get("/api/v1/jobs", headers={"Authorization": "Bearer nonsense"}).status_code == 200


# --- 模式 token -----------------------------------------------------------


def test_token_mode_requires_bearer(app_client, tmp_path):
    settings = auth_settings(tmp_path, auth_mode="token", auth_required=True)
    client, _service = app_client(settings)
    with client:
        missing = client.get("/api/v1/jobs")
        assert missing.status_code == 401
        # 401 的 JSON 形状必须与 v0.8 完全一致。
        assert missing.json() == {"detail": "Bearer token required"}
        assert missing.headers["www-authenticate"] == "Bearer"
        assert client.get("/api/v1/jobs", headers={"Authorization": "Bearer wrong"}).json() == {
            "detail": "Invalid bearer token"
        }
        # 非 bearer scheme 同样按"没带令牌"处理。
        assert client.get("/api/v1/jobs", headers={"Authorization": "Basic abc"}).json() == {
            "detail": "Bearer token required"
        }


def test_token_mode_accepts_static_token_and_identifies_admin(app_client, tmp_path):
    settings = auth_settings(tmp_path, auth_mode="token", auth_required=True)
    client, _service = app_client(settings)
    headers = {"Authorization": "Bearer test-token"}
    with client:
        me = client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200, me.text
        assert me.json()["username"] == TOKEN_USERNAME
        assert me.json()["role"] == "admin"
        assert me.json()["source"] == "token"
        assert me.json()["quotas"] == {
            "max_gpus": None,
            "max_active_jobs": None,
            "gpu_in_use": 0,
            "active_jobs": 0,
        }
        created = submit(client, headers=headers)
        assert created.status_code == 201, created.text
        assert created.json()["owner"] == TOKEN_USERNAME


@pytest.mark.parametrize("mode", ["token", "off"])
def test_login_outside_ldap_mode_returns_400(app_client, tmp_path, mode):
    settings = auth_settings(tmp_path, auth_mode=mode, auth_required=(mode == "token"))
    client, _service = app_client(settings)
    with client:
        response = client.post("/api/v1/auth/login", json={"username": "alice", "password": "secret"})
        assert response.status_code == 400
        assert response.json() == {"detail": f"login is not available in auth mode {mode}"}


def test_token_mode_session_token_does_not_authenticate(app_client, tmp_path):
    """会话令牌只在 ldap 模式下有效，token 模式只认静态令牌。"""
    settings = auth_settings(tmp_path, "token-session.db", auth_mode="token", auth_required=True)
    store = SessionStore(settings.db_path)
    issued = store.issue("alice", "user", 12)
    client, _service = app_client(settings)
    with client:
        session_headers = {"Authorization": f"Bearer {issued['token']}"}
        response = client.get("/api/v1/jobs", headers=session_headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid bearer token"}


# --- 会话令牌：持久化 / 过期 / 撤销 ---------------------------------------


def test_session_token_is_persisted_as_hash(tmp_path):
    store = SessionStore(str(tmp_path / "s.db"))
    issued = store.issue("alice", "user", 12)
    token = issued["token"]
    assert token not in store.path and token not in open(store.path, "rb").read().decode("latin-1")
    # 重开一个 store（模拟进程重启）后令牌仍然有效。
    reopened = SessionStore(str(tmp_path / "s.db"))
    session = reopened.lookup(token)
    assert session is not None
    assert session["username"] == "alice"
    assert session["role"] == "user"
    assert hash_token(token) != token


def test_session_token_expires(tmp_path):
    store = SessionStore(str(tmp_path / "s.db"))
    issued = store.issue("alice", "user", 1)
    later = utc_now() + timedelta(hours=2)
    assert store.lookup(issued["token"], now=later) is None
    # 过期会话被顺手清理。
    assert store.purge_expired(now=later) == 0


def test_session_purge_removes_expired_rows(tmp_path):
    store = SessionStore(str(tmp_path / "s.db"))
    store.issue("alice", "user", 1)
    store.issue("bob", "user", 48)
    removed = store.purge_expired(now=utc_now() + timedelta(hours=2))
    assert removed == 1
    assert set(store.known_users()) == {"bob"}


def test_session_revoke_immediately_invalidates(tmp_path):
    store = SessionStore(str(tmp_path / "s.db"))
    issued = store.issue("alice", "user", 12)
    assert store.revoke(issued["token"]) is True
    assert store.lookup(issued["token"]) is None
    assert store.revoke(issued["token"]) is False, "重复登出应幂等"


def test_ldap_session_token_authenticates_and_logout_revokes(app_client, tmp_path, monkeypatch):
    settings = auth_settings(tmp_path, auth_mode="ldap", auth_required=True, ldap_url="ldap://x", ldap_base_dn="dc=c")
    from app.services import ldap_auth

    monkeypatch.setattr(
        ldap_auth, "authenticate", lambda u, p, s: ldap_auth.LDAPUser(username=u, role="user")
    )

    def unreachable(username, _settings):
        raise ldap_auth.LDAPUnavailable("not needed")

    # 登录后的每个请求都会复查 LDAP；本用例只关心令牌语义，所以这里让复查失败，
    # 顺带验证"复查不可用时不打断已有会话"。
    monkeypatch.setattr(ldap_auth, "verify", unreachable)
    client, _service = app_client(settings)
    with client:
        login = client.post("/api/v1/auth/login", json={"username": "alice", "password": "pw"})
        assert login.status_code == 200, login.text
        body = login.json()
        assert body["user"] == {"username": "alice", "role": "user", "source": "ldap"}
        assert body["token"] and body["expires_at"].endswith("Z")
        assert "password" not in login.text

        headers = {"Authorization": f"Bearer {body['token']}"}
        assert client.get("/api/v1/auth/me", headers=headers).json()["username"] == "alice"
        assert submit(client, "alice-job", headers=headers).status_code == 201
        quotas = client.get("/api/v1/auth/me", headers=headers).json()["quotas"]
        assert quotas == {"max_gpus": None, "max_active_jobs": None, "gpu_in_use": 1, "active_jobs": 1}

        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
        after = client.get("/api/v1/auth/me", headers=headers)
        assert after.status_code == 401
        assert after.json() == {"detail": "Invalid bearer token"}


def test_ldap_mode_without_token_returns_401(app_client, tmp_path):
    settings = auth_settings(tmp_path, auth_mode="ldap", auth_required=True, ldap_url="ldap://x", ldap_base_dn="dc=c")
    client, _service = app_client(settings)
    with client:
        response = client.get("/api/v1/jobs")
        assert response.status_code == 401
        assert response.json() == {"detail": "Bearer token required"}


# --- 角色过滤 -------------------------------------------------------------


def make_session(service, username, role="user"):
    issued = service.sessions.issue(username, role, 12)
    return {"Authorization": f"Bearer {issued['token']}"}


def ldap_client(app_client, tmp_path, monkeypatch, roles=None, **overrides):
    """ldap 模式的 TestClient；roles 可选地按用户名注入角色（模拟组成员）。"""
    settings = auth_settings(
        tmp_path, auth_mode="ldap", auth_required=True, ldap_url="ldap://x", ldap_base_dn="dc=c", **overrides
    )
    from app.services import ldap_auth

    roles = roles or {}

    def fake_authenticate(username, password, _settings):
        return ldap_auth.LDAPUser(username=username, role=roles.get(username, "user"))

    monkeypatch.setattr(ldap_auth, "authenticate", fake_authenticate)
    return app_client(settings)


def test_user_sees_only_own_jobs(app_client, tmp_path, monkeypatch):
    client, service = ldap_client(app_client, tmp_path, monkeypatch)
    with client:
        alice = make_session(service, "alice")
        bob = make_session(service, "bob")
        admin = make_session(service, "root", role="admin")
        assert submit(client, "alice-a", alice).status_code == 201
        assert submit(client, "bob-a", bob).status_code == 201

        alice_list = client.get("/api/v1/jobs", headers=alice).json()
        assert [item["name"] for item in alice_list["items"]] == ["alice-a"]
        assert alice_list["total"] == 1

        admin_list = client.get("/api/v1/jobs", headers=admin).json()
        assert {item["name"] for item in admin_list["items"]} == {"alice-a", "bob-a"}


def test_user_cannot_read_or_cancel_others_job(app_client, tmp_path, monkeypatch):
    client, service = ldap_client(app_client, tmp_path, monkeypatch)
    with client:
        alice = make_session(service, "alice")
        bob = make_session(service, "bob")
        assert submit(client, "alice-secret", alice).status_code == 201

        # 不泄露他人任务是否存在：统一 404。
        for path in ("/api/v1/jobs/alice-secret", "/api/v1/jobs/alice-secret/logs", "/api/v1/jobs/alice-secret/events"):
            response = client.get(path, headers=bob)
            assert response.status_code == 404, path
            assert response.json() == {"detail": "job alice-secret not found"}
        assert client.delete("/api/v1/jobs/alice-secret", headers=bob).status_code == 404
        assert client.post("/api/v1/jobs/alice-secret/clone", json={"name": "bob-copy"}, headers=bob).status_code == 404
        assert client.get("/api/v1/jobs/alice-secret", headers=alice).status_code == 200


def test_admin_can_cancel_any_job(app_client, tmp_path, monkeypatch):
    client, service = ldap_client(app_client, tmp_path, monkeypatch)
    with client:
        alice = make_session(service, "alice")
        admin = make_session(service, "root", role="admin")
        assert submit(client, "alice-job2", alice).status_code == 201
        response = client.delete("/api/v1/jobs/alice-job2", headers=admin)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "CANCELLED"


def test_clone_assigns_new_owner(app_client, tmp_path, monkeypatch):
    client, service = ldap_client(app_client, tmp_path, monkeypatch)
    with client:
        alice = make_session(service, "alice")
        admin = make_session(service, "root", role="admin")
        assert submit(client, "alice-base", alice).status_code == 201
        cloned = client.post("/api/v1/jobs/alice-base/clone", json={"name": "root-copy"}, headers=admin)
        assert cloned.status_code == 201, cloned.text
        assert cloned.json()["owner"] == "root"


def test_legacy_job_without_owner_is_hidden_from_users():
    """v0.8 老任务 owner 为空串：普通用户看不到，admin 仍可见。"""
    user = AuthContext(username="alice", role="user", source="ldap")
    admin = AuthContext(username="root", role="admin", source="ldap")
    assert may_access_job(user, "") is False
    assert may_access_job(admin, "") is True
    assert may_access_job(user, "alice") is True
    assert may_access_job(user, "bob") is False


def test_legacy_submission_owner_backfilled_for_visibility(app_client, tmp_path, monkeypatch):
    """实时任务注解里没有 owner 时，用提交历史里的 owner 做过滤。"""
    client, service = ldap_client(app_client, tmp_path, monkeypatch)
    with client:
        alice = make_session(service, "alice")
        assert submit(client, "legacy-live", alice).status_code == 201
        # 模拟 v0.8 对象：注解里的 spec 没有 owner 字段。
        annotations = service.kubernetes.jobs["legacy-live"]["metadata"]["annotations"]
        annotations.pop("vla-platform/owner", None)
        annotations["vla-platform/spec"] = annotations["vla-platform/spec"].replace(',"owner":"alice"', "")

        alice_list = client.get("/api/v1/jobs", headers=alice).json()
        assert [item["name"] for item in alice_list["items"]] == ["legacy-live"]
        assert alice_list["items"][0]["owner"] == "alice"


# --- admin 端点 -----------------------------------------------------------


def test_users_endpoint_requires_admin(app_client, tmp_path, monkeypatch):
    client, service = ldap_client(app_client, tmp_path, monkeypatch)
    with client:
        alice = make_session(service, "alice")
        response = client.get("/api/v1/users", headers=alice)
        assert response.status_code == 403
        assert response.json() == {"detail": "admin privileges required"}
        quota = client.put("/api/v1/users/alice/quota", json={"max_gpus": 1}, headers=alice)
        assert quota.status_code == 403


def test_users_lists_quotas_and_usage(app_client, tmp_path, monkeypatch):
    client, service = ldap_client(app_client, tmp_path, monkeypatch)
    with client:
        alice = make_session(service, "alice")
        admin = make_session(service, "root", role="admin")
        assert submit(client, "alice-used", alice).status_code == 201
        put = client.put(
            "/api/v1/users/alice/quota", json={"max_gpus": 4, "max_active_jobs": 2}, headers=admin
        )
        assert put.status_code == 200, put.text
        assert put.json() == {
            "username": "alice",
            "quota": {"max_gpus": 4, "max_active_jobs": 2, "gpu_in_use": 1, "active_jobs": 1},
        }

        body = client.get("/api/v1/users", headers=admin).json()
        by_name = {item["username"]: item for item in body["items"]}
        assert set(by_name) >= {"alice", "root"}
        alice_row = by_name["alice"]
        assert alice_row["role"] == "user"
        assert alice_row["source"] == "ldap"
        assert alice_row["last_seen_at"]
        assert alice_row["active_jobs"] == 1
        assert alice_row["gpu_in_use"] == 1
        assert alice_row["quota"] == {"max_gpus": 4, "max_active_jobs": 2}
        assert by_name["root"]["quota"] == {"max_gpus": None, "max_active_jobs": None}


def test_put_quota_rejects_invalid_payload(app_client, tmp_path, monkeypatch):
    client, service = ldap_client(app_client, tmp_path, monkeypatch)
    with client:
        admin = make_session(service, "root", role="admin")
        assert client.put("/api/v1/users/alice/quota", json={"max_gpus": -1}, headers=admin).status_code == 422
        assert client.put("/api/v1/users/alice/quota", json={"nope": 1}, headers=admin).status_code == 422


def test_health_reports_auth_required_per_mode(app_client, tmp_path):
    settings = auth_settings(tmp_path, auth_mode="ldap", auth_required=True, ldap_url="ldap://x", ldap_base_dn="dc=c")
    client, _service = app_client(settings)
    with client:
        # v0.9：/health 在 v0.8 的 status / auth_required 之外再暴露 auth_mode，
        # 前端未登录时靠它决定显示令牌登录还是 LDAP 登录。
        body = client.get("/api/v1/health").json()
        assert body == {"status": "ok", "auth_required": True, "auth_mode": "ldap"}


def test_config_exposes_max_nodes(app_client, tmp_path, monkeypatch):
    client, service = ldap_client(app_client, tmp_path, monkeypatch, max_nodes=2)
    with client:
        alice = make_session(service, "alice")
        body = client.get("/api/v1/config", headers=alice).json()
        assert body["max_nodes"] == 2
        assert body["auth_mode"] == "ldap"


# --- from_env -------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"PLATFORM_API_TOKEN": "t"}, "token"),
        ({"PLATFORM_API_TOKEN": "t", "PLATFORM_AUTH_MODE": "ldap", "PLATFORM_LDAP_URL": "ldap://x",
          "PLATFORM_LDAP_BASE_DN": "dc=c"}, "ldap"),
        ({"PLATFORM_API_TOKEN": "t", "PLATFORM_AUTH_MODE": "off"}, "off"),
        ({"PLATFORM_AUTH_REQUIRED": "false"}, "off"),
        # 两者同时设置时契约规定 off 优先。
        ({"PLATFORM_AUTH_REQUIRED": "false", "PLATFORM_AUTH_MODE": "token", "PLATFORM_API_TOKEN": "t"}, "off"),
    ],
)
def test_from_env_auth_mode(monkeypatch, env, expected):
    for key in list(os.environ):
        if key.startswith("PLATFORM_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = Settings.from_env()
    assert settings.auth_mode == expected
    assert settings.auth_enabled is (expected != "off")


def test_from_env_rejects_unknown_auth_mode(monkeypatch):
    monkeypatch.delenv("PLATFORM_AUTH_REQUIRED", raising=False)
    monkeypatch.setenv("PLATFORM_API_TOKEN", "t")
    monkeypatch.setenv("PLATFORM_AUTH_MODE", "saml")
    with pytest.raises(RuntimeError, match="PLATFORM_AUTH_MODE"):
        Settings.from_env()


def test_from_env_ldap_requires_url_and_base_dn(monkeypatch):
    for key in list(os.environ):
        if key.startswith("PLATFORM_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLATFORM_AUTH_MODE", "ldap")
    with pytest.raises(RuntimeError, match="PLATFORM_LDAP_URL"):
        Settings.from_env()


def test_from_env_token_mode_requires_token(monkeypatch):
    for key in list(os.environ):
        if key.startswith("PLATFORM_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLATFORM_AUTH_MODE", "token")
    with pytest.raises(RuntimeError, match="PLATFORM_API_TOKEN"):
        Settings.from_env()


def test_from_env_parses_v09_settings(monkeypatch):
    for key in list(os.environ):
        if key.startswith("PLATFORM_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLATFORM_API_TOKEN", "t")
    monkeypatch.setenv("PLATFORM_SESSION_TTL_HOURS", "6")
    monkeypatch.setenv("PLATFORM_DEFAULT_MAX_GPUS", "8")
    monkeypatch.setenv("PLATFORM_DEFAULT_MAX_ACTIVE_JOBS", "3")
    monkeypatch.setenv("PLATFORM_MAX_NODES", "2")
    settings = Settings.from_env()
    assert settings.session_ttl_hours == 6
    assert settings.default_max_gpus == 8
    assert settings.default_max_active_jobs == 3
    assert settings.max_nodes == 2
    assert settings.auth_enabled is True


def test_from_env_defaults_are_unlimited(monkeypatch):
    for key in list(os.environ):
        if key.startswith("PLATFORM_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLATFORM_API_TOKEN", "t")
    settings = Settings.from_env()
    assert settings.default_max_gpus is None
    assert settings.default_max_active_jobs is None
    assert settings.max_nodes == 1
    assert settings.session_ttl_hours == 12


def test_require_admin_helper_shape():
    with pytest.raises(HTTPException) as captured:
        from app.auth import require_admin

        require_admin(AuthContext(username="u", role="user", source="ldap"))
    assert captured.value.status_code == 403