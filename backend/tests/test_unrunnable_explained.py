"""容器没起来 / 镜像没导入 / 数据集消失时，平台要说人话。

三个真实事故的共同点：**任务跑不起来，但用户看不懂为什么**。

1. 镜像只在 buildah 里有、没导入 k3s → Pod 卡在 ImagePullBackOff →
   读日志 400 → 前端显示「Kubernetes rejected the request: 400 Bad Request」。
2. 诊断只能靠事件里的 "Failed to pull image"，事件过期或只有
   "Back-off pulling image" 时，说不清是哪个镜像、该怎么修。
3. 数据集目录被删了，列表页仍读 SQLite 缓存显示 status=ready，
   用户选它提交，任务占住 GPU 才在容器里报 No such file or directory。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from app.config import Settings
from app.datasets.store import DatasetStore
from app.history import HistoryStore
from app.models import SubmitSpec
from app.services.diagnostics import WAITING_REASON_CODES, diagnose
from app.services.errors import ServiceError
from app.services.platform import PlatformService

WAITING_CODES = set(WAITING_REASON_CODES)

# 事故里的真实镜像名：buildah 里有，k3s 里没有。
MISSING_IMAGE = "vla-smolvla:latest"


def make_spec(name: str = "img-missing", **overrides) -> SubmitSpec:
    payload = {
        "name": name,
        "image": MISSING_IMAGE,
        "gpus": 1,
        "queue": "default",
        "priority": "normal",
        "command": "python /app/train.py",
        "env": {},
        "max_retries": 0,
    }
    payload.update(overrides)
    return SubmitSpec(**payload)


def add_waiting_pod(
    kube,
    job_name: str,
    reason: str = "ImagePullBackOff",
    message: str = 'Back-off pulling image "vla-smolvla:latest"',
    *,
    image: str = MISSING_IMAGE,
    pod_level: bool = False,
):
    """Pod 卡在拉镜像：容器状态是 waiting，Pod 本身 Pending。"""
    pod = {
        "metadata": {
            "name": f"{job_name}-master-0",
            "uid": f"pod-uid-{job_name}",
            "labels": {"vla-platform/job-name": job_name},
            "creationTimestamp": "2026-01-01T00:00:05Z",
        },
        "spec": {
            "containers": [
                {"name": "pytorch", "image": image, "resources": {}},
            ]
        },
        "status": {"phase": "Pending", "conditions": []},
    }
    if pod_level:
        # 镜像还没拉下来时 kubelet 有时只写 Pod 级别的 reason。
        pod["status"]["reason"] = reason
        pod["status"]["message"] = message
    else:
        pod["status"]["container_statuses"] = [
            {
                "name": "pytorch",
                "ready": False,
                "state": {"waiting": {"reason": reason, "message": message}},
            }
        ]
    kube.pods.append(pod)
    return pod


def _fail_job(kube, name: str, message: str) -> None:
    kube.jobs[name]["status"] = {
        "conditions": [
            {
                "type": "Failed",
                "status": True,
                "message": message,
                "lastTransitionTime": "2026-01-01T00:05:00Z",
            }
        ]
    }


@pytest.fixture
def service(settings, history, kube):
    return PlatformService(settings, history, kube)


# ---------------------------------------------------------------------------
# 1) 日志：容器 waiting 时返回说明，而不是 K8s 的 400
# ---------------------------------------------------------------------------
def test_logs_explains_image_pull_backoff_instead_of_k8s_error(service, kube):
    service.submit(make_spec())
    pod = add_waiting_pod(kube, "img-missing", image="")

    # 真实客户端在容器没起来时调日志接口会拿到 400；这里模拟「根本没有可读容器」。
    def boom(*_args, **_kwargs):
        raise ServiceError(422, "Kubernetes rejected the request: 400 Bad Request")

    with patch.object(kube, "read_pod_log", side_effect=boom):
        result = service.logs("img-missing", 100)

    assert result["pod_name"] == pod["metadata"]["name"]
    assert "Kubernetes rejected" not in result["logs"]
    assert "400" not in result["logs"]
    assert "ImagePullBackOff" in result["logs"]
    assert "镜像拉取失败" in result["logs"]
    # 建议要能直接照着做：说清镜像在节点上不存在 + buildah → ctr 导入
    assert MISSING_IMAGE in result["logs"]
    assert "k3s ctr -n k8s.io images import" in result["logs"]
    assert "buildah push" in result["logs"]
    assert result["container_waiting"] == "waiting"
    assert result["container_reason"] == "ImagePullBackOff"


def test_logs_reports_pod_level_image_pull_backoff(service, kube):
    """容器状态还没写进来时，Pod 级别的 reason 同样要认出来。"""
    service.submit(make_spec())
    add_waiting_pod(
        kube, "img-missing", reason="ErrImagePull", message="rpc error: 403 Forbidden", pod_level=True
    )

    result = service.logs("img-missing", 100)

    assert result["container_reason"] == "ErrImagePull"
    assert "镜像拉取失败" in result["logs"]
    assert "403 Forbidden" in result["logs"]


def test_logs_explains_crash_loop_backoff(service, kube):
    service.submit(make_spec())
    add_waiting_pod(kube, "img-missing", reason="CrashLoopBackOff", message="back-off 40s")

    result = service.logs("img-missing", 100)

    assert "CrashLoopBackOff" in result["logs"]
    assert "反复退出" in result["logs"]
    # 崩溃重启和镜像问题要分开说，否则用户会去改一个没问题的镜像
    assert "镜像拉取失败" not in result["logs"]


def test_logs_still_return_output_while_container_is_creating(service, kube):
    """ContainerCreating 是正常过渡态：给提示，不能说成失败。"""
    service.submit(make_spec())
    add_waiting_pod(kube, "img-missing", reason="ContainerCreating", message="")

    result = service.logs("img-missing", 100)

    assert "容器正在创建中" in result["logs"]
    assert "失败" not in result["logs"].splitlines()[0]


def test_logs_reads_container_output_when_pod_runs(service, kube):
    """容器起来了就必须照常读日志，别被 waiting 检查误伤。"""
    service.submit(make_spec())
    pod = add_waiting_pod(kube, "img-missing", reason="")
    pod["status"]["container_statuses"] = [{"state": {"running": {"started_at": "2026-01-01T00:00:09Z"}}}]
    pod["status"]["phase"] = "Running"
    kube.logs[pod["metadata"]["name"]] = "step=1 loss=0.5\n"

    result = service.logs("img-missing", 100)

    assert "loss=0.5" in result["logs"]
    assert "container_reason" not in result


def test_logs_explain_missing_pod(service, kube):
    service.submit(make_spec())
    result = service.logs("img-missing", 100)
    assert result["pod_name"] is None
    assert "训练 Pod 还没有创建" in result["logs"]


# ---------------------------------------------------------------------------
# 2) 诊断：ImagePullBackOff 要落到「镜像拉取失败」，并点名镜像
# ---------------------------------------------------------------------------
def test_diagnosis_says_image_pull_failed_with_actionable_fix(service, kube):
    service.submit(make_spec())
    add_waiting_pod(kube, "img-missing")
    # 卡在拉镜像的任务，PyTorchJob 要等 backoffLimit 用完才会被判失败；
    # 这里直接给它失败的终态条件，模拟退避耗尽之后用户点「诊断」。
    _fail_job(kube, "img-missing", "pytorch exited with ImagePullBackOff")

    result = service.job_diagnosis("img-missing")

    diagnosis = result["diagnosis"]
    assert diagnosis is not None
    assert diagnosis["code"] == "image_pull_backoff"
    assert diagnosis["summary"] == "镜像拉取失败"
    # 「镜像 X 在节点上不存在 + 用 buildah push 导入 k3s」
    assert MISSING_IMAGE in diagnosis["suggestion"]
    assert "在节点上不存在" in diagnosis["suggestion"]
    assert "buildah push" in diagnosis["suggestion"]
    assert "k3s ctr -n k8s.io images import" in diagnosis["suggestion"]
    assert "ImagePullBackOff" in diagnosis["evidence"]


def test_diagnose_maps_waiting_reason_without_events():
    """事件已被回收（只剩 waiting 状态）时也要能诊断出镜像问题。"""
    result = diagnose(status="FAILED", waiting_reason="ImagePullBackOff: Back-off pulling image", image="foo:v1")

    assert result is not None
    assert result["code"] == "image_pull_backoff"
    assert "foo:v1" in result["suggestion"]


def test_diagnose_works_without_image_name():
    """拿不到镜像名时建议里不能出现空串或未格式化的占位符。"""
    result = diagnose(status="FAILED", waiting_reason="ErrImagePull")

    assert result is not None
    assert "{" not in result["suggestion"]
    assert "<镜像名>" in result["suggestion"]


def test_diagnose_crash_loop_is_not_reported_as_oom():
    result = diagnose(status="FAILED", waiting_reason="CrashLoopBackOff")

    assert result is not None
    # 只用现有诊断码（前端按 code 选图标/过滤），但文案必须说成启动失败而非内存超限
    assert result["code"] in {code for code, _ in WAITING_CODES}
    assert "CrashLoopBackOff" in result["summary"]
    assert "内存" not in result["summary"]
    assert "日志" in result["suggestion"]


def test_diagnose_prefers_waiting_reason_over_log_tail():
    """日志里恰好有 Traceback，但容器根本没起来——根因是镜像，不是脚本。"""
    result = diagnose(
        status="FAILED",
        waiting_reason="ImagePullBackOff",
        log_tail="Traceback (most recent call last):\nValueError: whatever",
    )

    assert result is not None
    assert result["code"] == "image_pull_backoff"


# ---------------------------------------------------------------------------
# 3) 数据集：目录没了，状态必须变
# ---------------------------------------------------------------------------
def _register_dataset(store: DatasetStore, root: Path, name: str = "pick-3.0") -> Path:
    directory = root / name
    (directory / "meta").mkdir(parents=True, exist_ok=True)
    (directory / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    info = {
        "codebase_version": "v3.0",
        "total_episodes": 12,
        "total_frames": 4000,
        "fps": 30,
        "features": {
            "observation.state": {"dtype": "float32", "shape": [7]},
            "action": {"dtype": "float32", "shape": [7]},
            "observation.images.top": {"dtype": "video", "shape": [480, 640, 3]},
        },
    }
    (directory / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")
    (directory / "data" / "chunk-000" / "episode_000000.parquet").write_bytes(b"x")
    store.scan_one(root, name)
    return directory


@pytest.fixture
def datasets_service(tmp_path):
    root = tmp_path / "datasets"
    root.mkdir()
    settings = Settings(
        api_token="test-token",
        auth_required=False,
        db_path=str(tmp_path / "datasets.db"),
        gpu_metrics_url="",
        datasets_mount_path=str(root),
    )

    class _NoKube:
        def list_jobs(self):
            return []

        def list_namespace_pods(self):
            return []

    return PlatformService(settings, HistoryStore(settings.db_path), _NoKube()), root


def test_dataset_ready_when_directory_exists(datasets_service):
    service, root = datasets_service
    _register_dataset(service.dataset_store(), root)

    result = service.list_datasets()

    item = result["items"][0]
    assert item["status"] == "ready"
    assert result["summary"]["ready"] == 1
    assert result["summary"]["missing"] == 0


def test_dataset_missing_when_directory_removed(datasets_service):
    service, root = datasets_service
    directory = _register_dataset(service.dataset_store(), root)
    assert service.list_datasets()["items"][0]["status"] == "ready"

    import shutil

    shutil.rmtree(directory)

    result = service.list_datasets()
    item = result["items"][0]
    assert item["status"] == "missing"
    assert f"数据集目录已不存在：{directory}" in [issue["message"] for issue in item["issues"]]
    assert all(issue["level"] == "error" for issue in item["issues"] if issue["code"] == "path_missing")
    # missing 必须计入「不可用」，否则页面顶部写着 0 个不可用，列表却全是红的
    assert result["summary"]["missing"] == 1
    assert result["summary"]["unusable"] == 1
    assert result["summary"]["ready"] == 0


def test_dataset_status_recovers_when_directory_returns(datasets_service):
    """目录只是暂时消失（挂载盘掉了又回来），状态要跟着回来，不能写死。"""
    service, root = datasets_service
    directory = _register_dataset(service.dataset_store(), root)
    payload = service.list_datasets()["items"][0]
    original_path = directory.with_name("pick-3.0-offline")
    directory.rename(original_path)
    assert service.list_datasets()["items"][0]["status"] == "missing"

    original_path.rename(directory)

    assert service.list_datasets()["items"][0]["status"] == "ready"
    assert payload["id"] == service.list_datasets()["items"][0]["id"]


def test_submit_preflight_blocks_missing_dataset(datasets_service):
    """目录没了要在提交前就挡住，而不是让任务占住 GPU 再失败。"""
    from app.services.preflight import preflight

    service, root = datasets_service
    directory = _register_dataset(service.dataset_store(), root)
    import shutil

    shutil.rmtree(directory)
    dataset = service.list_datasets()["items"][0]
    assert dataset["status"] == "missing"

    result = preflight(
        dataset=dataset,
        recipe={"name": "smolvla", "dataset_formats": ["lerobot-v3"], "gpu_count": 1},
        gpu_total=1,
        gpu_allocated=0,
        requested_gpus=1,
    )

    assert result["ok"] is False
    codes = {check["code"] for check in result["checks"]}
    assert "dataset_missing" in codes
    suggestion = next(c["suggestion"] for c in result["checks"] if c["code"] == "dataset_missing")
    assert "重新扫描" in suggestion
