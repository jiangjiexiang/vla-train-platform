"""训练代码可以从宿主机挂载，覆盖镜像里的那几份。

改一行 train_vla.py 就要重建 10GB 镜像、导入 k3s，十几分钟。
迭代阶段受不了，所以给一个开关：training_code_host_path 非空时把
宿主机目录挂到 /app，改完只重建 PyTorchJob（秒级）。

默认为空——镜像自包含才是能拿去别的机器跑的前提，挂载只在开发机上用。
"""

from __future__ import annotations

from app.config import Settings
from app.models import SubmitSpec
from app.templates.pytorchjob import render_pytorch_job


def make_spec(**overrides) -> SubmitSpec:
    payload = {
        "name": "code-probe",
        "image": "vla-smolvla:latest",
        "gpus": 1,
        "queue": "default",
        "priority": "normal",
        "command": "python /app/train.py",
    }
    payload.update(overrides)
    return SubmitSpec(**payload)


def make_settings(**overrides) -> Settings:
    base = {
        "api_token": "t",
        "checkpoint_host_path": "/tmp/ckpt-host",
        "checkpoints_mount_path": "/checkpoints",
        "dataset_host_path": "/tmp/ds-host",
        "datasets_mount_path": "/datasets",
    }
    base.update(overrides)
    return Settings(**base)


def pod_spec(settings: Settings) -> dict:
    manifest = render_pytorch_job(make_spec(), "2026-11-10T00:00:00Z", settings)
    return manifest["spec"]["pytorchReplicaSpecs"]["Master"]["template"]["spec"]


def test_no_code_mount_by_default():
    """默认不挂代码——镜像自包含。"""
    spec = pod_spec(make_settings())

    names = [m["name"] for m in spec["containers"][0]["volumeMounts"]]

    assert "training-code" not in names
    assert "training-code" not in [v["name"] for v in spec["volumes"]]


def test_code_mount_when_configured():
    """配了路径就挂到 /app。"""
    spec = pod_spec(make_settings(training_code_host_path="/host/smolvla"))

    mounts = {m["name"]: m for m in spec["containers"][0]["volumeMounts"]}

    assert "training-code" in mounts
    assert mounts["training-code"]["mountPath"] == "/app"


def test_code_mount_is_read_only():
    """代码只读：训练不该改自己的源码。"""
    spec = pod_spec(make_settings(training_code_host_path="/host/smolvla"))

    mount = next(m for m in spec["containers"][0]["volumeMounts"] if m["name"] == "training-code")

    assert mount.get("readOnly") is True


def test_code_volume_points_to_configured_path():
    """hostPath 就是配的那个目录。"""
    spec = pod_spec(make_settings(training_code_host_path="/host/smolvla"))

    vol = next(v for v in spec["volumes"] if v["name"] == "training-code")

    assert vol["hostPath"]["path"] == "/host/smolvla"
    assert vol["hostPath"]["type"] == "Directory"


def test_init_container_not_affected_by_code_mount():
    """initContainer 只管权限，不该被挂上代码。"""
    spec = pod_spec(make_settings(training_code_host_path="/host/smolvla"))

    init_mounts = [m["name"] for m in spec["initContainers"][0]["volumeMounts"]]

    assert "training-code" not in init_mounts
    assert "checkpoints" in init_mounts
