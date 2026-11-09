"""审计日志测试。

重点不只是「记下来了」，还有两条安全属性：
1. 审计失败绝不能影响业务（提交任务不能因为写审计失败而 500）；
2. 需要 admin 的路由不能因为接入审计而放开权限（曾经踩过的坑）。
"""

from __future__ import annotations

import json

import pytest
from app.config import Settings
from app.services.audit import ACTIONS, AuditStore
from fastapi.testclient import TestClient


@pytest.fixture
def store(tmp_path):
    return AuditStore(str(tmp_path / "audit.db"))


# --------------------------------------------------------------------------
# 存储层
# --------------------------------------------------------------------------
def test_record_and_list(store):
    store.record("job.submit", actor="alice", target="job1", detail={"gpus": 1})

    result = store.list()

    assert result["total"] == 1
    item = result["items"][0]
    assert item["actor"] == "alice"
    assert item["action"] == "job.submit"
    assert item["target"] == "job1"
    assert item["detail"] == {"gpus": 1}


def test_list_is_newest_first(store):
    for name in ("a", "b", "c"):
        store.record("job.submit", actor="alice", target=name)

    items = store.list()["items"]

    assert [item["target"] for item in items] == ["c", "b", "a"]


def test_filter_by_action_actor_target(store):
    store.record("job.submit", actor="alice", target="j1")
    store.record("job.cancel", actor="bob", target="j1")
    store.record("job.submit", actor="bob", target="j2")

    assert store.list(action="job.submit")["total"] == 2
    assert store.list(actor="bob")["total"] == 2
    assert store.list(target="j1")["total"] == 2
    assert store.list(action="job.cancel", actor="alice")["total"] == 0


def test_unknown_action_is_ignored(store):
    store.record("not.an.action", actor="alice", target="x")

    assert store.list()["total"] == 0


def test_anonymous_actor_falls_back(store):
    store.record("job.submit", actor="", target="j1")

    assert store.list()["items"][0]["actor"] == "anonymous"


def test_audit_failure_does_not_raise(store, monkeypatch):
    """写审计失败必须静默降级，不能把异常抛给调用方。"""

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "_connect", boom)

    store.record("job.submit", actor="alice", target="j1")  # 不应抛异常


def test_actions_lists_distinct(store):
    store.record("job.submit", actor="a", target="1")
    store.record("job.submit", actor="a", target="2")
    store.record("job.cancel", actor="a", target="1")

    assert store.actions() == ["job.cancel", "job.submit"]


def test_limit_is_capped(store):
    for index in range(10):
        store.record("job.submit", actor="a", target=f"j{index}")

    assert len(store.list(limit=3)["items"]) == 3
    assert len(store.list(limit=9999)["items"]) == 10


def test_all_actions_are_whitelisted():
    assert {"job.submit", "job.cancel", "run.create"} <= ACTIONS


# --------------------------------------------------------------------------
# 接口层
# --------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_AUTH_MODE", "off")
    monkeypatch.setenv("PLATFORM_DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("PLATFORM_GPU_METRICS_URL", "")
    monkeypatch.setenv("PLATFORM_DATASETS_MOUNT_PATH", str(tmp_path / "datasets"))

    from app.main import create_app
    from app.services.platform import PlatformService  # noqa: F401

    settings = Settings(
        api_token="t",
        auth_required=False,
        auth_mode="off",
        db_path=str(tmp_path / "a.db"),
        gpu_metrics_url="",
        datasets_mount_path=str(tmp_path / "datasets"),
    )
    app = create_app(settings)

    class FakeKube:
        def __init__(self):
            self.jobs: dict = {}

        def get_job(self, name):
            return self.jobs.get(name)

        def list_jobs(self):
            return list(self.jobs.values())

        def create_job(self, body):
            name = body["metadata"]["name"]
            job = {
                "metadata": {
                    "name": name,
                    "uid": f"u-{name}",
                    "labels": {},
                    "annotations": body["metadata"].get("annotations", {}),
                    "creationTimestamp": "2026-01-01T00:00:00Z",
                },
                "spec": body.get("spec", {}),
                "status": {},
            }
            self.jobs[name] = job
            return job

        def delete_job(self, name):
            self.jobs.pop(name, None)

        def list_namespace_pods(self):
            return []

        def list_job_pods(self, name):
            return []

        def list_workloads(self):
            return []

        def list_nodes(self):
            return []

        def list_training_pods(self):
            return []

    app.state.service.kubernetes = FakeKube()
    with TestClient(app) as test_client:
        yield test_client


def test_submit_records_audit(client):
    response = client.post(
        "/api/v1/jobs",
        json={"name": "audited-1", "image": "vla-smoke:latest", "gpus": 1, "command": "true"},
    )
    assert response.status_code == 201, response.text

    body = client.get("/api/v1/audit").json()

    assert body["total"] >= 1
    actions = [item["action"] for item in body["items"]]
    assert "job.submit" in actions
    submit = next(item for item in body["items"] if item["action"] == "job.submit")
    assert submit["target"] == "audited-1"
    assert submit["detail"]["image"] == "vla-smoke:latest"


def test_cancel_records_audit(client):
    client.post("/api/v1/jobs", json={"name": "cancel-me", "image": "x", "gpus": 1, "command": "true"})
    assert client.delete("/api/v1/jobs/cancel-me").status_code == 200

    actions = [item["action"] for item in client.get("/api/v1/audit").json()["items"]]

    assert "job.cancel" in actions


def test_audit_filters_via_query(client):
    client.post("/api/v1/jobs", json={"name": "q-1", "image": "x", "gpus": 1, "command": "true"})
    client.delete("/api/v1/jobs/q-1")

    only_cancel = client.get("/api/v1/audit?action=job.cancel").json()

    assert {item["action"] for item in only_cancel["items"]} == {"job.cancel"}


def test_audit_response_includes_action_types(client):
    client.post("/api/v1/jobs", json={"name": "types-1", "image": "x", "gpus": 1, "command": "true"})

    body = client.get("/api/v1/audit").json()

    assert "job.submit" in body["actions"]


def test_audit_detail_is_json_serializable(client):
    client.post("/api/v1/jobs", json={"name": "json-1", "image": "x", "gpus": 1, "command": "true"})

    item = client.get("/api/v1/audit").json()["items"][0]

    json.dumps(item["detail"])  # 不应抛异常

def test_anonymous_actor_records_client_ip(client):
    """免登录模式下所有人都是 anonymous，审计要退而记录来源 IP。

    否则「谁取消了任务」这个问题永远无法回答，审计形同虚设。
    """
    client.post(
        "/api/v1/jobs",
        json={"name": "ip-1", "image": "x", "gpus": 1, "command": "true"},
        headers={"X-Forwarded-For": "10.1.2.3, 10.0.0.1"},
    )

    item = next(i for i in client.get("/api/v1/audit").json()["items"] if i["target"] == "ip-1")

    # 免登录模式下必须带来源，便于追溯；TestClient 的默认 host 是 "testclient"
    assert item["actor"].startswith("anonymous@")
    assert len(item["actor"]) > len("anonymous@")


def test_actor_uses_username_when_authenticated():
    """有真实用户名时用用户名，不加 IP 后缀。"""
    from app.auth import AuthContext, _actor_label

    class FakeClient:
        host = "10.9.9.9"

    class FakeRequest:
        headers: dict = {}
        client = FakeClient()

    user = AuthContext(username="alice", role="user", source="ldap")

    assert _actor_label(FakeRequest(), user) == "alice"


def test_actor_falls_back_to_client_host():
    from app.auth import AuthContext, _actor_label

    class FakeClient:
        host = "172.16.1.5"

    class FakeRequest:
        headers: dict = {}
        client = FakeClient()

    anonymous = AuthContext(username="anonymous", role="admin", source="off")

    assert _actor_label(FakeRequest(), anonymous) == "anonymous@172.16.1.5"

def test_run_create_records_actor(client, tmp_path):
    """一键训练（POST /runs）是主路径，审计必须记录操作者。

    曾经漏掉这里：jobs 三个路由改成了 set_actor，但 routes_runs 的 create_run
    还是 require_user，导致走「选数据集→点开始」这个主流程时审计仍是 anonymous。
    """
    dataset_dir = tmp_path / "datasets" / "ds1"
    (dataset_dir / "meta").mkdir(parents=True)
    import json

    (dataset_dir / "meta" / "info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v3.0",
                "robot_type": "test",
                "total_episodes": 20,
                "total_frames": 2000,
                "total_tasks": 1,
                "fps": 30,
                "features": {
                    "observation.images.cam": {"dtype": "video", "shape": [480, 640, 3]},
                    "observation.state": {"dtype": "float32", "shape": [7]},
                    "action": {"dtype": "float32", "shape": [7]},
                },
            }
        ),
        encoding="utf-8",
    )
    (dataset_dir / "data" / "chunk-000").mkdir(parents=True)
    (dataset_dir / "data" / "chunk-000" / "file-000.parquet").write_bytes(b"PAR1")
    client.get("/api/v1/datasets?refresh=true")

    # 用仓库里真实存在的配方（配方目录随代码发布，测试环境也有）
    recipes = client.get("/api/v1/recipes").json()["items"]
    assert recipes, "仓库里应当有内置配方"

    response = client.post(
        "/api/v1/runs",
        json={"recipe_id": recipes[0]["id"], "dataset_id": "ds1"},
        headers={"X-Forwarded-For": "10.5.5.5"},
    )
    assert response.status_code == 201, response.text

    target = response.json()["name"]
    item = next(i for i in client.get("/api/v1/audit").json()["items"] if i["target"] == target)

    assert item["actor"].startswith("anonymous@"), item["actor"]
    assert "anonymous@" in item["actor"]

