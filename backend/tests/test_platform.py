from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.config import Settings
from app.history import HistoryStore
from app.models import SubmitSpec
from app.services.errors import ServiceError
from app.services.platform import PlatformService


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


def add_pod(kube, job_name, phase="Running", waiting_reason=None, node_name=None):
    pod = {
        "metadata": {
            "name": f"{job_name}-master-0",
            "uid": f"pod-uid-{job_name}",
            "labels": {"vla-platform/job-name": job_name},
            "creationTimestamp": "2026-01-01T00:00:05Z",
        },
        "spec": {
            "node_name": node_name,
            "containers": [{"resources": {"requests": {"nvidia.com/gpu": "1"}}}],
        },
        "status": {"phase": phase, "conditions": []},
    }
    if waiting_reason:
        pod["status"]["container_statuses"] = [
            {"state": {"waiting": {"reason": waiting_reason, "message": "boom"}}}
        ]
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
    assert job["status"] == "QUEUED"
    assert "ImagePullBackOff" in job["message"], "已放行但 Pod 未就绪时必须给出真实原因"


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


def test_cancel_finished_job_preserves_success(service, kube):
    service.submit(make_spec())
    set_job_condition(kube, "exp001", "Succeeded")
    result = service.cancel("exp001")
    assert result["status"] == "SUCCEEDED", "删除已成功的任务不能改写成 CANCELLED"
    assert "Deleted after" in result["message"]


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
    assert result["logs"] == ""


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
    summary = service.cluster_summary()
    assert summary["gpu_total"] == 1
    assert summary["gpu_allocated"] == 1
    assert summary["nodes"][0]["ready"] is True

    kube.pods[0]["status"]["phase"] = "Succeeded"
    assert service.cluster_summary()["gpu_allocated"] == 0


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
