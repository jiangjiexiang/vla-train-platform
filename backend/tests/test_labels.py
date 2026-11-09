"""任务标签、owner / nodes 字段与 v0.8 老任务兼容（契约 1.6 / 1.7 / 2.6–2.7）。"""

from __future__ import annotations

import json
import sqlite3

import pytest
from app.config import Settings
from app.history import HistoryStore
from app.models import SubmitRequest, SubmitSpec, check_labels
from app.services.errors import ServiceError
from app.templates.pytorchjob import render_pytorch_job
from conftest import build_service
from pydantic import ValidationError


def make_settings(tmp_path, **overrides) -> Settings:
    values = {"api_token": "test-token", "db_path": str(tmp_path / "labels.db"), "gpu_metrics_url": ""}
    values.update(overrides)
    return Settings(**values)


def payload(name="labelled", **overrides):
    data = {"name": name, "gpus": 1, "command": "python /app/train.py"}
    data.update(overrides)
    return data


def make_spec(name="exp001", **overrides):
    data = {
        "name": name,
        "image": "vla-smoke:latest",
        "gpus": 1,
        "queue": "default",
        "priority": "normal",
        "command": "python /app/train.py",
    }
    data.update(overrides)
    return SubmitSpec(**data)


# --- 模型校验 -------------------------------------------------------------


def test_labels_default_to_empty():
    assert make_spec().labels == {}
    assert make_spec().owner == ""
    assert make_spec().nodes == 1
    assert make_spec().gpus_per_node is None


@pytest.mark.parametrize(
    "labels",
    [
        {"team": "perception"},
        {"exp": "astra-2.0"},
        {"owner": "alice", "stage": "pretrain"},
        {"a.b-c": "1"},
    ],
)
def test_valid_labels_accepted(labels):
    assert make_spec(labels=labels).labels == labels


@pytest.mark.parametrize(
    ("labels", "reason"),
    [
        ({"Team": "x"}, "uppercase key"),
        ({"team": "Perception"}, "uppercase value"),
        ({"team": "-x"}, "leading hyphen"),
        ({"team": "x-"}, "trailing hyphen"),
        ({"team_1": "x"}, "underscore"),
        ({"team": "x" * 64}, "value too long"),
        ({"team": "perception", "k": "a" * 250}, "total bytes over 256"),
    ],
)
def test_invalid_labels_rejected(labels, reason):
    with pytest.raises(ValidationError):
        make_spec(labels=labels)


def test_labels_size_limit_is_checked_at_submit():
    labels = {f"k{index}": "v" * 40 for index in range(8)}
    with pytest.raises(ValueError, match="256"):
        check_labels(labels)
    check_labels({"a" * 63: "b" * 63})


def test_submit_request_nodes_gt_one_computes_gpus():
    request = SubmitRequest(name="multi", nodes=2, gpus_per_node=1)
    spec = request.with_defaults("img", "cmd")
    assert spec.nodes == 2
    assert spec.gpus_per_node == 1
    assert spec.gpus == 2


def test_submit_request_requires_gpus_for_single_node():
    with pytest.raises(ValidationError):
        SubmitRequest(name="single")


def test_submit_request_requires_gpus_per_node_for_multi_node():
    with pytest.raises(ValidationError):
        SubmitRequest(name="multi", nodes=2)


def test_submit_request_gpus_fields_optional():
    """gpus 在请求里可省略（多机时服务端计算），但 SubmitSpec 仍必填。"""
    assert SubmitRequest.model_fields["gpus"].is_required() is False
    assert SubmitSpec.model_fields["gpus"].is_required() is True


# --- 注解渲染 -------------------------------------------------------------


def test_labels_rendered_as_annotations(settings):
    body = render_pytorch_job(make_spec(labels={"team": "perception"}), "2026-01-01T00:00:00Z", settings)
    annotations = body["metadata"]["annotations"]
    assert annotations["vla-platform/user-label-team"] == "perception"
    # 不参与调度：不进 label，也不进 Kueue 的选择器。
    assert "team" not in body["metadata"]["labels"]
    assert all(not key.startswith("vla-platform/user-label-") for key in body["metadata"]["labels"])


def test_owner_rendered_as_annotation(settings):
    body = render_pytorch_job(make_spec(owner="alice"), "2026-01-01T00:00:00Z", settings)
    assert body["metadata"]["annotations"]["vla-platform/owner"] == "alice"
    stored = json.loads(body["metadata"]["annotations"]["vla-platform/spec"])
    assert stored["owner"] == "alice"
    assert stored["labels"] == {}
    assert stored["nodes"] == 1


def test_no_owner_annotation_for_legacy_spec(settings):
    body = render_pytorch_job(make_spec(), "2026-01-01T00:00:00Z", settings)
    assert "vla-platform/owner" not in body["metadata"]["annotations"]


# --- 读老任务 / 老记录 ----------------------------------------------------


def test_old_spec_without_new_fields_parses():
    old = {
        "name": "legacy",
        "image": "vla-smoke:latest",
        "gpus": 1,
        "queue": "default",
        "priority": "normal",
        "command": "python /app/train.py",
        "env": {},
        "max_retries": 0,
        "termination_grace_period_seconds": 30,
    }
    spec = SubmitSpec.model_validate(old)
    assert spec.owner == ""
    assert spec.labels == {}
    assert spec.nodes == 1


def test_legacy_job_response_backfills_defaults(tmp_path):
    """v0.8 对象注解里的 spec 没有新字段，响应必须回填而不是 500。"""
    settings = make_settings(tmp_path)
    service = build_service(settings)
    legacy_spec = {
        "name": "old-job",
        "image": "vla-smoke:latest",
        "gpus": 1,
        "queue": "default",
        "priority": "normal",
        "command": "python /app/train.py",
        "env": {"A": "1"},
        "max_retries": 0,
        "termination_grace_period_seconds": 30,
    }
    service.kubernetes.jobs["old-job"] = {
        "metadata": {
            "name": "old-job",
            "uid": "uid-old",
            "labels": {},
            "annotations": {
                "vla-platform/submitted-at": "2025-01-01T00:00:00Z",
                "vla-platform/spec": json.dumps(legacy_spec),
            },
            "creationTimestamp": "2025-01-01T00:00:00Z",
        },
        "spec": {},
        "status": {},
    }

    job = service.get_job("old-job")
    assert job["owner"] == ""
    assert job["labels"] == {}
    assert job["nodes"] == 1
    assert job["gpu_type"] == "nvidia.com/gpu"
    assert job["env"] == {"A": "1"}

    listed = service.list_jobs()["items"]
    assert [item["name"] for item in listed] == ["old-job"]


def test_legacy_history_record_without_new_fields(tmp_path):
    path = str(tmp_path / "legacy.db")
    old_spec = {
        "name": "oldjob",
        "image": "vla-smoke:latest",
        "gpus": 1,
        "queue": "default",
        "priority": "normal",
        "command": "true",
        "env": {},
        "max_retries": 0,
        "termination_grace_period_seconds": 30,
    }
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE cancellations (
                name TEXT PRIMARY KEY,
                spec_json TEXT NOT NULL,
                created_at TEXT,
                cancelled_at TEXT NOT NULL,
                status TEXT
            );
            CREATE TABLE submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                spec_json TEXT NOT NULL,
                submitted_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO cancellations(name, spec_json, created_at, cancelled_at, status) VALUES (?, ?, ?, ?, ?)",
            ("oldjob", json.dumps(old_spec), "2025-01-01T00:00:00Z", "2025-01-01T01:00:00Z", "SUCCEEDED"),
        )

    store = HistoryStore(path)
    record = store.get_cancellation("oldjob")
    assert record["owner"] == ""
    assert record["spec"].labels == {}
    assert record["status"] == "SUCCEEDED"

    settings = make_settings(tmp_path, db_path=path)
    service = build_service(settings)
    job = service.get_job("oldjob")
    assert job["owner"] == ""
    assert job["labels"] == {}
    assert job["nodes"] == 1
    assert job["status"] == "SUCCEEDED"


def test_owner_column_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "migrate.db")
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE cancellations (
                name TEXT PRIMARY KEY,
                spec_json TEXT NOT NULL,
                created_at TEXT,
                cancelled_at TEXT NOT NULL,
                status TEXT
            );
            CREATE TABLE submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                spec_json TEXT NOT NULL,
                submitted_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO cancellations(name, spec_json, created_at, cancelled_at, status) VALUES (?, ?, ?, ?, ?)",
            ("oldjob", make_spec("oldjob").model_dump_json(), None, "2025-01-01T01:00:00Z", "CANCELLED"),
        )

    HistoryStore(path)
    store = HistoryStore(path)  # 二次打开不能报错（幂等迁移）
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(cancellations)")}
    assert "owner" in columns
    assert store.get_cancellation("oldjob")["owner"] == ""


def test_history_roundtrips_new_fields(tmp_path):
    store = HistoryStore(str(tmp_path / "h.db"))
    spec = make_spec(owner="alice", labels={"team": "perception"}, nodes=1)
    store.record_submission(spec, "2026-01-01T00:00:00Z")
    loaded, _ = store.latest_submission("exp001")
    assert loaded.owner == "alice"
    assert loaded.labels == {"team": "perception"}

    record = store.mark_cancelled(spec, "2026-01-01T00:00:00Z")
    assert record["owner"] == "alice"
    assert [item["name"] for item in store.list_cancellations(owner="alice")] == ["exp001"]
    assert store.list_cancellations(owner="bob") == []


# --- HTTP 层 --------------------------------------------------------------


def test_submit_accepts_labels_and_returns_them(app_client, tmp_path):
    settings = make_settings(tmp_path, auth_mode="off", auth_required=False)
    client, service = app_client(settings)
    with client:
        response = client.post("/api/v1/jobs", json=payload(labels={"team": "perception"}))
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["labels"] == {"team": "perception"}
        assert body["owner"] == "anonymous"
        assert body["nodes"] == 1
        assert body["gpu_type"] == "nvidia.com/gpu"
        assert (
            service.kubernetes.jobs["labelled"]["metadata"]["annotations"]["vla-platform/user-label-team"]
            == "perception"
        )


@pytest.mark.parametrize("labels", [{"Bad": "x"}, {"team": "X"}, {"a_b": "c"}])
def test_submit_invalid_labels_returns_422(app_client, tmp_path, labels):
    settings = make_settings(tmp_path, auth_mode="off", auth_required=False)
    client, _service = app_client(settings)
    with client:
        assert client.post("/api/v1/jobs", json=payload(labels=labels)).status_code == 422


def test_submit_too_many_label_bytes_returns_422(app_client, tmp_path):
    settings = make_settings(tmp_path, auth_mode="off", auth_required=False)
    client, _service = app_client(settings)
    labels = {f"k{index}": "v" * 60 for index in range(6)}
    with client:
        response = client.post("/api/v1/jobs", json=payload(labels=labels))
        assert response.status_code == 422
        assert "256" in response.json()["detail"]


def test_submit_multinode_rejected_when_disabled(app_client, tmp_path):
    settings = make_settings(tmp_path, auth_mode="off", auth_required=False)
    client, _service = app_client(settings)
    with client:
        response = client.post("/api/v1/jobs", json={"name": "multi", "nodes": 2, "gpus_per_node": 1})
        assert response.status_code == 422
        assert response.json() == {"detail": "multi-node training is not enabled"}


def test_submit_multinode_rejected_without_gpus_per_node(app_client, tmp_path):
    settings = make_settings(tmp_path, auth_mode="off", auth_required=False, max_nodes=2)
    client, _service = app_client(settings)
    with client:
        assert client.post("/api/v1/jobs", json={"name": "multi", "nodes": 2}).status_code == 422


def test_clone_carries_labels_and_new_owner(app_client, tmp_path):
    settings = make_settings(tmp_path, auth_mode="off", auth_required=False)
    client, _service = app_client(settings)
    with client:
        assert client.post("/api/v1/jobs", json=payload("src", labels={"team": "perception"})).status_code == 201
        cloned = client.post("/api/v1/jobs/src/clone", json={"name": "copy"})
        assert cloned.status_code == 201, cloned.text
        assert cloned.json()["labels"] == {"team": "perception"}
        assert cloned.json()["owner"] == "anonymous"


def test_clone_of_legacy_job_keeps_defaults(tmp_path):
    """老任务的 spec 缺少新字段时仍可克隆（默认值补齐后重新提交）。"""
    settings = make_settings(tmp_path)
    service = build_service(settings)
    service.submit(make_spec("src", owner="alice"), owner="alice")
    annotations = service.kubernetes.jobs["src"]["metadata"]["annotations"]
    annotations["vla-platform/spec"] = annotations["vla-platform/spec"].replace(',"owner":"alice"', "")

    cloned = service.clone("src", "copy", owner="bob")
    assert cloned["owner"] == "bob"
    assert cloned["labels"] == {}


def test_clone_rejects_spec_with_unknown_field(tmp_path):
    """克隆会重新提交，因此不宽容：未知字段报 422 而不是悄悄丢掉。"""
    settings = make_settings(tmp_path)
    service = build_service(settings)
    service.submit(make_spec("src"), owner="alice")
    annotations = service.kubernetes.jobs["src"]["metadata"]["annotations"]
    spec = json.loads(annotations["vla-platform/spec"])
    spec["privileged"] = True
    annotations["vla-platform/spec"] = json.dumps(spec)

    # 读取（详情）仍然可用。
    assert service.get_job("src")["name"] == "src"
    with pytest.raises(ServiceError) as captured:
        service.clone("src", "copy", owner="alice")
    assert captured.value.status_code == 422


def test_read_drops_unknown_future_fields(tmp_path):
    settings = make_settings(tmp_path)
    service = build_service(settings)
    service.submit(make_spec("src"), owner="alice")
    annotations = service.kubernetes.jobs["src"]["metadata"]["annotations"]
    spec = json.loads(annotations["vla-platform/spec"])
    spec["future_field"] = "x"
    annotations["vla-platform/spec"] = json.dumps(spec)

    job = service.get_job("src")
    assert job["owner"] == "alice"
    assert job["name"] == "src"


def test_gpus_mismatch_with_nodes_is_rejected(tmp_path):
    settings = Settings(api_token="t", auth_required=False, db_path=str(tmp_path / "p.db"),
                        gpu_metrics_url="", max_nodes=2, allowed_gpus=(1,))
    service = build_service(settings)
    with pytest.raises(ServiceError) as captured:
        service.submit(make_spec("multi", gpus=3, nodes=2, gpus_per_node=1), owner="alice")
    assert captured.value.status_code == 422