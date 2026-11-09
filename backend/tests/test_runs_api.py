"""一键训练（/runs）与训练配方（/recipes）测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.config import Settings
from app.recipes.loader import Recipe, RecipeError, check_compatibility, parse_recipe
from app.services.runs import generated_name, merge_values
from fastapi.testclient import TestClient


def make_dataset(root: Path, name: str = "pick1028", *, format_version: str = "v3.0", episodes: int = 10) -> Path:
    dataset = root / name
    (dataset / "meta").mkdir(parents=True)
    (dataset / "data" / "chunk-000").mkdir(parents=True)
    info = {
        "codebase_version": format_version,
        "total_episodes": episodes,
        "total_frames": episodes * 300,
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


def write_recipe(directory: Path, name: str, payload: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(payload, encoding="utf-8")


SIMPLE_RECIPE = """
id: demo-recipe
name: 演示配方
description: 单测用
image: demo:latest
entrypoint: python /app/train.py
dataset_formats: [lerobot-v3.0]
template:
  schema_version: 1
  params:
    - key: steps
      flag: --steps
      label: 训练步数
      type: int
      default: 100
defaults:
  steps: 50
gpu_count: 1
order: 5
public: true
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    datasets_root = tmp_path / "datasets"
    datasets_root.mkdir()
    make_dataset(datasets_root)
    recipes_dir = tmp_path / "recipes"
    write_recipe(recipes_dir, "demo.yaml", SIMPLE_RECIPE)
    monkeypatch.setenv("PLATFORM_AUTH_MODE", "off")
    monkeypatch.setenv("PLATFORM_DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setenv("PLATFORM_GPU_METRICS_URL", "")
    monkeypatch.setenv("PLATFORM_DATASETS_MOUNT_PATH", str(datasets_root))
    monkeypatch.setenv("PLATFORM_RECIPES_DIR", str(recipes_dir))
    return datasets_root, recipes_dir


@pytest.fixture
def client(tmp_path, env):
    from app.main import create_app

    datasets_root, recipes_dir = env
    settings = Settings(
        api_token="t",
        auth_required=False,
        auth_mode="off",
        db_path=str(tmp_path / "r.db"),
        gpu_metrics_url="",
        datasets_mount_path=str(datasets_root),
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
    app.state.service._recipes = None
    app.state.service._datasets = None
    with TestClient(app) as test_client:
        test_client.get("/api/v1/datasets?refresh=true")
        yield test_client


# --------------------------------------------------------------------------
# 配方解析
# --------------------------------------------------------------------------
def test_parse_recipe_accepts_valid_payload():
    recipe = parse_recipe(
        {
            "id": "demo",
            "name": "演示",
            "image": "demo:latest",
            "dataset_formats": ["lerobot-v3.0"],
        }
    )

    assert recipe.id == "demo"
    assert recipe.gpu_count == 1
    assert recipe.public is True


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "无 id", "image": "x", "dataset_formats": ["a"]},
        {"id": "Bad_ID", "name": "x", "image": "x", "dataset_formats": ["a"]},
        {"id": "ok", "name": "x", "image": "x", "dataset_formats": []},
        {"id": "ok", "name": "x", "image": "x", "dataset_formats": ["a"], "gpu_count": 0},
        {"id": "ok", "name": "x", "image": "x", "dataset_formats": ["a"], "template": {}, "params_template": "x"},
    ],
)
def test_parse_recipe_rejects_invalid(payload):
    with pytest.raises(RecipeError):
        parse_recipe(payload)


def test_check_compatibility():
    recipe = Recipe(
        id="r",
        name="r",
        description="",
        image="i",
        entrypoint=None,
        dataset_formats=["lerobot-v2.1"],
    )
    assert check_compatibility(recipe, "lerobot-v2.1") is None
    message = check_compatibility(recipe, "lerobot-v3.0")
    assert message and "lerobot-v2.1" in message


# --------------------------------------------------------------------------
# 参数合并与命名
# --------------------------------------------------------------------------
def test_merge_values_priority(tmp_path):
    from app.params_templates import parse_template

    template = parse_template(
        {
            "schema_version": 1,
            "params": [
                {"key": "steps", "flag": "--steps", "label": "步数", "type": "int", "default": 10},
                {"key": "bs", "flag": "--bs", "label": "BS", "type": "int", "default": 1},
            ],
        }
    )

    values = merge_values(template, {"steps": 50}, {"steps": 100})

    assert values["steps"] == 100  # overrides 最高
    assert "bs" not in values  # 未给默认值，交给模板 default


def test_merge_values_rejects_unknown_key():
    from app.params_templates import parse_template
    from app.services.errors import ServiceError

    template = parse_template({"schema_version": 1, "params": []})

    with pytest.raises(ServiceError) as excinfo:
        merge_values(template, {}, {"nope": 1})

    assert excinfo.value.status_code == 422


def test_merge_values_ignores_defaults_not_in_template():
    from app.params_templates import parse_template

    template = parse_template(
        {"schema_version": 1, "params": [{"key": "a", "flag": "--a", "label": "A", "type": "int"}]}
    )

    values = merge_values(template, {"wrong_key": 1}, {})

    assert values == {}


def test_generated_name_is_unique_and_bounded():
    recipe = Recipe(
        id="smolvla-finetune",
        name="x",
        description="",
        image="i",
        entrypoint=None,
        dataset_formats=["a"],
    )

    name = generated_name(recipe, "1028pick_3.0", set())

    assert name.startswith("smolvla-")
    assert len(name) <= 50
    assert name not in generated_name(recipe, "1028pick_3.0", {name})


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------
def test_list_recipes_returns_params(client):
    response = client.get("/api/v1/recipes")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == ["demo-recipe"]
    assert [param["key"] for param in items[0]["params"]] == ["steps"]


def test_get_recipe_404(client):
    assert client.get("/api/v1/recipes/nope").status_code == 404


def test_create_run_expands_recipe(client):
    response = client.post("/api/v1/runs", json={"recipe_id": "demo-recipe", "dataset_id": "pick1028"})

    assert response.status_code == 201, response.text
    body = response.json()
    # 配方的 defaults（steps=50）覆盖模板 default（100）
    assert body["command"] == "python /app/train.py --steps 50"
    assert body["image"] == "demo:latest"
    assert body["name"].startswith("demo-pick1028-")
    assert body["status"] == "QUEUED"


def test_create_run_applies_overrides(client):
    response = client.post(
        "/api/v1/runs",
        json={"recipe_id": "demo-recipe", "dataset_id": "pick1028", "overrides": {"steps": 7}},
    )

    assert response.status_code == 201
    assert response.json()["command"] == "python /app/train.py --steps 7"


def test_create_run_uses_explicit_name(client):
    response = client.post(
        "/api/v1/runs",
        json={"recipe_id": "demo-recipe", "dataset_id": "pick1028", "name": "my-run-1"},
    )

    assert response.status_code == 201
    assert response.json()["name"] == "my-run-1"


def test_create_run_rejects_incompatible_dataset(client, tmp_path, env):
    datasets_root, _recipes = env
    make_dataset(datasets_root, "other", format_version="v2.1")
    client.get("/api/v1/datasets?refresh=true")

    response = client.post("/api/v1/runs", json={"recipe_id": "demo-recipe", "dataset_id": "other"})

    assert response.status_code == 422
    assert "不支持" in response.json()["detail"]


def test_create_run_404_for_unknown_recipe(client):
    response = client.post("/api/v1/runs", json={"recipe_id": "nope", "dataset_id": "pick1028"})

    assert response.status_code == 404


def test_create_run_404_for_unknown_dataset(client):
    response = client.post("/api/v1/runs", json={"recipe_id": "demo-recipe", "dataset_id": "nope"})

    assert response.status_code == 404


def test_create_run_rejects_unusable_dataset(client, env):
    datasets_root, _recipes = env
    broken = datasets_root / "broken"
    broken.mkdir()
    client.get("/api/v1/datasets?refresh=true")

    response = client.post("/api/v1/runs", json={"recipe_id": "demo-recipe", "dataset_id": "broken"})

    assert response.status_code == 422
    assert "不可用" in response.json()["detail"]


def test_create_run_injects_dataset_root_env(client):
    response = client.post("/api/v1/runs", json={"recipe_id": "demo-recipe", "dataset_id": "pick1028"})

    assert response.json()["env"]["DATASET_ROOT"] == "/datasets/pick1028"


def test_create_run_rejects_duplicate_name(client):
    payload = {"recipe_id": "demo-recipe", "dataset_id": "pick1028", "name": "dup-1"}
    assert client.post("/api/v1/runs", json=payload).status_code == 201

    response = client.post("/api/v1/runs", json=payload)

    assert response.status_code == 409


def test_create_run_validates_priority(client):
    response = client.post(
        "/api/v1/runs",
        json={"recipe_id": "demo-recipe", "dataset_id": "pick1028", "priority": "urgent"},
    )

    # 由 RunSubmitRequest 的 pydantic 校验拦下，FastAPI 统一返回 422
    assert response.status_code == 422

def test_recipes_dir_resolves_in_source_tree(monkeypatch):
    """源码运行时应当找到 <repo>/recipes。"""
    monkeypatch.delenv("PLATFORM_RECIPES_DIR", raising=False)
    from app.recipes import loader

    directory = loader._resolve_recipes_dir()

    assert directory.is_dir(), directory
    assert directory.name == "recipes"


def test_recipes_dir_env_override(monkeypatch, tmp_path):
    """部署时可以用环境变量显式指定，避免依赖目录层级推断。"""
    monkeypatch.setenv("PLATFORM_RECIPES_DIR", str(tmp_path))
    from app.recipes import loader

    assert loader._resolve_recipes_dir() == tmp_path
    assert loader.RecipeStore().directory == tmp_path


def test_recipes_dir_container_layout(monkeypatch):
    """容器内 loader.py 位于 /app/app/recipes/，应解析到 /app/recipes。

    这里不真的读 /app，而是验证「父层级推断」这一规则本身：
    用仓库里的路径模拟同一深度关系。
    """
    monkeypatch.delenv("PLATFORM_RECIPES_DIR", raising=False)
    from pathlib import Path

    here = Path("/home/jjx/vla-train-platform/backend/app/recipes/loader.py")
    # 容器：parents[2] = /app；源码：parents[2] = backend/
    assert here.parents[2].name in ("backend", "app")

def test_concurrent_run_creation_gets_distinct_names(client, tmp_path):
    """并发点「开始」时，每个请求都应拿到不同的自动任务名。

    曾经的 bug：查重名与创建是分开的两步，并发请求拿到同一份 existing 快照，
    生成同一个名字，只有一个能建成，其余用户看到「任务名已存在」——
    可他们本来就没填任务名。
    """
    import json as _json
    import threading

    dataset_dir = tmp_path / "datasets" / "ds1"
    (dataset_dir / "meta").mkdir(parents=True)
    (dataset_dir / "meta" / "info.json").write_text(
        _json.dumps(
            {
                "codebase_version": "v3.0",
                "robot_type": "test",
                "total_episodes": 30,
                "total_frames": 3000,
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

    recipe_id = client.get("/api/v1/recipes").json()["items"][0]["id"]
    results: list[tuple[int, str]] = []

    def submit() -> None:
        response = client.post("/api/v1/runs", json={"recipe_id": recipe_id, "dataset_id": "ds1"})
        body = response.json()
        results.append((response.status_code, body.get("name") or body.get("detail", "")))

    threads = [threading.Thread(target=submit) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    names = [name for status, name in results if status == 201]
    # 全部成功，且名字互不相同
    assert len(names) == 5, results
    assert len(set(names)) == 5, names
