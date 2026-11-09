"""按用户配额（契约 1.5）：默认值、单用户覆盖、提交校验、取消即释放。"""

from __future__ import annotations

import pytest
from app.config import Settings
from app.services.errors import ServiceError
from app.services.quotas import Quota, QuotaStore, Usage, check_quota, effective_quota
from conftest import FakeKubernetes, build_service, make_settings


def submit(service, name, owner="alice", gpus=1):
    return service.submit(make_spec(name, gpus=gpus), owner=owner)


def make_spec(name, gpus=1):
    from app.models import SubmitSpec

    return SubmitSpec(name=name, image="vla-smoke:latest", gpus=gpus, command="python /app/train.py")


def test_override_wins_over_default(tmp_path):
    store = QuotaStore(str(tmp_path / "q.db"))
    settings = Settings(api_token="t", default_max_gpus=8, default_max_active_jobs=4)
    assert effective_quota(settings, store.get_override("alice")) == Quota(max_gpus=8, max_active_jobs=4)
    store.set_override("alice", 2, None)
    assert effective_quota(settings, store.get_override("alice")) == Quota(max_gpus=2, max_active_jobs=None)


def test_override_is_persisted(tmp_path):
    path = str(tmp_path / "q.db")
    QuotaStore(path).set_override("alice", 3, 5)
    row = QuotaStore(path).get_override("alice")
    assert row["max_gpus"] == 3
    assert row["max_active_jobs"] == 5
    assert row["updated_at"].endswith("Z")


def test_quota_store_shares_history_db_file(tmp_path):
    """会话/配额表必须复用历史库同一个 SQLite 文件。"""
    settings = make_settings(tmp_path)
    service = build_service(settings)
    service.set_quota("alice", 1, 1)
    service.sessions.issue("alice", "user", 1)
    import sqlite3

    tables = {
        row[0]
        for row in sqlite3.connect(settings.db_path).execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"submissions", "cancellations", "sessions", "user_quotas"} <= tables


def test_check_quota_unlimited_never_raises():
    check_quota(Quota(), Usage(active_jobs=99, gpu_in_use=99), gpus=99)


def test_check_quota_gpu_message_matches_contract():
    with pytest.raises(ServiceError) as captured:
        check_quota(Quota(max_gpus=4), Usage(gpu_in_use=4), gpus=1)
    assert captured.value.status_code == 422
    assert captured.value.message == "GPU quota exceeded: 4 used of 4 allowed"


def test_check_quota_active_jobs_message_matches_contract():
    with pytest.raises(ServiceError) as captured:
        check_quota(Quota(max_active_jobs=2), Usage(active_jobs=2), gpus=1)
    assert captured.value.status_code == 422
    assert captured.value.message == "Active job quota exceeded: 2 of 2 allowed"


def test_check_quota_allows_exact_fit():
    check_quota(Quota(max_gpus=2, max_active_jobs=1), Usage(active_jobs=0, gpu_in_use=1), gpus=1)


def test_usage_counts_only_active_jobs(tmp_path):
    settings = make_settings(tmp_path)
    service = build_service(settings)
    submit(service, "a-active")
    submit(service, "a-done")
    service.kubernetes.jobs["a-done"]["status"] = {
        "conditions": [{"type": "Succeeded", "status": True, "lastTransitionTime": "2026-01-01T00:00:00Z"}]
    }
    submit(service, "b-other", owner="bob")

    usage = service.usage_for("alice")
    assert usage.active_jobs == 1
    assert usage.gpu_in_use == 1


def test_gpu_quota_blocks_submit_with_422(tmp_path):
    settings = make_settings(tmp_path)
    service = build_service(settings)
    service.set_quota("alice", 1, None)
    submit(service, "first")

    with pytest.raises(ServiceError) as captured:
        submit(service, "second")
    assert captured.value.status_code == 422
    assert captured.value.message == "GPU quota exceeded: 1 used of 1 allowed"


def test_active_job_quota_blocks_submit_with_422(tmp_path):
    settings = make_settings(tmp_path)
    service = build_service(settings)
    service.set_quota("alice", None, 1)
    submit(service, "first")

    with pytest.raises(ServiceError) as captured:
        submit(service, "second")
    assert captured.value.status_code == 422
    assert captured.value.message == "Active job quota exceeded: 1 of 1 allowed"


def test_default_quota_applies_without_override(tmp_path):
    settings = make_settings(tmp_path, default_max_gpus=1)
    service = build_service(settings)
    submit(service, "first")
    with pytest.raises(ServiceError) as captured:
        submit(service, "second")
    assert "GPU quota exceeded" in captured.value.message


def test_admin_is_also_limited_but_can_raise_own_quota(tmp_path):
    settings = make_settings(tmp_path, default_max_active_jobs=1)
    service = build_service(settings)
    submit(service, "root-1", owner="root")
    with pytest.raises(ServiceError):
        submit(service, "root-2", owner="root")
    service.set_quota("root", None, 5)
    assert submit(service, "root-2", owner="root")["owner"] == "root"


def test_cancel_releases_quota_immediately(tmp_path):
    settings = make_settings(tmp_path)
    service = build_service(settings)
    service.set_quota("alice", 1, 1)
    submit(service, "first")
    service.cancel("first")

    assert service.usage_for("alice") == Usage()
    second = submit(service, "second")
    assert second["owner"] == "alice"


def test_finished_job_releases_quota_after_cleanup(tmp_path):
    settings = make_settings(tmp_path)
    service = build_service(settings)
    service.set_quota("alice", 1, 1)
    submit(service, "first")
    service.kubernetes.jobs["first"]["status"] = {
        "conditions": [{"type": "Succeeded", "status": True, "lastTransitionTime": "2026-01-01T00:00:00Z"}]
    }
    # 已完成的实时任务不再占用额度（状态不是 QUEUED/RUNNING）。
    assert service.usage_for("alice").active_jobs == 0
    assert submit(service, "second")["status"] == "QUEUED"


def test_quota_is_per_user(tmp_path):
    settings = make_settings(tmp_path)
    service = build_service(settings)
    service.set_quota("alice", 1, 1)
    submit(service, "alice-1")
    assert submit(service, "bob-1", owner="bob")["owner"] == "bob"


def test_multi_gpu_request_counts_whole_request(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, allowed_gpus=(1, 2, 4), default_max_gpus=2)
    service = build_service(settings)
    assert submit(service, "two", gpus=2)["gpus"] == 2
    with pytest.raises(ServiceError) as captured:
        submit(service, "three", gpus=1)
    assert captured.value.message == "GPU quota exceeded: 2 used of 2 allowed"


def test_submit_via_api_returns_422_detail(tmp_path, app_client):
    settings = make_settings(tmp_path, auth_mode="off", auth_required=False, default_max_gpus=1)
    client, service = app_client(settings)
    with client:
        first = client.post("/api/v1/jobs", json={"name": "one", "gpus": 1})
        assert first.status_code == 201, first.text
        second = client.post("/api/v1/jobs", json={"name": "two", "gpus": 1})
        assert second.status_code == 422, second.text
        assert second.json() == {"detail": "GPU quota exceeded: 1 used of 1 allowed"}


def test_quota_endpoint_affects_submission(tmp_path, app_client, monkeypatch):
    settings = make_settings(
        tmp_path, auth_mode="ldap", auth_required=True, ldap_url="ldap://x", ldap_base_dn="dc=c"
    )
    from app.services import ldap_auth

    monkeypatch.setattr(ldap_auth, "authenticate", lambda u, p, s: ldap_auth.LDAPUser(username=u, role="admin"))
    client, _service = app_client(settings)
    with client:
        # 第一个登录者在没有任何配额配置时是 admin（测试里注入的角色）。
        login = client.post("/api/v1/auth/login", json={"username": "root", "password": "pw"}).json()
        root = {"Authorization": f"Bearer {login['token']}"}
        assert client.put("/api/v1/users/alice/quota", json={"max_active_jobs": 0}, headers=root).status_code == 200
        alice_login = client.post("/api/v1/auth/login", json={"username": "alice", "password": "pw"}).json()
        alice = {"Authorization": f"Bearer {alice_login['token']}"}
        response = client.post("/api/v1/jobs", json={"name": "blocked", "gpus": 1}, headers=alice)
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == "Active job quota exceeded: 0 of 0 allowed"


def test_clone_checks_quota(tmp_path):
    settings = make_settings(tmp_path)
    service = build_service(settings)
    service.set_quota("alice", 1, None)
    submit(service, "first")
    with pytest.raises(ServiceError) as captured:
        service.clone("first", "copy", owner="alice")
    assert captured.value.message == "GPU quota exceeded: 1 used of 1 allowed"


def test_usage_uses_single_pod_list(tmp_path):
    settings = make_settings(tmp_path)
    kube = FakeKubernetes()
    service = build_service(settings, kube)
    for index in range(3):
        service.submit(make_spec(f"job-{index}"), owner="alice")
    kube.pod_list_calls = 0
    service.usage_for("alice")
    assert kube.pod_list_calls == 1