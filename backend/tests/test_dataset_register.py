"""数据集注册接口（POST /api/v1/datasets）与单集重扫（POST /datasets/{id}/scan）。

这是安全相关功能：注册等于让管理员把任意路径带进平台，所以测试重点不在
「能注册」，而在于**不该注册的都注册不上**：
路径穿越、训练产物目录、非数据集目录、非 admin 用户、重复 id。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.config import Settings
from app.services.dataset_register import (
    UnsafeDatasetPath,
    check_lerobot_format,
    resolve_dataset_dir,
    to_container_path,
    validate_dataset_id,
)
from fastapi.testclient import TestClient

ADMIN = {"Authorization": "Bearer tok-admin"}
USER = {"Authorization": "Bearer tok-user"}


def make_dataset(root: Path, name: str, *, episodes: int = 4, frames: int = 480, version: str = "v2.1") -> Path:
    """造一个最小可识别的 LeRobot 数据集目录。"""
    dataset = root / name
    (dataset / "meta").mkdir(parents=True)
    (dataset / "data" / "chunk-000").mkdir(parents=True)
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
        },
    }
    (dataset / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")
    return dataset


@pytest.fixture
def datasets_root(tmp_path):
    root = tmp_path / "datasets"
    root.mkdir()
    make_dataset(root, "alpha")
    return root


def make_settings(tmp_path, datasets_root, **overrides) -> Settings:
    """token 模式 + 双身份：alice 是 admin，bob 是普通用户。"""
    values = {
        "api_token": "tok-admin",
        "auth_mode": "token",
        "auth_required": True,
        "user_tokens": "alice:admin:tok-admin,bob:user:tok-user",
        "db_path": str(tmp_path / "register.db"),
        "gpu_metrics_url": "",
        "datasets_mount_path": str(datasets_root),
        "backup_interval_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def register_client(tmp_path, datasets_root, monkeypatch):
    """返回 (client, settings)；client 已绑定 admin / user 两个身份。

    ``auth.token_registry()`` 是模块级缓存（生产里进程启动后令牌不会变），
    而本用例要在同一个 pytest 进程里换一套令牌，所以这里显式填缓存，
    保证测试结果不依赖用例执行顺序。
    """
    monkeypatch.setenv("PLATFORM_AUTH_MODE", "token")
    monkeypatch.setenv("PLATFORM_AUTH_REQUIRED", "true")
    monkeypatch.setenv("PLATFORM_API_TOKEN", "tok-admin")
    monkeypatch.setenv("PLATFORM_USER_TOKENS", "alice:admin:tok-admin,bob:user:tok-user")
    monkeypatch.setenv("PLATFORM_DB_PATH", str(tmp_path / "import-side-effect.db"))

    from app import auth
    from app.main import create_app

    auth._TOKEN_REGISTRY = None  # noqa: SLF001 - 测试隔离：见上面 docstring
    settings = make_settings(tmp_path, datasets_root)
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, settings


# --- 正常路径 -------------------------------------------------------------


def test_register_existing_directory(register_client, datasets_root):
    client, _settings = register_client
    target = make_dataset(datasets_root, "new-data", episodes=12, frames=3600)

    response = client.post("/api/v1/datasets", json={"path": str(target), "id": "my-new-data"}, headers=ADMIN)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == "my-new-data"
    # 返回识别出的格式、集数、帧数——前端注册成功后要立刻能显示
    assert body["format"] == "lerobot-v2.1"
    assert body["episodes"] == 12
    assert body["frames"] == 3600
    assert body["path"] == str(target.resolve())

    # 已进入列表。注意默认只返回缓存，新目录要 refresh=true 才会被扫描发现，
    # 而注册接口本身已经把记录写进缓存了。
    listing = client.get("/api/v1/datasets?refresh=true", headers=USER).json()
    assert {"alpha", "my-new-data"} <= {item["id"] for item in listing["items"]}


def test_register_defaults_id_to_directory_name(register_client, datasets_root):
    client, _settings = register_client
    target = make_dataset(datasets_root, "no-id-given")

    response = client.post("/api/v1/datasets", json={"path": str(target)}, headers=ADMIN)

    assert response.status_code == 201, response.text
    assert response.json()["id"] == "no-id-given"


# --- 失败路径 -------------------------------------------------------------


def test_register_missing_path_returns_400(register_client, datasets_root):
    client, _settings = register_client

    response = client.post("/api/v1/datasets", json={"path": str(datasets_root / "ghost")}, headers=ADMIN)

    assert response.status_code == 400
    assert "不存在" in response.json()["detail"]


@pytest.mark.parametrize(
    "suffix",
    [
        "../../etc/passwd",
        "../outside-dataset",
        "../../../../../../etc",
    ],
)
def test_register_path_traversal_rejected(register_client, datasets_root, tmp_path, suffix):
    """路径穿越必须被拒，且拒绝发生在 stat 之前（不泄露目标是否存在）。"""
    client, _settings = register_client
    # 造一个真实存在的越界目录，确保拒绝理由是「越界」而不是「不存在」
    outside = tmp_path / "outside-dataset"
    (outside / "meta").mkdir(parents=True)
    (outside / "meta" / "info.json").write_text(json.dumps({"codebase_version": "v2.1"}), encoding="utf-8")

    response = client.post("/api/v1/datasets", json={"path": str(datasets_root) + "/" + suffix}, headers=ADMIN)

    assert response.status_code == 400, response.text
    assert "允许" in response.json()["detail"]


def test_register_symlink_escape_rejected(register_client, datasets_root, tmp_path):
    """软链接指向根目录之外同样要被拒——resolve() 之后再比对。"""
    client, _settings = register_client
    outside = tmp_path / "outside"
    make_dataset(tmp_path, "outside/evil")
    (datasets_root / "sneaky").symlink_to(outside / "evil")

    response = client.post("/api/v1/datasets", json={"path": str(datasets_root / "sneaky")}, headers=ADMIN)

    assert response.status_code == 400, response.text
    assert "允许" in response.json()["detail"]


def test_register_training_productions_rejected(register_client, datasets_root):
    """训练产物目录不允许注册（即使它刚好长得像数据集）。"""
    client, _settings = register_client
    checkpoints = datasets_root / "checkpoints"
    make_dataset(datasets_root, "checkpoints/01000")

    response = client.post("/api/v1/datasets", json={"path": str(checkpoints)}, headers=ADMIN)

    assert response.status_code == 400
    assert "训练产物" in response.json()["detail"]


def test_register_non_lerobot_directory_rejected(register_client, datasets_root):
    client, _settings = register_client
    plain = datasets_root / "plain-files"
    plain.mkdir()
    (plain / "readme.txt").write_text("not a dataset", encoding="utf-8")

    response = client.post("/api/v1/datasets", json={"path": str(plain)}, headers=ADMIN)

    assert response.status_code == 400
    assert "info.json" in response.json()["detail"]


def test_register_requires_admin(register_client, datasets_root):
    client, _settings = register_client
    target = make_dataset(datasets_root, "user-tries")

    response = client.post("/api/v1/datasets", json={"path": str(target)}, headers=USER)

    assert response.status_code == 403
    assert response.json() == {"detail": "admin privileges required"}
    # 没被写进库
    assert "user-tries" not in {item["id"] for item in client.get("/api/v1/datasets", headers=ADMIN).json()["items"]}


def test_register_requires_auth(register_client, datasets_root):
    client, _settings = register_client
    target = make_dataset(datasets_root, "anon-tries")

    response = client.post("/api/v1/datasets", json={"path": str(target)})

    assert response.status_code == 401


def test_register_duplicate_id_returns_409(register_client, datasets_root):
    client, _settings = register_client
    first = make_dataset(datasets_root, "dup-a")
    second = make_dataset(datasets_root, "dup-b")

    assert client.post("/api/v1/datasets", json={"path": str(first), "id": "same"}, headers=ADMIN).status_code == 201
    response = client.post("/api/v1/datasets", json={"path": str(second), "id": "same"}, headers=ADMIN)

    assert response.status_code == 409
    assert "already registered" in response.json()["detail"]
    # 原记录没被顶掉
    detail = client.get("/api/v1/datasets/same", headers=ADMIN).json()
    assert detail["path"] == str(first.resolve())


def test_scan_unknown_id_returns_404(register_client):
    client, _settings = register_client

    message = client.post("/api/v1/datasets/missing/scan", headers=ADMIN)
    legacy = client.post("/api/v1/datasets/scan", json={"id": "missing"}, headers=ADMIN)

    assert message.status_code == 404
    assert legacy.status_code == 404


# --- 重扫 ----------------------------------------------------------------


def test_rescan_registered_dataset(register_client, datasets_root):
    client, _settings = register_client
    target = make_dataset(datasets_root, "rescan-me", episodes=2, frames=240)
    assert client.post("/api/v1/datasets", json={"path": str(target)}, headers=ADMIN).status_code == 201

    # 数据变多之后重扫，新的集数要落库
    info = json.loads((target / "meta" / "info.json").read_text(encoding="utf-8"))
    info["total_episodes"] = 9
    info["total_frames"] = 1080
    (target / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")

    response = client.post("/api/v1/datasets/rescan-me/scan", headers=ADMIN)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scanned"] == 1
    assert body["items"][0]["episodes"] == 9
    assert body["items"][0]["frames"] == 1080


def test_rescan_requires_admin(register_client, datasets_root):
    client, _settings = register_client
    target = make_dataset(datasets_root, "rescan-guard")
    assert client.post("/api/v1/datasets", json={"path": str(target)}, headers=ADMIN).status_code == 201

    response = client.post("/api/v1/datasets/rescan-guard/scan", headers=USER)

    assert response.status_code == 403


def test_rescan_detects_removed_directory(register_client, datasets_root, tmp_path):
    """注册后目录被删：重扫返回 404，并把失效记录从库里清掉。"""
    import shutil

    client, _settings = register_client
    target = make_dataset(datasets_root, "will-vanish")
    assert client.post("/api/v1/datasets", json={"path": str(target)}, headers=ADMIN).status_code == 201
    shutil.rmtree(target)

    response = client.post("/api/v1/datasets/will-vanish/scan", headers=ADMIN)

    assert response.status_code == 404
    # 失效记录已清掉，列表里不应再出现
    listing = client.get("/api/v1/datasets", headers=ADMIN).json()
    assert "will-vanish" not in {item["id"] for item in listing["items"]}


# --- 纯函数层：允许根目录配置 -----------------------------------------------


def test_allowed_roots_extends_registration_scope(tmp_path, datasets_root):
    """配置了额外允许根目录后，其中更深层的数据集也能注册。"""
    raw = tmp_path / "raw"
    nested = make_dataset(raw, "team/deep-data")
    settings = make_settings(tmp_path, datasets_root, dataset_allowed_roots=(str(raw),))

    resolved = resolve_dataset_dir(settings, str(nested))

    assert resolved.dataset_id == "deep-data"
    assert resolved.path == nested.resolve()
    assert check_lerobot_format(resolved.path) == "lerobot-v2.1"


def test_root_itself_cannot_be_registered(tmp_path, datasets_root):
    settings = make_settings(tmp_path, datasets_root)

    with pytest.raises(UnsafeDatasetPath, match="根目录本身"):
        resolve_dataset_dir(settings, str(datasets_root))


@pytest.mark.parametrize("bad_id", ["..", ".", "a/b", "a\\b", "", " ", "x\ty", ".hidden", "tmp.partial"])
def test_invalid_dataset_ids_rejected(bad_id):
    with pytest.raises(UnsafeDatasetPath):
        validate_dataset_id(bad_id)


# ---------------------------------------------------------------------------
# 宿主机路径 → 容器路径的翻译
#
# 用户看到的是宿主机上的 /data/vla-platform/datasets/xxx，
# 而容器把它挂在 /datasets/xxx。让人自己换算挂载点没道理，
# 所以注册接口两种路径都接受。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/data/vla-platform/datasets/pick1028", "/datasets/pick1028"),
        ("/datasets/pick1028", "/datasets/pick1028"),
        ("/data/vla-platform/datasets", "/datasets"),
        # 末尾斜杠不该影响判断
        ("/data/vla-platform/datasets/pick1028/", "/datasets/pick1028"),
        # 无关的绝对路径原样返回，交给后续校验拒绝
        ("/etc/passwd", "/etc/passwd"),
        # 前缀相似但不是子目录，不能被误判成数据集路径
        ("/data/vla-platform/datasets-backup/x", "/data/vla-platform/datasets-backup/x"),
        # 相对路径不处理
        ("datasets/x", "datasets/x"),
    ],
)
def test_host_path_translated_to_container_path(raw, expected):
    settings = Settings(api_token="t", dataset_host_path="/data/vla-platform/datasets", datasets_mount_path="/datasets")

    assert to_container_path(settings, raw) == expected


def test_host_path_translation_is_noop_when_equal():
    """宿主路径和挂载点相同时不应改动任何东西（比如开发环境直接跑）。"""
    settings = Settings(api_token="t", dataset_host_path="/datasets", datasets_mount_path="/datasets")

    assert to_container_path(settings, "/datasets/x") == "/datasets/x"


def test_host_path_translation_is_noop_without_host_path():
    settings = Settings(api_token="t", dataset_host_path="", datasets_mount_path="/datasets")

    assert to_container_path(settings, "/data/vla-platform/datasets/x") == "/data/vla-platform/datasets/x"
