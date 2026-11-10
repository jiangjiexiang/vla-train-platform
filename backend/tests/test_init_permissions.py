"""训练 Pod 启动前必须把 checkpoint 目录权限放开。

训练容器和 Web 容器 uid 不同（训练默认 root，Web 是 10001），
而它们读写同一个 checkpoint 目录：训练写、Web 读和清理。
不一致的后果实测过：训练写出 root:root 600 的文件，Web 读不了，
任务详情页指标是空的、权重下载按钮点下去 403。
反过来把目录属主改成 Web 的 uid，训练就写不进去。

所以训练 Pod 带一个 initContainer，在容器起来之前 chmod 一次。
"""

from __future__ import annotations

from app.config import Settings
from app.models import SubmitSpec
from app.templates.pytorchjob import render_pytorch_job


def make_spec(**overrides) -> SubmitSpec:
    payload = {
        "name": "perm-probe",
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


def test_init_container_present():
    """训练 Pod 必须有 initContainer 处理权限。"""
    manifest = render_pytorch_job(make_spec(), "2026-11-10T00:00:00Z", make_settings())

    pod = manifest["spec"]["pytorchReplicaSpecs"]["Master"]["template"]["spec"]

    assert "initContainers" in pod
    assert len(pod["initContainers"]) == 1
    assert pod["initContainers"][0]["name"] == "init-permissions"


def test_init_container_mounts_checkpoints():
    """initContainer 要挂 checkpoint 目录，否则 chmod 的是它自己的临时文件系统。"""
    manifest = render_pytorch_job(make_spec(), "2026-11-10T00:00:00Z", make_settings())

    init = manifest["spec"]["pytorchReplicaSpecs"]["Master"]["template"]["spec"]["initContainers"][0]

    mounts = {m["name"]: m["mountPath"] for m in init["volumeMounts"]}
    assert mounts.get("checkpoints") == "/checkpoints"


def test_init_container_opens_permissions():
    """命令里要有 chmod a+rwX，否则 root 写出的文件 Web 还是读不了。"""
    manifest = render_pytorch_job(make_spec(), "2026-11-10T00:00:00Z", make_settings())

    init = manifest["spec"]["pytorchReplicaSpecs"]["Master"]["template"]["spec"]["initContainers"][0]

    script = " ".join(init["command"])
    assert "chmod" in script
    assert "a+rwX" in script


def test_init_container_uses_same_image():
    """initContainer 复用训练镜像，不额外引入一个要拉取的镜像。"""
    manifest = render_pytorch_job(make_spec(), "2026-11-10T00:00:00Z", make_settings())

    pod = manifest["spec"]["pytorchReplicaSpecs"]["Master"]["template"]["spec"]

    assert pod["initContainers"][0]["image"] == pod["containers"][0]["image"]


def test_init_container_follows_mount_path_setting():
    """挂载点可配置时，initContainer 要跟着走，不能写死 /checkpoints。"""
    manifest = render_pytorch_job(
        make_spec(), "2026-11-10T00:00:00Z", make_settings(checkpoints_mount_path="/ckpt")
    )

    init = manifest["spec"]["pytorchReplicaSpecs"]["Master"]["template"]["spec"]["initContainers"][0]

    assert any("chmod" in part and "/ckpt" in part for part in init["command"])
