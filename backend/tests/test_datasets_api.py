"""数据集接口测试（/api/v1/datasets）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.datasets.store import DatasetStore


def make_dataset(root: Path, name: str, *, episodes: int = 10, frames: int = 3000, version: str = "v3.0") -> Path:
    dataset = root / name
    (dataset / "meta").mkdir(parents=True)
    (dataset / "data" / "chunk-000").mkdir(parents=True)
    (dataset / "videos" / "observation.images.top" / "chunk-000").mkdir(parents=True)
    info = {
        "codebase_version": version,
        "robot_type": "dual_arm",
        "total_episodes": episodes,
        "total_frames": frames,
        "total_tasks": 1,
        "chunks_size": 1000,
        "fps": 30,
        "features": {
            "observation.state": {"dtype": "float32", "shape": [7]},
            "action": {"dtype": "float32", "shape": [7]},
            "observation.images.top": {"dtype": "video", "shape": [480, 640, 3]},
        },
    }
    (dataset / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")
    return dataset


@pytest.fixture
def datasets_root(tmp_path):
    root = tmp_path / "datasets"
    root.mkdir()
    make_dataset(root, "alpha")
    make_dataset(root, "beta", episodes=5, frames=1500)
    return root


@pytest.fixture
def client(tmp_path, datasets_root, monkeypatch):
    monkeypatch.setenv("PLATFORM_AUTH_MODE", "off")
    monkeypatch.setenv("PLATFORM_DB_PATH", str(tmp_path / "ds.db"))
    monkeypatch.setenv("PLATFORM_GPU_METRICS_URL", "")
    monkeypatch.setenv("PLATFORM_DATASETS_MOUNT_PATH", str(datasets_root))

    from app.main import create_app

    settings = Settings(
        api_token="t",
        auth_required=False,
        auth_mode="off",
        db_path=str(tmp_path / "ds.db"),
        gpu_metrics_url="",
        datasets_mount_path=str(datasets_root),
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def test_list_datasets_scans_new_directories(client):
    response = client.get("/api/v1/datasets?refresh=true")

    assert response.status_code == 200, response.text
    body = response.json()
    ids = [item["id"] for item in body["items"]]
    assert ids == ["alpha", "beta"]
    assert body["summary"]["total"] == 2
    assert body["summary"]["ready"] == 2


def test_list_datasets_without_refresh_uses_cache(client):
    client.get("/api/v1/datasets?refresh=true")

    body = client.get("/api/v1/datasets").json()

    assert [item["id"] for item in body["items"]] == ["alpha", "beta"]


def test_dataset_detail_returns_metadata(client):
    response = client.get("/api/v1/datasets/alpha")

    assert response.status_code == 200
    body = response.json()
    assert body["episodes"] == 10
    assert body["frames"] == 3000
    assert body["format"] == "lerobot-v3.0"
    assert body["cameras"] == ["observation.images.top"]
    assert body["image_size"] == [480, 640]
    assert body["state_dim"] == 7


def test_dataset_detail_deep_triggers_deep_scan(client):
    response = client.get("/api/v1/datasets/alpha?deep=true")

    assert response.status_code == 200
    assert response.json()["scan_depth"] == "deep"


def test_dataset_detail_404_for_unknown(client):
    response = client.get("/api/v1/datasets/nope")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_scan_endpoint_scans_single_dataset(client):
    response = client.post("/api/v1/datasets/scan", json={"id": "beta", "deep": True})

    assert response.status_code == 200
    body = response.json()
    assert body["scanned"] == 1
    assert body["items"][0]["id"] == "beta"


def test_scan_endpoint_404_for_unknown(client):
    response = client.post("/api/v1/datasets/scan", json={"id": "missing"})

    assert response.status_code == 404


def test_scan_all_when_id_omitted(client):
    response = client.post("/api/v1/datasets/scan", json={})

    assert response.status_code == 200
    assert response.json()["scanned"] == 2


def test_summary_counts_unusable_datasets(client, datasets_root):
    broken = datasets_root / "broken"
    broken.mkdir()
    client.get("/api/v1/datasets?refresh=true")

    body = client.get("/api/v1/datasets").json()

    assert body["summary"]["unusable"] == 1
    assert body["summary"]["ready"] == 2


def test_dataset_store_reports_needs_rescan(tmp_path, datasets_root):
    store = DatasetStore(str(tmp_path / "s.db"))
    assert set(store.needs_rescan(datasets_root)) == {"alpha", "beta"}

    store.scan_many(datasets_root)

    assert store.needs_rescan(datasets_root) == []


def test_dataset_store_removes_deleted_directories(tmp_path, datasets_root):
    store = DatasetStore(str(tmp_path / "s.db"))
    store.scan_many(datasets_root)
    import shutil

    shutil.rmtree(datasets_root / "alpha")

    store.scan_many(datasets_root)

    assert [meta.id for meta in store.list_all()] == ["beta"]


def test_dataset_store_survives_single_failure(tmp_path, datasets_root):
    """单个数据集扫描抛异常时，其他数据集仍应被扫描。"""
    store = DatasetStore(str(tmp_path / "s.db"))
    original = store.scan_one

    def flaky(root, dataset_id, deep=False):
        if dataset_id == "alpha":
            raise RuntimeError("boom")
        return original(root, dataset_id, deep=deep)

    store.scan_one = flaky  # type: ignore[method-assign]

    results = store.scan_many(datasets_root)

    statuses = {meta.id: meta.status for meta in results}
    assert statuses["alpha"] == "error"
    assert statuses["beta"] == "ready"