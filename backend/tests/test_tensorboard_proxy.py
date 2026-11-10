"""TensorBoard 反代测试。

重点测鉴权：iframe 带不了 Authorization 头，只能靠 Cookie。
未登录时必须 401，**不能**落到 SPA 兜底返回 200 的 index.html——
那样看起来像成功，实际什么都没拿到。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# 与 app.main.TENSORBOARD_COOKIE 保持一致；单独写一份是为了能在模块级引用
COOKIE = "vla_tb_session"


class FakeKubernetes:
    def get_job(self, name):
        return None

    def list_jobs(self):
        return []

    def list_namespace_pods(self):
        return []

    def list_training_pods(self):
        return []

    def list_job_pods(self, name):
        return []

    def list_workloads(self):
        return []

    def list_nodes(self):
        return []

    def get_cluster_queue(self, name):
        return None

    def get_local_queue(self):
        return None


@pytest.fixture
def client(tmp_path):
    # main 模块顶层有 `app = create_app()`，导入即执行，需要环境变量；
    # 所以放到 fixture 内部导入，让其他测试也能收集这个文件。
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        api_token="t",
        db_path=str(tmp_path / "p.db"),
        auth_mode="local",
        local_users="",
        tensorboard_enabled=False,  # 不起真进程，只测路由与鉴权
    )
    app = create_app(settings)
    app.state.service.kubernetes = FakeKubernetes()
    with TestClient(app) as c:
        yield c


def test_tensorboard_requires_cookie(client):
    """未登录访问必须是 401，而不是 SPA 兜底的 200。"""
    response = client.get("/tensorboard/")

    assert response.status_code == 401
    # 关键：不能返回 index.html。SPA 兜底会返回 HTML，这里要是纯文本
    assert "text/html" not in response.headers.get("content-type", "")


def test_tensorboard_data_endpoint_requires_cookie(client):
    """TensorBoard 的 API 子路径同样要挡。"""
    response = client.get("/tensorboard/data/runs")

    assert response.status_code == 401


def test_tensorboard_rejects_authorization_header_only(client):
    """只带 Authorization 不够——iframe 走的是 Cookie，鉴权只看 Cookie。

    这条锁住「不出现两套并存认证路径」的设计。
    """
    response = client.get("/tensorboard/", headers={"Authorization": "Bearer whatever"})

    assert response.status_code == 401


def test_tensorboard_rejects_invalid_cookie(client):
    response = client.get(
        "/tensorboard/",
        cookies={COOKIE: "not-a-real-session"},
    )

    assert response.status_code == 401
