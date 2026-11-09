from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings  # noqa: E402
from app.history import HistoryStore  # noqa: E402
from app.services.platform import PlatformService  # noqa: E402


class FakeKubernetes:
    """In-memory stand-in for KubernetesClient.

    Values are shaped like the real client's output: core objects come from
    ``to_dict()`` (snake_case keys), custom objects come straight from JSON.
    """

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.workloads: list[dict] = []
        self.pods: list[dict] = []
        self.nodes: list[dict] = []
        self.events: list[dict] = []
        self.logs: dict[str, str] = {}
        self.local_queue: dict | None = None
        self.cluster_queue: dict | None = None
        self.deleted: list[str] = []

    # --- PyTorchJob / Workload -------------------------------------------
    def get_job(self, name):
        return self.jobs.get(name)

    def list_jobs(self):
        return list(self.jobs.values())

    def create_job(self, body):
        name = body["metadata"]["name"]
        if name in self.jobs:
            from app.services.errors import ServiceError

            raise ServiceError(409, "already exists")
        job = {
            "metadata": {
                "name": name,
                "uid": f"uid-{name}",
                "labels": body["metadata"].get("labels", {}),
                "annotations": body["metadata"].get("annotations", {}),
                "creationTimestamp": "2026-01-01T00:00:00Z",
            },
            "spec": body.get("spec", {}),
            "status": {},
        }
        self.jobs[name] = job
        return job

    def delete_job(self, name):
        self.jobs.pop(name, None)
        self.deleted.append(name)

    def list_workloads(self):
        return list(self.workloads)

    def list_job_pods(self, name):
        return [pod for pod in self.pods if pod["metadata"].get("labels", {}).get("vla-platform/job-name") == name]

    # --- core -------------------------------------------------------------
    def read_pod_log(self, pod_name, tail_lines):
        return self.logs.get(pod_name, "")

    def list_nodes(self):
        return list(self.nodes)

    def list_training_pods(self):
        return list(self.pods)

    def list_events(self, object_names):
        names = set(object_names)
        return [e for e in self.events if e.get("involved_object", {}).get("name") in names]

    def get_local_queue(self):
        return self.local_queue

    def get_cluster_queue(self, name):
        return self.cluster_queue


@pytest.fixture
def settings(tmp_path):
    return Settings(
        api_token="test-token",
        auth_required=False,
        db_path=str(tmp_path / "platform.db"),
        gpu_metrics_url="",
    )


@pytest.fixture
def history(settings):
    return HistoryStore(settings.db_path)


@pytest.fixture
def kube():
    return FakeKubernetes()


@pytest.fixture
def service(settings, history, kube):
    return PlatformService(settings, history, kube)
