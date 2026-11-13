"""配方管理（/admin/recipes）测试：完整 YAML 覆盖、校验、恢复默认、历史与回滚。"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import Settings

DEMO_RECIPE = """
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

OVERRIDE_RECIPE = """
id: demo-recipe
name: 演示配方（管理员改过）
description: 覆盖版
image: demo:v2
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
  steps: 80
gpu_count: 2
order: 5
public: true
"""


@pytest.fixture
def admin_client(app_client, tmp_path, monkeypatch):
    """auth=off（匿名即 admin）的客户端，配方目录指向临时目录。"""
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    (recipes_dir / "demo.yaml").write_text(DEMO_RECIPE, encoding="utf-8")
    monkeypatch.setenv("PLATFORM_RECIPES_DIR", str(recipes_dir))
    settings = Settings(
        api_token="test-token",
        auth_required=False,
        auth_mode="off",
        db_path=str(tmp_path / "r.db"),
        gpu_metrics_url="",
    )
    client, _service = app_client(settings)
    with client:
        yield client


def test_admin_list_marks_builtin(admin_client):
    response = admin_client.get("/api/v1/admin/recipes")
    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()["items"]}
    assert items["demo-recipe"]["source"] == "builtin"
    assert items["demo-recipe"]["updated_by"] is None


def test_admin_get_returns_builtin_yaml(admin_client):
    response = admin_client.get("/api/v1/admin/recipes/demo-recipe")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "builtin"
    assert "演示配方" in body["yaml"]


def test_save_override_takes_effect_immediately(admin_client):
    saved = admin_client.put("/api/v1/admin/recipes/demo-recipe", json={"yaml": OVERRIDE_RECIPE})
    assert saved.status_code == 200, saved.text
    assert saved.json()["source"] == "override"
    assert saved.json()["updated_by"].startswith("anonymous")

    # 普通用户侧的只读列表立刻读到覆盖后的内容，不需要重启。
    listing = admin_client.get("/api/v1/recipes").json()["items"]
    demo = next(item for item in listing if item["id"] == "demo-recipe")
    assert demo["name"] == "演示配方（管理员改过）"
    assert demo["gpu_count"] == 2
    assert demo["image"] == "demo:v2"


def test_save_rejects_invalid_yaml(admin_client):
    response = admin_client.put("/api/v1/admin/recipes/demo-recipe", json={"yaml": "id: [broken"})
    assert response.status_code == 422
    # 非法输入不落库：生效的仍是内置默认
    assert admin_client.get("/api/v1/admin/recipes/demo-recipe").json()["source"] == "builtin"


def test_save_rejects_id_mismatch(admin_client):
    response = admin_client.put(
        "/api/v1/admin/recipes/demo-recipe",
        json={"yaml": OVERRIDE_RECIPE.replace("id: demo-recipe", "id: other-recipe")},
    )
    assert response.status_code == 422
    assert "id" in response.json()["detail"]


def test_save_rejects_bad_image(admin_client):
    response = admin_client.put(
        "/api/v1/admin/recipes/demo-recipe",
        json={"yaml": OVERRIDE_RECIPE.replace("demo:v2", "Bad Image!!")},
    )
    assert response.status_code == 422
    assert "镜像名" in response.json()["detail"]


def test_save_rejects_broken_template(admin_client):
    broken = OVERRIDE_RECIPE.replace("type: int", "type: enum")
    response = admin_client.put("/api/v1/admin/recipes/demo-recipe", json={"yaml": broken})
    assert response.status_code == 422


def test_save_rejects_unknown_default_key(admin_client):
    broken = OVERRIDE_RECIPE.replace("steps: 80", "nonsense: 80")
    response = admin_client.put("/api/v1/admin/recipes/demo-recipe", json={"yaml": broken})
    assert response.status_code == 422
    assert "defaults" in response.json()["detail"]


def test_save_unknown_recipe_is_404(admin_client):
    response = admin_client.put("/api/v1/admin/recipes/nope", json={"yaml": OVERRIDE_RECIPE})
    assert response.status_code == 404


def test_restore_default(admin_client):
    assert admin_client.put(
        "/api/v1/admin/recipes/demo-recipe", json={"yaml": OVERRIDE_RECIPE}
    ).status_code == 200
    restored = admin_client.delete("/api/v1/admin/recipes/demo-recipe")
    assert restored.status_code == 200
    assert restored.json()["restored"] is True
    assert restored.json()["source"] == "builtin"

    listing = admin_client.get("/api/v1/recipes").json()["items"]
    demo = next(item for item in listing if item["id"] == "demo-recipe")
    assert demo["name"] == "演示配方"

    # 再恢复一次：没有覆盖时不报错，restored=false
    again = admin_client.delete("/api/v1/admin/recipes/demo-recipe")
    assert again.status_code == 200
    assert again.json()["restored"] is False


def test_history_records_actor_and_actions(app_client, tmp_path, monkeypatch):
    """ldap 模式下用 admin 会话保存，历史里要看到操作者与动作序列。"""
    from test_auth import ldap_client, make_session

    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    (recipes_dir / "demo.yaml").write_text(DEMO_RECIPE, encoding="utf-8")
    monkeypatch.setenv("PLATFORM_RECIPES_DIR", str(recipes_dir))
    client, service = ldap_client(app_client, tmp_path, monkeypatch)
    with client:
        admin = make_session(service, "root", role="admin")
        assert client.put(
            "/api/v1/admin/recipes/demo-recipe", json={"yaml": OVERRIDE_RECIPE}, headers=admin
        ).status_code == 200
        assert client.delete("/api/v1/admin/recipes/demo-recipe", headers=admin).status_code == 200

        history = client.get("/api/v1/admin/recipes/demo-recipe/history", headers=admin).json()["items"]
        assert [item["action"] for item in history] == ["restore", "save"]
        assert all(item["changed_by"] == "root" for item in history)
        assert history[1]["has_yaml"] is True  # save 带 YAML 全量
        assert history[0]["has_yaml"] is False  # restore 不带


def test_rollback_to_previous_version(admin_client):
    v1 = OVERRIDE_RECIPE.replace("gpu_count: 2", "gpu_count: 1")
    v2 = OVERRIDE_RECIPE
    assert admin_client.put("/api/v1/admin/recipes/demo-recipe", json={"yaml": v1}).status_code == 200
    assert admin_client.put("/api/v1/admin/recipes/demo-recipe", json={"yaml": v2}).status_code == 200

    # 回滚一次：回到 v1（gpu_count=1 的覆盖）
    rolled = admin_client.post("/api/v1/admin/recipes/demo-recipe/rollback")
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["source"] == "override"
    assert "gpu_count: 1" in rolled.json()["yaml"]

    # 再回滚一次：v1 之前没有覆盖记录，恢复内置默认
    rolled = admin_client.post("/api/v1/admin/recipes/demo-recipe/rollback")
    assert rolled.status_code == 200
    assert rolled.json()["source"] == "builtin"

    # 没有更早的历史了
    assert admin_client.post("/api/v1/admin/recipes/demo-recipe/rollback").status_code == 409


def test_admin_routes_reject_normal_users(app_client, tmp_path, monkeypatch):
    from test_auth import ldap_client, make_session

    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    (recipes_dir / "demo.yaml").write_text(DEMO_RECIPE, encoding="utf-8")
    monkeypatch.setenv("PLATFORM_RECIPES_DIR", str(recipes_dir))
    client, service = ldap_client(app_client, tmp_path, monkeypatch)
    with client:
        alice = make_session(service, "alice")
        checks = [
            client.get("/api/v1/admin/recipes", headers=alice),
            client.get("/api/v1/admin/recipes/demo-recipe", headers=alice),
            client.put("/api/v1/admin/recipes/demo-recipe", json={"yaml": OVERRIDE_RECIPE}, headers=alice),
            client.delete("/api/v1/admin/recipes/demo-recipe", headers=alice),
            client.get("/api/v1/admin/recipes/demo-recipe/history", headers=alice),
            client.post("/api/v1/admin/recipes/demo-recipe/rollback", headers=alice),
        ]
        for response in checks:
            assert response.status_code == 403
            assert response.json() == {"detail": "admin privileges required"}


def test_override_survives_recipe_store_recreation(admin_client):
    """覆盖存在数据库里：服务重建（等价于 Web 容器重启）后仍然生效。"""
    assert admin_client.put(
        "/api/v1/admin/recipes/demo-recipe", json={"yaml": OVERRIDE_RECIPE}
    ).status_code == 200
    # 丢掉缓存的 RecipeStore，模拟重启后第一次读取
    admin_client.app.state.service._recipes = None
    listing = admin_client.get("/api/v1/recipes").json()["items"]
    demo = next(item for item in listing if item["id"] == "demo-recipe")
    assert demo["name"] == "演示配方（管理员改过）"
