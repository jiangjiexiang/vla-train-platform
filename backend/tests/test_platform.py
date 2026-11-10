from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from app.config import Settings
from app.history import HistoryStore
from app.models import SubmitSpec
from app.services.errors import ServiceError
from app.services.platform import (
    PlatformService,
    _is_permanent_start_failure,
    _pod_waiting_reason,
)


def make_spec(name="exp001", **overrides):
    payload = {
        "name": name,
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


def add_pod(kube, job_name, phase="Running", waiting_reason=None, node_name=None,
            name=None, created_at="2026-01-01T00:00:05Z", started_at=None,
            init_waiting_reason=None, pod_scheduled=None):
    """构造一个训练 Pod。

    ``waiting_reason`` 写普通容器的 waiting 状态；
    ``init_waiting_reason`` 写 initContainer 的 waiting 状态（训练 Pod 带
    init-permissions 那个 initContainer，卡住时只会体现在
    initContainerStatuses 里）。``pod_scheduled=None`` 表示不写
    PodScheduled 条件；``False`` + 原因表示调度不上。
    """
    pod = {
        "metadata": {
            "name": name or f"{job_name}-master-0",
            "uid": f"pod-uid-{name or job_name}",
            "labels": {"vla-platform/job-name": job_name},
            "creationTimestamp": created_at,
        },
        "spec": {
            "node_name": node_name,
            "containers": [{"resources": {"requests": {"nvidia.com/gpu": "1"}}}],
        },
        "status": {"phase": phase, "conditions": []},
    }
    if started_at:
        pod["status"]["start_time"] = started_at
    if waiting_reason:
        pod["status"]["container_statuses"] = [
            {"state": {"waiting": {"reason": waiting_reason, "message": "boom"}}}
        ]
    if init_waiting_reason:
        pod["status"]["init_container_statuses"] = [
            {
                "name": "init-permissions",
                "state": {"waiting": {"reason": init_waiting_reason, "message": "init boom"}},
            }
        ]
    if pod_scheduled is False:
        pod["status"]["conditions"].append(
            {"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
             "message": '0/3 nodes are available: 3 Insufficient nvidia.com/gpu.'}
        )
    elif pod_scheduled is True:
        pod["status"]["conditions"].append({"type": "PodScheduled", "status": "True"})
    kube.pods.append(pod)
    return pod


def add_workload(kube, job_name, condition=None):
    workload = {
        "metadata": {"name": f"pytorchjob-{job_name}-abc", "labels": {"vla-platform/job-name": job_name}},
        "status": {"conditions": []},
    }
    if condition:
        workload["status"]["conditions"].append(
            {"type": condition, "status": True, "lastTransitionTime": "2026-01-01T00:00:06Z"}
        )
    kube.workloads.append(workload)
    return workload


def set_job_condition(kube, name, condition_type):
    kube.jobs[name]["status"] = {
        "conditions": [
            {
                "type": condition_type,
                "status": True,
                "lastTransitionTime": "2026-01-01T00:01:00Z",
                "message": f"PyTorchJob {name} is {condition_type.lower()}.",
            }
        ]
    }


# --- submit ---------------------------------------------------------------

def test_submit_creates_job_and_records_history(service, kube, history):
    job = service.submit(make_spec())
    assert job["status"] == "QUEUED"
    assert "exp001" in kube.jobs
    assert history.latest_submission("exp001") is not None


def test_submit_rejects_duplicate(service, kube):
    service.submit(make_spec())
    with pytest.raises(ServiceError) as excinfo:
        service.submit(make_spec())
    assert excinfo.value.status_code == 409


def test_submit_rejects_disallowed_gpu_count(service):
    with pytest.raises(ServiceError) as excinfo:
        service.submit(make_spec(gpus=2))
    assert excinfo.value.status_code == 422


def test_submit_rejects_unknown_queue(service):
    with pytest.raises(ServiceError) as excinfo:
        service.submit(make_spec(queue="other"))
    assert excinfo.value.status_code == 422


def test_failed_create_leaves_no_history(service, kube, history):
    kube.create_job = lambda body: (_ for _ in ()).throw(ServiceError(500, "boom"))
    with pytest.raises(ServiceError):
        service.submit(make_spec())
    assert history.latest_submission("exp001") is None, "创建失败不应留下提交记录"


# --- status derivation ----------------------------------------------------

def test_response_exposes_grace_period(service, kube):
    service.submit(make_spec(termination_grace_period_seconds=7))
    assert service.get_job("exp001")["termination_grace_period_seconds"] == 7


def test_cancel_response_keeps_grace_period(service, kube):
    service.submit(make_spec(termination_grace_period_seconds=9))
    add_pod(kube, "exp001", phase="Running")
    assert service.cancel("exp001")["termination_grace_period_seconds"] == 9


def test_status_queued_without_workload(service, kube):
    service.submit(make_spec())
    job = service.get_job("exp001")
    assert job["status"] == "QUEUED"
    assert job["message"] == "Waiting for Kueue admission"


def test_status_running_with_running_pod(service, kube):
    service.submit(make_spec())
    add_workload(kube, "exp001", "Admitted")
    add_pod(kube, "exp001", phase="Running")
    job = service.get_job("exp001")
    assert job["status"] == "RUNNING"
    assert job["workload_status"] == "Admitted"


def test_status_succeeded_from_job_condition(service, kube):
    service.submit(make_spec())
    set_job_condition(kube, "exp001", "Succeeded")
    job = service.get_job("exp001")
    assert job["status"] == "SUCCEEDED"
    assert job["message"].endswith("succeeded.")


def test_status_failed_from_job_condition(service, kube):
    service.submit(make_spec())
    set_job_condition(kube, "exp001", "Failed")
    assert service.get_job("exp001")["status"] == "FAILED"


def test_message_reports_pod_waiting_reason(service, kube):
    service.submit(make_spec())
    add_workload(kube, "exp001", "Admitted")
    add_pod(kube, "exp001", phase="Pending", waiting_reason="ImagePullBackOff")
    job = service.get_job("exp001")
    # 镜像拉不回来不会自己好，必须报「启动失败」而不是让用户一直等「排队中」。
    assert job["status"] == "STARTING_FAILED"
    # 镜像类失败的消息是给人看的操作指引，不一定含原始 reason 字符串；
    # 这里断言的是「说清了是什么问题、哪个镜像」。
    assert "镜像拉取失败" in job["message"]
    assert "vla-smoke:latest" in job["message"], "已放行但 Pod 未就绪时必须给出真实原因"


# --- permanent start failure vs. transient waiting ------------------------

def test_pending_image_pull_backoff_marks_starting_failed(service, kube):
    service.submit(make_spec(image="vla-smoke:latest"))
    add_workload(kube, "exp001", "Admitted")
    add_pod(kube, "exp001", phase="Pending", waiting_reason="ImagePullBackOff")
    job = service.get_job("exp001")
    assert job["status"] == "STARTING_FAILED"
    assert "镜像拉取失败" in job["message"]
    assert "vla-smoke:latest" in job["message"], "消息里要带镜像名，用户才知道该导入哪个"


def test_pending_err_image_pull_marks_starting_failed(service, kube):
    service.submit(make_spec(image="ghcr.io/acme/train:cache"))
    add_workload(kube, "exp001", "Admitted")
    add_pod(kube, "exp001", phase="Pending", waiting_reason="ErrImagePull")
    job = service.get_job("exp001")
    assert job["status"] == "STARTING_FAILED"
    assert "镜像拉取失败" in job["message"]
    assert "ghcr.io/acme/train:cache" in job["message"]


def test_pending_invalid_image_name_marks_starting_failed(service, kube):
    service.submit(make_spec(image="Not A Valid Image"))
    add_workload(kube, "exp001", "Admitted")
    add_pod(kube, "exp001", phase="Pending", waiting_reason="InvalidImageName")
    job = service.get_job("exp001")
    assert job["status"] == "STARTING_FAILED"
    assert "镜像名不合法" in job["message"]


def test_pending_container_creating_stays_queued(service, kube):
    """正在创建容器只是慢，会自己好，不能误报成启动失败。"""
    service.submit(make_spec())
    add_workload(kube, "exp001", "Admitted")
    add_pod(kube, "exp001", phase="Pending", waiting_reason="ContainerCreating")
    job = service.get_job("exp001")
    assert job["status"] == "QUEUED"
    assert "ContainerCreating" in job["message"]


def test_pending_unschedulable_stays_queued(service, kube):
    """调度不上是在等资源，资源一空出来就会起，仍然算排队。"""
    service.submit(make_spec())
    add_workload(kube, "exp001", "Admitted")
    add_pod(kube, "exp001", phase="Pending", pod_scheduled=False)
    job = service.get_job("exp001")
    assert job["status"] == "QUEUED"
    assert "Insufficient nvidia.com/gpu" in job["message"]


def test_pending_init_container_image_pull_marks_starting_failed(service, kube):
    """initContainer 卡在 ImagePullBackOff 时也要报真实原因。

    训练 Pod 的第一个容器是 init-permissions；它拉不到镜像时 Pod 一直
    Pending，但只有 initContainerStatuses 里有 waiting reason。之前只看
    containerStatuses，这里会退化成「waiting for container start」，界面
    显示「排队中」。
    """
    service.submit(make_spec(image="vla-smoke:latest"))
    add_workload(kube, "exp001", "Admitted")
    add_pod(kube, "exp001", phase="Pending", init_waiting_reason="ImagePullBackOff")
    job = service.get_job("exp001")
    assert job["status"] == "STARTING_FAILED"
    assert "镜像拉取失败" in job["message"]
    # 关键回归点：如果不是从 initContainerStatuses 读到原因，这里会变成
    # 「waiting for container start」，也就不会有镜像相关的指引。
    assert "waiting for container start" not in job["message"]
    assert "vla-smoke:latest" in job["message"], "initContainer 卡住时要暴露真实原因"


def test_pending_init_container_without_reason_stays_queued(service, kube):
    """initContainer 正常跑着（没有 waiting reason）时不算启动失败。"""
    service.submit(make_spec())
    add_workload(kube, "exp001", "Admitted")
    pod = add_pod(kube, "exp001", phase="Pending")
    pod["status"]["init_container_statuses"] = [
        {"name": "init-permissions", "state": {"running": {"startedAt": "2026-01-01T00:00:06Z"}}}
    ]
    job = service.get_job("exp001")
    assert job["status"] == "QUEUED"


def test_pod_waiting_reason_reads_init_container_statuses():
    """直接钉住辅助函数：initContainer 的 waiting reason 优先于兜底文案。"""
    pod = {
        "status": {
            "phase": "Pending",
            "init_container_statuses": [
                {"name": "init-permissions",
                 "state": {"waiting": {"reason": "ImagePullBackOff", "message": "no such image"}}}
            ],
            "container_statuses": [],
        }
    }
    assert _pod_waiting_reason(pod) == "ImagePullBackOff: no such image"
    assert _pod_waiting_reason({"status": {"phase": "Pending"}}) == "waiting for container start"
    # 没有 Pod 时也要有可读的兜底
    assert _pod_waiting_reason(None) == "pod has not been created"


@pytest.mark.parametrize("reason", ["ImagePullBackOff", "ErrImagePull", "InvalidImageName"])
def test_permanent_start_failures_are_detected(reason):
    assert _is_permanent_start_failure(reason) is True


@pytest.mark.parametrize("reason", ["ContainerCreating", "NotScheduled", "waiting for container start"])
def test_transient_waiting_reasons_are_not_permanent(reason):
    assert _is_permanent_start_failure(reason) is False


def test_null_conditions_do_not_crash(service, kube):
    """Kubernetes serialises absent repeated fields as null, not missing."""
    service.submit(make_spec())
    add_workload(kube, "exp001", "Admitted")
    pod = add_pod(kube, "exp001", phase="Pending")
    pod["status"]["conditions"] = None
    kube.jobs["exp001"]["status"] = {"conditions": None}
    job = service.get_job("exp001")
    assert job["status"] in ("QUEUED", "RUNNING")


# --- cancel ---------------------------------------------------------------

def test_cancel_running_job_marks_cancelled(service, kube):
    service.submit(make_spec())
    add_pod(kube, "exp001", phase="Running")
    result = service.cancel("exp001")
    assert result["status"] == "CANCELLED"
    assert "exp001" in kube.deleted
    assert service.get_job("exp001")["status"] == "CANCELLED"


def test_cancel_is_idempotent_when_object_vanishes(service, kube):
    """并发取消：对象已被另一个请求删除时，应返回已有记录而不是 404。"""
    service.submit(make_spec())
    add_pod(kube, "exp001", phase="Running")
    first = service.cancel("exp001")
    assert first["status"] == "CANCELLED"

    # 模拟竞态：get_job 之后对象才消失，delete 抛 404
    kube.jobs["exp001"] = {
        "metadata": {"name": "exp001", "uid": "uid-exp001", "labels": {}, "annotations": {},
                     "creationTimestamp": "2026-01-01T00:00:00Z"},
        "spec": {},
        "status": {"conditions": []},
    }

    def racy_delete(name):
        raise ServiceError(404, "not found")

    kube.delete_job = racy_delete
    second = service.cancel("exp001")
    assert second["status"] == "CANCELLED", "并发取消不应把 404 抛给调用方"


def test_cancel_finished_job_preserves_success(service, kube):
    service.submit(make_spec())
    set_job_condition(kube, "exp001", "Succeeded")
    result = service.cancel("exp001")
    assert result["status"] == "SUCCEEDED", "删除已成功的任务不能改写成 CANCELLED"
    # 提示语按 status 分行：对一个正常完成的任务说「已删除」是误导，
    # 它其实是被 TTL 回收的，用户也没取消过它。
    assert "成功完成" in result["message"]
    assert "Deleted after" not in result["message"]


def test_cancel_unknown_job_raises_404(service):
    with pytest.raises(ServiceError) as excinfo:
        service.cancel("nope")
    assert excinfo.value.status_code == 404


# --- clone / logs / events ------------------------------------------------

def test_clone_copies_parameters(service, kube):
    service.submit(make_spec(command="python /app/train.py --steps 7", env={"A": "1"}))
    clone = service.clone("exp001", "exp002")
    assert clone["name"] == "exp002"
    assert clone["command"] == "python /app/train.py --steps 7"
    assert clone["env"] == {"A": "1"}
    assert "exp002" in kube.jobs


def test_clone_from_cancelled_job(service, kube):
    service.submit(make_spec())
    add_pod(kube, "exp001", phase="Running")
    service.cancel("exp001")
    clone = service.clone("exp001", "exp003")
    assert clone["status"] == "QUEUED"


def test_logs_empty_when_pod_absent(service, kube):
    service.submit(make_spec())
    result = service.logs("exp001", 100)
    assert result["pod_name"] is None
    # 不是 K8s 报错，而是一句解释：Pod 还没创建。
    assert result["logs"].startswith("训练 Pod 还没有创建")


def test_logs_returned_when_pod_exists(service, kube):
    service.submit(make_spec())
    pod = add_pod(kube, "exp001", phase="Running")
    kube.logs[pod["metadata"]["name"]] = "step=1 loss=0.5\n"
    result = service.logs("exp001", 100)
    assert result["pod_name"] == pod["metadata"]["name"]
    assert "loss=0.5" in result["logs"]


def test_events_sorted_and_shaped(service, kube):
    service.submit(make_spec())
    pod = add_pod(kube, "exp001", phase="Pending", waiting_reason="ImagePullBackOff")
    kube.events = [
        {
            "metadata": {"uid": "e1", "creation_timestamp": "2026-01-01T00:00:01Z"},
            "involved_object": {"kind": "Pod", "name": pod["metadata"]["name"]},
            "type": "Warning",
            "reason": "Failed",
            "message": "Failed to pull image",
            "count": 3,
            "last_timestamp": "2026-01-01T00:00:09Z",
        },
        {
            "metadata": {"uid": "e2", "creation_timestamp": "2026-01-01T00:00:00Z"},
            "involved_object": {"kind": "PyTorchJob", "name": "exp001"},
            "type": "Normal",
            "reason": "PyTorchJobCreated",
            "message": "created",
            "count": 1,
            "last_timestamp": "2026-01-01T00:00:02Z",
        },
    ]
    result = service.events("exp001")
    assert [item["reason"] for item in result["items"]] == ["Failed", "PyTorchJobCreated"]
    assert set(result["items"][0]) == {
        "type", "reason", "message", "count", "object_kind", "object_name", "first_seen", "last_seen"
    }


def test_events_unknown_job_raises_404(service):
    with pytest.raises(ServiceError) as excinfo:
        service.events("nope")
    assert excinfo.value.status_code == 404


# --- list jobs (batched pod snapshot) -------------------------------------

def test_list_jobs_lists_pods_once(service, kube):
    for index in range(5):
        name = f"exp{index:03d}"
        service.submit(make_spec(name=name))
        add_pod(kube, name, phase="Running")
    kube.pod_list_calls = 0
    kube.job_pod_list_calls = 0

    result = service.list_jobs()

    assert result["total"] == 5
    assert kube.pod_list_calls == 1, "list_jobs 只能拉取一次命名空间级 Pod 快照"
    assert kube.job_pod_list_calls == 0, "不得再按任务逐个查询 Pod（N+1）"


def test_list_jobs_attributes_pods_to_owning_job(service, kube):
    service.submit(make_spec("exp001"))
    service.submit(make_spec("exp002"))
    service.submit(make_spec("exp003"))
    # exp001: an older failed attempt plus a newer running pod -> newest wins.
    add_pod(kube, "exp001", phase="Failed", name="exp001-master-0",
            created_at="2026-01-01T00:00:05Z")
    add_pod(kube, "exp001", phase="Running", name="exp001-master-1",
            created_at="2026-01-01T00:02:00Z", started_at="2026-01-01T00:02:10Z")
    # exp002: its pod is newer than exp001's, so cross-contamination would show.
    add_pod(kube, "exp002", phase="Pending", name="exp002-master-0",
            created_at="2026-01-01T00:03:00Z")

    items = {item["name"]: item for item in service.list_jobs()["items"]}

    assert items["exp001"]["pod_status"] == "Running"
    assert items["exp001"]["status"] == "RUNNING"
    assert items["exp001"]["started_at"] == "2026-01-01T00:02:10Z"
    assert items["exp002"]["pod_status"] == "Pending"
    assert items["exp002"]["status"] == "QUEUED"
    assert items["exp003"]["pod_status"] is None, "没有 Pod 的任务不能拿到别人的 Pod"
    assert items["exp003"]["status"] == "QUEUED"

    # The batched path must agree with the per-job path used by get_job().
    for name, item in items.items():
        single = service.get_job(name)
        assert (item["pod_status"], item["status"], item["started_at"]) == (
            single["pod_status"], single["status"], single["started_at"]
        )


# --- cluster summary ------------------------------------------------------

def test_cluster_summary_counts_only_live_pods(service, kube):
    kube.nodes = [
        {
            "metadata": {"name": "node1"},
            "status": {
                "allocatable": {"nvidia.com/gpu": "1"},
                "conditions": [{"type": "Ready", "status": True}],
            },
        }
    ]
    service.submit(make_spec())
    add_pod(kube, "exp001", phase="Running", node_name="node1")
    # 本用例要验证「Pod 结束后不再计入」，需要绕过 2 秒的摘要缓存
    service._cluster_cache = None
    summary = service.cluster_summary()
    assert summary["gpu_total"] == 1
    assert summary["gpu_allocated"] == 1
    assert summary["nodes"][0]["ready"] is True

    kube.pods[0]["status"]["phase"] = "Succeeded"
    service._cluster_cache = None
    assert service.cluster_summary()["gpu_allocated"] == 0


def test_cluster_summary_reuses_one_pod_snapshot(service, kube):
    kube.nodes = [
        {
            "metadata": {"name": "node1"},
            "status": {
                "allocatable": {"nvidia.com/gpu": "2"},
                "conditions": [{"type": "Ready", "status": True}],
            },
        }
    ]
    for name in ("exp001", "exp002"):
        service.submit(make_spec(name=name))
        add_pod(kube, name, phase="Running", node_name="node1")
    kube.pod_list_calls = 0
    kube.job_pod_list_calls = 0

    summary = service.cluster_summary()

    assert summary["gpu_allocated"] == 2
    assert summary["jobs"]["running"] == 2
    assert kube.pod_list_calls == 1, "summary 的 GPU 统计与任务状态必须共用一次 Pod 快照"
    assert kube.job_pod_list_calls == 0


def test_queues_lists_pods_once(service, kube):
    kube.local_queue = {
        "metadata": {"name": "default"},
        "spec": {"clusterQueue": "default"},
        "status": {},
    }
    kube.cluster_queue = {
        "spec": {
            "resourceGroups": [
                {
                    "coveredResources": ["nvidia.com/gpu"],
                    "flavors": [{"resources": [{"name": "nvidia.com/gpu", "nominalQuota": 4}]}],
                }
            ]
        }
    }
    for name in ("exp001", "exp002"):
        service.submit(make_spec(name=name))
        add_pod(kube, name, phase="Running")
    kube.pod_list_calls = 0
    kube.job_pod_list_calls = 0

    items = service.queues()["items"][0]["items"]

    assert {item["name"] for item in items} == {"exp001", "exp002"}
    assert all(item["status"] == "RUNNING" for item in items)
    assert kube.pod_list_calls == 1
    assert kube.job_pod_list_calls == 0


def test_gpu_metrics_unavailable_without_service(service):
    assert service.gpu_metrics() == {"available": False, "items": []}


def test_gpu_metrics_parsed_and_sorted(tmp_path, kube):
    settings = Settings(
        api_token="t",
        auth_required=False,
        db_path=str(tmp_path / "metrics.db"),
        gpu_metrics_url="http://metrics.invalid/",
    )
    service = PlatformService(settings, HistoryStore(settings.db_path), kube)
    payload = [
        {"index": 1, "name": "GPU1", "utilization_percent": 5, "memory_used_mib": 10,
         "memory_total_mib": 20, "temperature_celsius": 30},
        {"index": 0, "name": "GPU0", "utilization_percent": "95", "memory_used_mib": "38747",
         "memory_total_mib": 49140, "temperature_celsius": 55.4},
    ]

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode()

    with patch("app.services.platform.urllib.request.urlopen", return_value=FakeResponse()):
        result = service.gpu_metrics()

    assert result["available"] is True
    assert [item["index"] for item in result["items"]] == [0, 1], "应按 index 排序"
    assert result["items"][0]["utilization_percent"] == 95
    assert result["items"][0]["temperature_celsius"] == 55


def test_gpu_metrics_cached_within_ttl(tmp_path, kube):
    settings = Settings(
        api_token="t",
        auth_required=False,
        db_path=str(tmp_path / "cache.db"),
        gpu_metrics_url="http://metrics.invalid/",
        gpu_metrics_ttl=60.0,
    )
    service = PlatformService(settings, HistoryStore(settings.db_path), kube)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps([{"index": 0, "name": "GPU0", "utilization_percent": 1,
                                "memory_used_mib": 2, "memory_total_mib": 3,
                                "temperature_celsius": 4}]).encode()

    with patch("app.services.platform.urllib.request.urlopen", return_value=FakeResponse()) as mocked:
        first = service.gpu_metrics()
        second = service.gpu_metrics()
        assert mocked.call_count == 1, "TTL 内应复用缓存，不重复请求指标服务"
    assert first == second


def test_gpu_metrics_cache_disabled_when_ttl_zero(tmp_path, kube):
    settings = Settings(
        api_token="t",
        auth_required=False,
        db_path=str(tmp_path / "nocache.db"),
        gpu_metrics_url="http://metrics.invalid/",
        gpu_metrics_ttl=0.0,
    )
    service = PlatformService(settings, HistoryStore(settings.db_path), kube)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps([]).encode()

    with patch("app.services.platform.urllib.request.urlopen", return_value=FakeResponse()) as mocked:
        service.gpu_metrics()
        service.gpu_metrics()
        assert mocked.call_count == 2


def test_gpu_metrics_caches_failures(tmp_path, kube):
    """指标服务挂掉时也要缓存失败，否则每个请求都要等满 2 秒超时。"""
    settings = Settings(
        api_token="t",
        auth_required=False,
        db_path=str(tmp_path / "failcache.db"),
        gpu_metrics_url="http://metrics.invalid/",
        gpu_metrics_ttl=60.0,
    )
    service = PlatformService(settings, HistoryStore(settings.db_path), kube)
    with patch("app.services.platform.urllib.request.urlopen", side_effect=OSError("down")) as mocked:
        first = service.gpu_metrics()
        second = service.gpu_metrics()
        assert mocked.call_count == 1
    assert first == second == {"available": False, "items": []}


def test_gpu_metrics_degrades_on_error(tmp_path, kube):
    settings = Settings(
        api_token="t",
        auth_required=False,
        db_path=str(tmp_path / "metrics2.db"),
        gpu_metrics_url="http://metrics.invalid/",
    )
    service = PlatformService(settings, HistoryStore(settings.db_path), kube)
    with patch("app.services.platform.urllib.request.urlopen", side_effect=OSError("down")):
        assert service.gpu_metrics() == {"available": False, "items": []}


# --- list jobs (filtering / pagination) -----------------------------------

def set_created_at(kube, name, timestamp):
    kube.jobs[name]["metadata"]["annotations"]["vla-platform/submitted-at"] = timestamp


def submit_with_created_at(service, kube, name, timestamp):
    service.submit(make_spec(name))
    set_created_at(kube, name, timestamp)


def job_names(result):
    return [item["name"] for item in result["items"]]


def test_list_jobs_without_filters_keeps_legacy_response_shape(service, kube):
    for index, name in enumerate(("exp001", "exp002", "exp003")):
        submit_with_created_at(service, kube, name, f"2026-01-01T00:00:0{index + 1}Z")

    result = service.list_jobs()

    assert set(result) == {"items", "total", "limit", "offset"}
    assert job_names(result) == ["exp003", "exp002", "exp001"]
    assert result["total"] == len(result["items"]) == 3
    assert result["limit"] is None
    assert result["offset"] == 0


def test_list_jobs_limit_offset_slice_after_created_at_sort(service, kube):
    for index, name in enumerate(f"exp{item:03d}" for item in range(5)):
        submit_with_created_at(service, kube, name, f"2026-01-01T00:0{index}:00Z")

    result = service.list_jobs(limit=2, offset=1)

    assert job_names(result) == ["exp003", "exp002"]
    assert result["total"] == 5
    assert result["limit"] == 2
    assert result["offset"] == 1


def test_list_jobs_offset_without_limit_returns_everything(service, kube):
    for index, name in enumerate(f"exp{item:03d}" for item in range(4)):
        submit_with_created_at(service, kube, name, f"2026-01-01T00:0{index}:00Z")

    result = service.list_jobs(offset=2)

    assert job_names(result) == ["exp003", "exp002", "exp001", "exp000"]
    assert result["total"] == 4
    assert result["limit"] is None
    assert result["offset"] == 0, "未传 limit 时 offset 不生效"


def test_list_jobs_total_counts_filtered_items_before_pagination(service, kube):
    for index, name in enumerate(("exp-alpha", "exp-alpine", "exp-beta", "other")):
        submit_with_created_at(service, kube, name, f"2026-01-01T00:0{index}:00Z")

    result = service.list_jobs(q="AL", limit=1)

    assert result["total"] == 2, "total 必须是过滤后、分页前的总数"
    assert len(result["items"]) == 1
    assert result["items"][0]["name"] == "exp-alpine"


def test_list_jobs_status_filter_applies_to_cancelled_history(service, kube):
    service.submit(make_spec("exp-live"))
    add_pod(kube, "exp-live", phase="Running")
    service.submit(make_spec("exp-gone"))
    add_pod(kube, "exp-gone", phase="Running")
    service.cancel("exp-gone")
    service.submit(make_spec("exp-waiting"))

    assert job_names(service.list_jobs(status="RUNNING")) == ["exp-live"]
    assert job_names(service.list_jobs(status="CANCELLED")) == ["exp-gone"]
    assert job_names(service.list_jobs(status="QUEUED")) == ["exp-waiting"]

    cancelled = service.list_jobs(status="CANCELLED")
    assert cancelled["total"] == 1
    assert cancelled["items"][0]["status"] == "CANCELLED"


def test_list_jobs_status_filter_supports_succeeded_and_failed(service, kube):
    service.submit(make_spec("exp-ok"))
    set_job_condition(kube, "exp-ok", "Succeeded")
    service.submit(make_spec("exp-bad"))
    set_job_condition(kube, "exp-bad", "Failed")

    assert job_names(service.list_jobs(status="SUCCEEDED")) == ["exp-ok"]
    assert job_names(service.list_jobs(status="FAILED")) == ["exp-bad"]


def test_list_jobs_q_is_case_insensitive_and_matches_cancelled_history(service, kube):
    service.submit(make_spec("exp-cancelled-run"))
    add_pod(kube, "exp-cancelled-run", phase="Running")
    service.cancel("exp-cancelled-run")
    service.submit(make_spec("exp-live-run"))
    add_pod(kube, "exp-live-run", phase="Running")

    assert job_names(service.list_jobs(q="CANCELLED")) == ["exp-cancelled-run"]
    assert job_names(service.list_jobs(q="LIVE")) == ["exp-live-run"]
    assert set(job_names(service.list_jobs(q="EXP-"))) == {"exp-cancelled-run", "exp-live-run"}

def test_cluster_summary_is_cached_within_ttl(service, kube):
    """2 秒内重复调用不应重复打 K8s API——总览页轮询与预检都会频繁读它。"""
    kube.nodes = [
        {
            "metadata": {"name": "node1"},
            "status": {"allocatable": {"nvidia.com/gpu": "1"}, "conditions": [{"type": "Ready", "status": True}]},
        }
    ]
    calls = {"n": 0}
    original = kube.list_nodes

    def counting():
        calls["n"] += 1
        return original()

    kube.list_nodes = counting

    first = service.cluster_summary()
    second = service.cluster_summary()

    assert calls["n"] == 1
    assert first == second
    # 返回的是副本，调用方修改不能污染缓存
    second["gpu_total"] = 999
    assert service.cluster_summary()["gpu_total"] == 1


def test_cluster_summary_ttl_zero_disables_cache(service, kube, settings):
    """TTL=0 时必须每次都重新读（便于排查问题）。"""
    cached_service = type(service)(
        type(settings)(**{**settings.__dict__, "cluster_summary_ttl": 0.0}),
        service.history,
        kube,
    )
    calls = {"n": 0}
    original = kube.list_nodes

    def counting():
        calls["n"] += 1
        return original()

    kube.list_nodes = counting
    cached_service.cluster_summary()
    cached_service.cluster_summary()

    assert calls["n"] == 2


# --- 终态落库：对象被 TTL 回收后仍能说出真实结局 -------------------------


def test_succeeded_job_survives_ttl_collection(service, kube):
    """成功任务的对象被回收后仍显示 SUCCEEDED 而不是 GONE。

    PyTorchJob 带 ttlSecondsAfterFinished，跑完一小时后对象会被彻底删掉。
    只要平台在对象还在时读到过终态，回收之后就应该还能说出真实结局。
    """
    service.submit(make_spec())
    set_job_condition(kube, "exp001", "Succeeded")
    # 平台轮询到了终态（这一步负责落库）
    assert service.get_job("exp001")["status"] == "SUCCEEDED"

    # 模拟 ttl 回收：对象从集群里消失
    kube.jobs.pop("exp001")

    job = service.get_job("exp001")
    assert job["status"] == "SUCCEEDED"
    assert job["status"] != "GONE"


def test_failed_job_survives_ttl_collection(service, kube):
    """失败任务同理：回收后仍显示 FAILED 而不是 GONE。"""
    service.submit(make_spec())
    set_job_condition(kube, "exp001", "Failed")
    assert service.get_job("exp001")["status"] == "FAILED"

    kube.jobs.pop("exp001")

    job = service.get_job("exp001")
    assert job["status"] == "FAILED"
    assert job["status"] != "GONE"


def test_vanished_job_without_recorded_terminal_stays_gone(service, kube, history):
    """从没读到过终态的对象消失后必须仍是 GONE，不能编一个结局。

    编造终态比不解释更糟：用户会把「平台猜的」当成「平台知道的」。
    """
    service.submit(make_spec())
    # 平台一次都没读到终态，对象就没了（比如轮询间隔内被删）
    kube.jobs.pop("exp001")

    job = service.get_job("exp001")
    assert job["status"] == "GONE"
    assert history.get_cancellation("exp001") is None, "GONE 不应反写终态记录"


def test_vanished_job_keeps_submitted_fields(service, kube):
    """对象消失后仍能告诉用户当初提交的镜像/命令，方便重跑。"""
    service.submit(make_spec(image="vla-smoke:latest", command="python /app/train.py --steps 3"))
    kube.jobs.pop("exp001")

    job = service.get_job("exp001")
    assert job["status"] == "GONE"
    assert job["image"] == "vla-smoke:latest"
    assert job["command"] == "python /app/train.py --steps 3"

