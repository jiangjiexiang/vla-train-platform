"""宿主机路径 vs 容器内路径的区分测试。

数据目录从 ``/data/vla-platform`` 迁到 ``<项目>/data`` 之后，配置里出现了两组
看着相似、用途完全不同的路径：

- ``checkpoint_host_path`` / ``dataset_host_path``：**宿主机**路径，
  只用来构造训练 Job 的 hostPath volume；
- ``checkpoints_mount_path`` / ``datasets_mount_path``：**容器内**路径，
  Web 进程实际能读到的路径。

Web 容器里根本不存在 ``/home/jjx/vla-train-platform/data/...`` 这个前缀。
代码里只要有一处拿宿主机路径去读文件，那个功能在真实部署里就是静默失效
（不报错，只是永远返回空），本地测试还看不出来——所以这里刻意让两个路径
指向**不同的真实目录**，任何一处用错都会立刻表现为「读不到数据」。

本文件的用例覆盖四类读取/统计动作 + 一处构造动作：
读 metrics.jsonl、读 TensorBoard events、列 checkpoint 文件、统计磁盘占用、
以及渲染训练 Job。
"""

from __future__ import annotations

import pytest
from app.config import Settings
from app.models import SubmitSpec
from app.services.platform import PlatformService
from app.services.tensorboard_proxy import TensorboardProcess
from app.templates.pytorchjob import render_pytorch_job

JOB = "path-probe"


# --- 夹具 -------------------------------------------------------------------


@pytest.fixture
def paths(tmp_path):
    """宿主机目录与容器内目录是两份互不相同的真实目录。

    host 目录里什么都不放，模拟「容器里那个宿主机前缀不存在」。
    只有 mount 目录里才有产物，用错路径就必然读空。
    """
    host = tmp_path / "host-root" / "data" / "checkpoints"
    host.mkdir(parents=True)
    mount = tmp_path / "container-root" / "checkpoints"
    mount.mkdir(parents=True)
    return host, mount


def make_settings(tmp_path, host, mount, **overrides) -> Settings:
    values = {
        "api_token": "test-token",
        "db_path": str(tmp_path / "path-kinds.db"),
        "gpu_metrics_url": "",
        "checkpoint_host_path": str(host),
        "checkpoints_mount_path": str(mount),
    }
    values.update(overrides)
    return Settings(**values)


def build_service(settings: Settings) -> PlatformService:
    """建服务并提交一个任务。

    job_metrics / job_tensorboard 会先判定「任务是否曾经存在」（K8s 对象被 TTL
    回收后仍要能查历史曲线），所以必须真的走一遍提交链路，否则直接 404。
    """
    from app.history import HistoryStore
    from conftest import FakeKubernetes

    service = PlatformService(settings, HistoryStore(settings.db_path), FakeKubernetes())
    service.submit(make_spec(), "tester")
    return service


def make_spec(**overrides) -> SubmitSpec:
    payload = {
        "name": JOB,
        "image": "vla-smoke:latest",
        "gpus": 1,
        "queue": "default",
        "priority": "normal",
        "command": "python /app/train.py",
        "env": {},
        "max_retries": 0,
    }
    payload.update(overrides)
    return SubmitSpec(**payload)


def _write_events(directory, *, points, tag="loss"):
    """用 EventFileWriter 落一个真实的 events 文件（与 test_tensorboard.py 同款）。"""
    from tensorboard.compat.proto.event_pb2 import Event
    from tensorboard.compat.proto.summary_pb2 import Summary
    from tensorboard.summary.writer.event_file_writer import EventFileWriter

    directory.mkdir(parents=True, exist_ok=True)
    writer = EventFileWriter(str(directory))
    for step, value in points:
        writer.add_event(
            Event(
                wall_time=1_760_000_000.0 + step,
                step=step,
                summary=Summary(value=[Summary.Value(tag=tag, simple_value=value)]),
            )
        )
    writer.close()


def write_artifacts(mount, *, with_events: bool = True):
    """在「容器内」目录里造一份完整产物：metrics、checkpoint、events。"""
    job_dir = mount / JOB
    weights = job_dir / "run" / "checkpoints" / "000100"
    weights.mkdir(parents=True)
    (weights / "model.safetensors").write_bytes(b"x" * 256)
    (job_dir / "metrics.jsonl").write_text(
        '{"step": 100, "loss": 0.5}\n{"step": 200, "loss": 0.25}\n', encoding="utf-8"
    )
    if with_events:
        _write_events(job_dir / "tensorboard", points=[(100, 0.5), (200, 0.25)])
    return job_dir


# --- 1. 读取类动作一律走容器内路径 -------------------------------------------


def test_metrics_are_read_from_mount_path(tmp_path, paths):
    """metrics.jsonl 在容器内路径下，宿主机路径下是空的。"""
    host, mount = paths
    write_artifacts(mount, with_events=False)
    service = build_service(make_settings(tmp_path, host, mount))

    result = service.job_metrics(JOB)

    assert result["path_exists"] is True, "读 metrics 用错了路径：容器内路径才读得到"
    assert [point["step"] for point in result["items"]] == [100, 200]
    assert result["items"][-1]["loss"] == 0.25


def test_tensorboard_events_are_read_from_mount_path(tmp_path, paths):
    """events.out.tfevents.* 同样只在容器内路径下。"""
    pytest.importorskip("tensorboard")
    host, mount = paths
    write_artifacts(mount)
    service = build_service(make_settings(tmp_path, host, mount))

    result = service.job_tensorboard(JOB)

    assert result["path_exists"] is True, "读 events 用错了路径：容器内路径才读得到"
    assert result["series"]["loss"][-1]["value"] == 0.25


def test_checkpoint_listing_uses_mount_path(tmp_path, paths):
    """列 checkpoint 文件只看容器内路径，返回的 path 也是容器内路径。"""
    host, mount = paths
    write_artifacts(mount, with_events=False)
    service = build_service(make_settings(tmp_path, host, mount))

    result = service.job_checkpoints(JOB)

    assert result["exists"] is True, "列 checkpoint 用错了路径：容器内路径才读得到"
    names = [item["name"] for item in result["items"]]
    assert "run/checkpoints/000100/model.safetensors" in names
    assert result["path"].startswith(str(mount))
    assert str(host) not in result["path"]


def test_checkpoint_usage_counts_mount_path(tmp_path, paths):
    """磁盘占用统计若用宿主机路径，会永远显示 0 字节。"""
    host, mount = paths
    write_artifacts(mount, with_events=False)
    service = build_service(make_settings(tmp_path, host, mount))

    result = service.checkpoint_usage()

    by_job = {item["job"]: item for item in result["items"]}
    assert JOB in by_job, "统计占用用错了路径：容器内路径才读得到"
    assert by_job[JOB]["bytes"] > 0


def test_tensorboard_process_logdir_is_mount_path(tmp_path, paths):
    """TensorBoard 是 Web 容器里的子进程，--logdir 必须是容器内路径。"""
    host, mount = paths
    settings = make_settings(tmp_path, host, mount)

    process = TensorboardProcess(logdir=settings.checkpoints_mount_path, port=0)

    assert process.logdir == str(mount)
    assert process.logdir != str(host)


def test_app_wires_tensorboard_logdir_to_mount_path(tmp_path, paths, monkeypatch):
    """app.main 建 TensorboardProcess 时必须用容器内路径（不能只看单测的构造方式）。"""
    host, mount = paths
    settings = make_settings(
        tmp_path, host, mount, tensorboard_enabled=False, auth_mode="off", auth_required=False
    )
    monkeypatch.setenv("PLATFORM_DB_PATH", str(tmp_path / "import-side-effect.db"))

    from app.main import create_app

    app = create_app(settings)

    assert app.state.tensorboard.logdir == str(mount)
    assert app.state.tensorboard.logdir != str(host)


# --- 2. 构造训练 Job 用宿主机路径 --------------------------------------------


def test_pytorchjob_uses_host_path_for_hostpath_volumes(tmp_path, paths):
    """hostPath 必须是宿主机路径，同时容器内 mountPath 必须是容器内路径。

    两者用反了有两种典型故障：hostPath 写容器路径 → 训练 Pod 起不来
    （宿主机上没这个目录）；mountPath 写宿主机路径 → 训练进程写到容器临时层，
    任务一结束产物就没了。
    """
    host, mount = paths
    settings = make_settings(tmp_path, host, mount)

    body = render_pytorch_job(make_spec(), "2026-01-01T00:00:00Z", settings)
    pod = body["spec"]["pytorchReplicaSpecs"]["Master"]["template"]["spec"]
    volumes = {item["name"]: item for item in pod["volumes"]}
    mounts = {item["name"]: item for item in pod["containers"][0]["volumeMounts"]}
    env = {item["name"]: item["value"] for item in pod["containers"][0]["env"]}

    assert volumes["checkpoints"]["hostPath"]["path"] == str(host)
    assert volumes["datasets"]["hostPath"]["path"] == str(settings.dataset_host_path)
    assert mounts["checkpoints"]["mountPath"] == str(mount)
    assert env["CHECKPOINT_DIR"] == f"{mount}/{JOB}"
    # 两组路径不能互相串门
    assert str(mount) not in volumes["checkpoints"]["hostPath"]["path"]
    assert mounts["checkpoints"]["mountPath"] != str(host)


# --- 3. 两者不同时的端到端行为 ----------------------------------------------


def test_host_and_mount_paths_differ_and_reads_follow_mount(tmp_path, paths):
    """宿主机路径下没有产物时读结果为空；同一份产物放到容器内路径就读得到。

    这条用例同时是「路径搞混了」的回归哨兵：如果谁把某处改回
    ``checkpoint_host_path``，前半段断言不会失败但后半段一定会。
    """
    host, mount = paths
    settings = make_settings(tmp_path, host, mount)
    assert str(host) != str(mount)

    service = build_service(settings)
    # 还没造产物：两个路径都读不到，接口降级为 exists=False
    assert service.job_metrics(JOB)["path_exists"] is False
    assert service.job_checkpoints(JOB)["exists"] is False
    assert service.job_tensorboard(JOB)["path_exists"] is False

    pytest.importorskip("tensorboard")
    write_artifacts(mount)

    assert service.job_metrics(JOB)["path_exists"] is True
    assert service.job_checkpoints(JOB)["exists"] is True
    assert service.job_tensorboard(JOB)["path_exists"] is True
    # 宿主机路径确实仍然是空的：证明上面读到的不是它
    assert list(host.iterdir()) == []