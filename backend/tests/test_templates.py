"""训练 Pod 模板测试。

重点覆盖那些「本地测试发现不了、上线才崩」的配置：
/dev/shm 太小导致 DataLoader Bus error，是容器训练最常见的坑。
"""

from __future__ import annotations

import pytest
from app.config import Settings
from app.models import SubmitSpec
from app.templates.pytorchjob import render_pytorch_job


@pytest.fixture
def settings_for_template() -> Settings:
    return Settings(api_token="test-token")


def make_spec() -> SubmitSpec:
    return SubmitSpec(
        name="tpl-probe",
        image="vla-smolvla:latest",
        gpus=1,
        command="python /app/train_vla.py --steps 5",
    )


def test_pod_has_shared_memory_volume(settings_for_template):
    """训练 Pod 必须有足够大的 /dev/shm。

    容器默认只有 64MB。PyTorch DataLoader 通过共享内存传 batch，
    num_workers 一多就 DataLoader worker killed by signal: Bus error——
    这是容器训练最经典的坑，实测 batch=8/workers=4 必定崩溃。
    平台必须默认给够，而不是让用户自己猜为什么一调大参数就崩。
    """
    job = render_pytorch_job(make_spec(), "2026-01-01T00:00:00Z", settings_for_template)
    pod = job["spec"]["pytorchReplicaSpecs"]["Master"]["template"]["spec"]

    volumes = {item["name"]: item for item in pod["volumes"]}
    assert "dshm" in volumes, "缺少 dshm volume"
    assert volumes["dshm"]["emptyDir"]["medium"] == "Memory"
    # 默认值必须显著大于 64MB，否则问题仍然存在
    assert volumes["dshm"]["emptyDir"]["sizeLimit"] == "4Gi"

    mounts = {item["mountPath"]: item for item in pod["containers"][0]["volumeMounts"]}
    assert mounts.get("/dev/shm", {}).get("name") == "dshm", "没有挂载到 /dev/shm"


def test_shm_size_is_configurable(settings_for_template):
    """不同机器内存不同，大小要能改。"""
    from dataclasses import replace

    # Settings 是 frozen dataclass，不能直接赋值
    settings = replace(settings_for_template, shm_size="16Gi")

    job = render_pytorch_job(make_spec(), "2026-01-01T00:00:00Z", settings)
    pod = job["spec"]["pytorchReplicaSpecs"]["Master"]["template"]["spec"]
    volumes = {item["name"]: item for item in pod["volumes"]}

    assert volumes["dshm"]["emptyDir"]["sizeLimit"] == "16Gi"
