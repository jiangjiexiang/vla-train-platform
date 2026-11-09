from __future__ import annotations

import pytest
from app.models import SubmitSpec

DEFAULT_IMAGE = "vla-smoke:latest"
DEFAULT_COMMAND = "python /app/train.py"
MAX_COMMAND_BYTES = 64 * 1024


def payload(name="api-job", **overrides):
    data = {
        "name": name,
        "gpus": 1,
        "queue": "default",
        "priority": "normal",
        "command": DEFAULT_COMMAND,
    }
    data.update(overrides)
    return data


def make_spec(name="api-job", **overrides):
    data = {
        "name": name,
        "image": DEFAULT_IMAGE,
        "gpus": 1,
        "queue": "default",
        "priority": "normal",
        "command": DEFAULT_COMMAND,
        "env": {},
        "max_retries": 0,
    }
    data.update(overrides)
    return SubmitSpec(**data)


def create_job(service, name, **overrides):
    return service.submit(make_spec(name, **overrides))


def set_submitted_at(kube, name, timestamp):
    kube.jobs[name]["metadata"]["annotations"]["vla-platform/submitted-at"] = timestamp


def seed_jobs(service, kube, names):
    for index, name in enumerate(names):
        create_job(service, name)
        set_submitted_at(kube, name, f"2026-01-01T00:0{index}:00Z")


def add_pod(kube, job_name, phase="Running", node_name=None, waiting_reason=None):
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


# --- POST /jobs -----------------------------------------------------------


def test_submit_returns_201_and_job_representation(api_client):
    response = api_client.post("/api/v1/jobs", json=payload("api-created"))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "api-created"
    assert body["status"] == "QUEUED"
    assert body["image"] == DEFAULT_IMAGE
    assert body["command"] == DEFAULT_COMMAND
    assert body["gpus"] == 1
    assert body["queue"] == "default"
    assert body["priority"] == "normal"
    assert body["max_retries"] == 0
    assert body["termination_grace_period_seconds"] == 30
    assert body["checkpoint_dir"] == "/checkpoints/api-created"
    assert body["workload_status"] == "Pending"
    assert body["created_at"].endswith("Z")


def test_submit_fills_optional_defaults(api_client):
    response = api_client.post("/api/v1/jobs", json={"name": "api-defaults", "gpus": 1})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["image"] == DEFAULT_IMAGE
    assert body["command"] == DEFAULT_COMMAND
    assert body["queue"] == "default"
    assert body["priority"] == "normal"
    assert body["max_retries"] == 0


def test_submit_accepts_explicit_null_image_and_command(api_client):
    response = api_client.post(
        "/api/v1/jobs",
        json={"name": "api-null-defaults", "gpus": 1, "image": None, "command": None},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["image"] == DEFAULT_IMAGE
    assert body["command"] == DEFAULT_COMMAND


def test_submit_accepts_command_at_size_limit(api_client):
    command = "x" * MAX_COMMAND_BYTES
    response = api_client.post("/api/v1/jobs", json=payload("api-limit", command=command))

    assert response.status_code == 201, response.text
    assert len(response.json()["command"]) == MAX_COMMAND_BYTES


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"name": "Api-Job"}, id="uppercase-name"),
        pytest.param({"name": "-api-job"}, id="leading-hyphen"),
        pytest.param({"name": "api_job"}, id="underscore-in-name"),
        pytest.param({"name": "a" * 51}, id="name-too-long"),
        pytest.param({"name": None}, id="null-name"),
        pytest.param({"gpus": None}, id="null-gpus"),
        pytest.param({"gpus": 0}, id="gpus-zero"),
        pytest.param({"gpus": -1}, id="gpus-negative"),
        pytest.param({"gpus": 2}, id="gpus-not-allowed"),
        pytest.param({"gpus": "one"}, id="gpus-not-an-int"),
        pytest.param({"priority": "urgent"}, id="unknown-priority"),
        pytest.param({"queue": "gpu-other"}, id="unknown-queue"),
        pytest.param({"max_retries": 3}, id="max-retries-too-high"),
        pytest.param({"max_retries": -1}, id="max-retries-negative"),
        pytest.param({"command": ""}, id="blank-command"),
        pytest.param({"command": " "}, id="whitespace-command"),
        pytest.param({"command": "x" * (MAX_COMMAND_BYTES + 1)}, id="command-too-long"),
        pytest.param({"unknown_field": True}, id="unknown-field"),
    ],
)
def test_submit_rejects_invalid_payload_with_422(api_client, overrides):
    response = api_client.post("/api/v1/jobs", json=payload(**overrides))

    assert response.status_code == 422, response.text
    assert "detail" in response.json()


def test_submit_duplicate_returns_409(api_client):
    assert api_client.post("/api/v1/jobs", json=payload("api-dup")).status_code == 201

    response = api_client.post("/api/v1/jobs", json=payload("api-dup"))

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "job api-dup already exists"


# --- GET /jobs ------------------------------------------------------------


def test_list_jobs_returns_pagination_envelope(api_client, service, kube):
    seed_jobs(service, kube, ("exp000", "exp001", "exp002"))

    response = api_client.get("/api/v1/jobs")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert [item["name"] for item in body["items"]] == ["exp002", "exp001", "exp000"]
    assert body["total"] == 3
    assert body["limit"] is None
    assert body["offset"] == 0


def test_list_jobs_limit_offset_slices_sorted_items(api_client, service, kube):
    seed_jobs(service, kube, ("exp000", "exp001", "exp002"))

    response = api_client.get("/api/v1/jobs", params={"limit": 2, "offset": 1})

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["name"] for item in body["items"]] == ["exp001", "exp000"]
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 1


def test_list_jobs_filters_by_status(api_client, service, kube):
    create_job(service, "api-waiting")
    create_job(service, "api-live")
    add_pod(kube, "api-live", phase="Running")
    create_job(service, "api-gone")
    add_pod(kube, "api-gone", phase="Running")
    service.cancel("api-gone")

    running = api_client.get("/api/v1/jobs", params={"status": "RUNNING"})
    cancelled = api_client.get("/api/v1/jobs", params={"status": "CANCELLED"})
    queued = api_client.get("/api/v1/jobs", params={"status": "QUEUED"})

    assert running.status_code == 200
    assert [item["name"] for item in running.json()["items"]] == ["api-live"]
    assert cancelled.status_code == 200
    assert [item["name"] for item in cancelled.json()["items"]] == ["api-gone"]
    assert cancelled.json()["items"][0]["status"] == "CANCELLED"
    assert queued.status_code == 200
    assert [item["name"] for item in queued.json()["items"]] == ["api-waiting"]


def test_list_jobs_q_is_case_insensitive(api_client, service, kube):
    create_job(service, "api-alpha")
    create_job(service, "api-alpine")
    create_job(service, "api-beta")

    response = api_client.get("/api/v1/jobs", params={"q": "AL"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert {item["name"] for item in body["items"]} == {"api-alpha", "api-alpine"}


def test_list_jobs_accepts_boundary_values(api_client):
    response = api_client.get(
        "/api/v1/jobs",
        params={"limit": 200, "offset": 0, "status": "RUNNING", "q": "x" * 100},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["limit"] == 200
    assert body["offset"] == 0
    assert body["total"] == 0
    assert body["items"] == []


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"limit": 0}, id="limit-zero"),
        pytest.param({"limit": 201}, id="limit-too-large"),
        pytest.param({"limit": "abc"}, id="limit-not-an-int"),
        pytest.param({"offset": -1}, id="offset-negative"),
        pytest.param({"offset": "abc"}, id="offset-not-an-int"),
        pytest.param({"status": "UNKNOWN"}, id="unknown-status"),
        pytest.param({"status": "running"}, id="lowercase-status"),
        pytest.param({"q": "x" * 101}, id="q-too-long"),
    ],
)
def test_list_jobs_rejects_invalid_query_parameters(api_client, params):
    response = api_client.get("/api/v1/jobs", params=params)

    assert response.status_code == 422, response.text
    assert "detail" in response.json()


# --- GET /jobs/{name} -----------------------------------------------------


def test_get_job_returns_representation(api_client, service):
    create_job(service, "api-single", command="python /app/train.py --steps 3")

    response = api_client.get("/api/v1/jobs/api-single")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "api-single"
    assert body["status"] == "QUEUED"
    assert body["command"] == "python /app/train.py --steps 3"
    assert body["checkpoint_dir"] == "/checkpoints/api-single"
    assert body["created_at"].endswith("Z")
    for key in (
        "started_at",
        "finished_at",
        "duration_seconds",
        "workload_status",
        "pod_status",
        "message",
    ):
        assert key in body


def test_get_job_unknown_returns_404(api_client):
    response = api_client.get("/api/v1/jobs/nope")

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "job nope not found"}


def test_get_job_rejects_invalid_name_with_422(api_client):
    response = api_client.get("/api/v1/jobs/Bad_Name")

    assert response.status_code == 422, response.text
    assert "detail" in response.json()


# --- DELETE /jobs/{name} --------------------------------------------------


def test_cancel_job_returns_cancelled(api_client, service, kube):
    create_job(service, "api-cancel")
    add_pod(kube, "api-cancel", phase="Running")

    response = api_client.delete("/api/v1/jobs/api-cancel")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "api-cancel"
    assert body["status"] == "CANCELLED"
    assert body["message"] == "Cancelled by user"
    assert "api-cancel" in kube.deleted

    follow_up = api_client.get("/api/v1/jobs/api-cancel")
    assert follow_up.status_code == 200
    assert follow_up.json()["status"] == "CANCELLED"


def test_cancel_job_is_idempotent(api_client, service):
    create_job(service, "api-cancel-twice")

    assert api_client.delete("/api/v1/jobs/api-cancel-twice").status_code == 200
    second = api_client.delete("/api/v1/jobs/api-cancel-twice")

    assert second.status_code == 200, second.text
    assert second.json()["status"] == "CANCELLED"


def test_cancel_unknown_job_returns_404(api_client):
    response = api_client.delete("/api/v1/jobs/nope")

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "job nope not found"}


# --- POST /jobs/{name}/clone ----------------------------------------------


def test_clone_job_creates_new_queued_job(api_client, service):
    create_job(service, "api-source", command="python /app/train.py --steps 7", env={"A": "1"})

    response = api_client.post("/api/v1/jobs/api-source/clone", json={"name": "api-copy"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "api-copy"
    assert body["status"] == "QUEUED"
    assert body["command"] == "python /app/train.py --steps 7"
    assert body["env"] == {"A": "1"}


def test_clone_to_existing_name_returns_409(api_client, service):
    create_job(service, "api-source")
    create_job(service, "api-taken")

    response = api_client.post("/api/v1/jobs/api-source/clone", json={"name": "api-taken"})

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "job api-taken already exists"


def test_clone_unknown_source_returns_404(api_client):
    response = api_client.post("/api/v1/jobs/nope/clone", json={"name": "api-copy"})

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "job nope not found"}


def test_clone_rejects_invalid_target_name_with_422(api_client, service):
    create_job(service, "api-source")

    response = api_client.post("/api/v1/jobs/api-source/clone", json={"name": "Bad_Name"})

    assert response.status_code == 422, response.text
    assert "detail" in response.json()


# --- GET /jobs/{name}/logs ------------------------------------------------


def test_logs_empty_when_job_has_no_pod(api_client, service):
    create_job(service, "api-logs-empty")

    response = api_client.get("/api/v1/jobs/api-logs-empty/logs")

    assert response.status_code == 200, response.text
    assert response.json() == {"name": "api-logs-empty", "pod_name": None, "logs": ""}


def test_logs_returns_pod_output(api_client, service, kube):
    create_job(service, "api-logs")
    pod = add_pod(kube, "api-logs", phase="Running")
    kube.logs[pod["metadata"]["name"]] = "step=1 loss=0.5\n"

    response = api_client.get("/api/v1/jobs/api-logs/logs")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "api-logs"
    assert body["pod_name"] == pod["metadata"]["name"]
    assert "loss=0.5" in body["logs"]


def test_logs_unknown_job_returns_404(api_client):
    response = api_client.get("/api/v1/jobs/nope/logs")

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "job nope not found"}


@pytest.mark.parametrize("tail_lines", [0, 10001])
def test_logs_rejects_out_of_range_tail_lines(api_client, service, tail_lines):
    create_job(service, "api-logs-range")

    response = api_client.get(
        "/api/v1/jobs/api-logs-range/logs", params={"tail_lines": tail_lines}
    )

    assert response.status_code == 422, response.text
    assert "detail" in response.json()


# --- GET /jobs/{name}/events ----------------------------------------------


def test_events_empty_when_job_has_no_events(api_client, service):
    create_job(service, "api-events-empty")

    response = api_client.get("/api/v1/jobs/api-events-empty/events")

    assert response.status_code == 200, response.text
    assert response.json() == {"name": "api-events-empty", "items": []}


def test_events_returns_normalised_items(api_client, service, kube):
    create_job(service, "api-events")
    pod = add_pod(kube, "api-events", phase="Pending", waiting_reason="ImagePullBackOff")
    kube.events = [
        {
            "metadata": {"uid": "e1", "creation_timestamp": "2026-01-01T00:00:01Z"},
            "involved_object": {"kind": "Pod", "name": pod["metadata"]["name"]},
            "type": "Warning",
            "reason": "Failed",
            "message": "Failed to pull image",
            "count": 3,
            "last_timestamp": "2026-01-01T00:00:09Z",
        }
    ]

    response = api_client.get("/api/v1/jobs/api-events/events")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "api-events"
    assert len(body["items"]) == 1
    assert body["items"][0] == {
        "type": "Warning",
        "reason": "Failed",
        "message": "Failed to pull image",
        "count": 3,
        "object_kind": "Pod",
        "object_name": pod["metadata"]["name"],
        "first_seen": None,
        "last_seen": "2026-01-01T00:00:09Z",
    }


def test_events_unknown_job_returns_404(api_client):
    response = api_client.get("/api/v1/jobs/nope/events")

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "job nope not found"}


# --- platform endpoints ---------------------------------------------------


def test_health_reports_status_and_auth_mode(api_client):
    response = api_client.get("/api/v1/health")

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "auth_required": False}


def test_config_returns_platform_limits(api_client):
    response = api_client.get("/api/v1/config")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed_gpus"] == [1]
    assert body["default_image"] == DEFAULT_IMAGE
    assert body["default_command"] == DEFAULT_COMMAND
    assert body["default_queue"] == "default"
    assert body["priorities"] == ["low", "normal", "high"]
    assert body["dataset_path"] == "/datasets"
    assert body["checkpoint_root"] == "/checkpoints"


def test_queues_returns_local_queue_summary(api_client, service, kube):
    kube.local_queue = {
        "metadata": {"name": "default"},
        "spec": {"clusterQueue": "vla-cluster-queue"},
        "status": {},
    }
    kube.cluster_queue = {
        "spec": {
            "resourceGroups": [
                {
                    "coveredResources": ["nvidia.com/gpu"],
                    "flavors": [
                        {"resources": [{"name": "nvidia.com/gpu", "nominalQuota": 4}]}
                    ],
                }
            ]
        }
    }
    create_job(service, "api-queued")

    response = api_client.get("/api/v1/queues")

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 1
    queue = body["items"][0]
    assert queue["name"] == "default"
    assert queue["cluster_queue"] == "vla-cluster-queue"
    assert queue["gpu_quota"] == 4
    assert queue["gpu_used"] == 0
    assert queue["pending"] == 0
    assert queue["admitted"] == 0
    assert [item["name"] for item in queue["items"]] == ["api-queued"]


def test_queues_returns_503_without_kueue_objects(api_client):
    response = api_client.get("/api/v1/queues")

    assert response.status_code == 503, response.text
    assert "LocalQueue" in response.json()["detail"]


def test_cluster_summary_reports_gpu_usage(api_client, service, kube):
    kube.nodes = [
        {
            "metadata": {"name": "node1"},
            "status": {
                "allocatable": {"nvidia.com/gpu": "2"},
                "conditions": [{"type": "Ready", "status": True}],
            },
        }
    ]
    create_job(service, "api-cluster")
    add_pod(kube, "api-cluster", phase="Running", node_name="node1")

    response = api_client.get("/api/v1/cluster/summary")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["gpu_total"] == 2
    assert body["gpu_allocated"] == 1
    assert body["gpu_available"] == 1
    assert body["node_count"] == 1
    assert body["nodes"] == [
        {"name": "node1", "ready": True, "gpu_total": 2, "gpu_allocated": 1}
    ]
    assert body["jobs"]["running"] == 1
    assert body["gpu_metrics"] == {"available": False, "items": []}
