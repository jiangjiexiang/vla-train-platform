from __future__ import annotations

import json

from app.models import SubmitSpec
from app.templates.pytorchjob import render_pytorch_job


def make_spec(**overrides):
    payload = {
        "name": "exp001",
        "image": "vla-smoke:latest",
        "gpus": 1,
        "queue": "default",
        "priority": "normal",
        "command": "python /app/train.py",
        "env": {"A": "1"},
        "max_retries": 0,
    }
    payload.update(overrides)
    return SubmitSpec(**payload)


def render(spec, settings):
    return render_pytorch_job(spec, "2026-01-01T00:00:00Z", settings)


def pod_spec(body):
    return body["spec"]["pytorchReplicaSpecs"]["Master"]["template"]["spec"]


def test_default_grace_period_is_30(settings):
    assert pod_spec(render(make_spec(), settings))["terminationGracePeriodSeconds"] == 30


def test_custom_grace_period_is_rendered(settings):
    body = render(make_spec(termination_grace_period_seconds=5), settings)
    assert pod_spec(body)["terminationGracePeriodSeconds"] == 5


def test_labels_and_annotations_carry_spec(settings):
    body = render(make_spec(priority="high"), settings)
    labels = body["metadata"]["labels"]
    assert labels["kueue.x-k8s.io/queue-name"] == "default"
    assert labels["kueue.x-k8s.io/priority-class"] == "high"
    stored = json.loads(body["metadata"]["annotations"]["vla-platform/spec"])
    assert stored["name"] == "exp001"
    assert stored["termination_grace_period_seconds"] == 30


def test_checkpoint_dir_env_injected(settings):
    body = render(make_spec(env={"A": "1", "CHECKPOINT_DIR": "/wrong"}), settings)
    env = {item["name"]: item["value"] for item in pod_spec(body)["containers"][0]["env"]}
    assert env["CHECKPOINT_DIR"] == "/checkpoints/exp001"
    assert env["A"] == "1"


def test_dataset_mount_is_read_only(settings):
    mounts = {m["name"]: m for m in pod_spec(render(make_spec(), settings))["containers"][0]["volumeMounts"]}
    assert mounts["datasets"]["readOnly"] is True
    assert mounts["checkpoints"]["readOnly"] is False


def test_gpu_request_equals_limit(settings):
    resources = pod_spec(render(make_spec(gpus=1), settings))["containers"][0]["resources"]
    assert resources["requests"]["nvidia.com/gpu"] == resources["limits"]["nvidia.com/gpu"] == "1"


def test_restart_policy_follows_max_retries(settings):
    def master(body):
        return body["spec"]["pytorchReplicaSpecs"]["Master"]

    assert master(render(make_spec(max_retries=0), settings))["restartPolicy"] == "Never"
    assert master(render(make_spec(max_retries=2), settings))["restartPolicy"] == "OnFailure"
