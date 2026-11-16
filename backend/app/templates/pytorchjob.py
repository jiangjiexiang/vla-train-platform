from __future__ import annotations

import json

from ..config import Settings
from ..models import SubmitSpec

APP_LABELS = {
    "app.kubernetes.io/name": "vla-train-platform",
    "app.kubernetes.io/managed-by": "vla-train-platform",
}
# 自由文本不参与调度，写进注解以避免 Kubernetes label 的字符限制和选择器副作用。
USER_LABEL_ANNOTATION_PREFIX = "vla-platform/user-label-"
OWNER_ANNOTATION = "vla-platform/owner"


def _labels(spec: SubmitSpec) -> dict:
    return {
        **APP_LABELS,
        "vla-platform/job-name": spec.name,
        "kueue.x-k8s.io/queue-name": spec.queue,
        "kueue.x-k8s.io/priority-class": spec.priority,
    }


def _annotations(spec: SubmitSpec, submitted_at: str) -> dict:
    annotations = {
        "vla-platform/submitted-at": submitted_at,
        "vla-platform/spec": json.dumps(
            spec.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
        ),
    }
    if spec.owner:
        annotations[OWNER_ANNOTATION] = spec.owner
    for key, value in sorted(spec.labels.items()):
        annotations[f"{USER_LABEL_ANNOTATION_PREFIX}{key}"] = value
    return annotations


def _environment(spec: SubmitSpec, settings: Settings) -> list[dict]:
    environment = [
        {"name": name, "value": value}
        for name, value in sorted(spec.env.items())
        if name != "CHECKPOINT_DIR"
    ]
    checkpoint_dir = f"{settings.checkpoints_mount_path.rstrip('/')}/{spec.name}"
    environment.extend(
        [
            {"name": "CHECKPOINT_DIR", "value": checkpoint_dir},
            {"name": "JOB_NAME", "value": spec.name},
        ]
    )
    return environment


def _volume_mounts(spec: SubmitSpec, settings: Settings) -> list[dict]:
    mounts = [
        {"name": "dshm", "mountPath": "/dev/shm"},
        {"name": "datasets", "mountPath": "/datasets", "readOnly": True},
        {
            "name": "checkpoints",
            "mountPath": settings.checkpoints_mount_path,
            "readOnly": False,
        },
    ]
    if settings.training_code_host_path:
        mounts.append({"name": "training-code", "mountPath": "/app", "readOnly": True})
    mounts.extend(
        {
            "name": item.name,
            "mountPath": item.mount_path,
            "readOnly": item.read_only,
        }
        for item in spec.host_paths
    )
    return mounts


def _volumes(spec: SubmitSpec, settings: Settings) -> list[dict]:
    volumes = [
        {
            "name": "dshm",
            "emptyDir": {"medium": "Memory", "sizeLimit": settings.shm_size},
        },
        {
            "name": "datasets",
            "hostPath": {"path": settings.dataset_host_path, "type": "Directory"},
        },
        {
            "name": "checkpoints",
            "hostPath": {"path": settings.checkpoint_host_path, "type": "Directory"},
        },
    ]
    if settings.training_code_host_path:
        volumes.append(
            {
                "name": "training-code",
                "hostPath": {"path": settings.training_code_host_path, "type": "Directory"},
            }
        )
    volumes.extend(
        {
            "name": item.name,
            "hostPath": {"path": item.host_path, "type": "Directory"},
        }
        for item in spec.host_paths
    )
    return volumes


def _training_pod_spec(spec: SubmitSpec, settings: Settings) -> dict:
    return {
        "runtimeClassName": "nvidia",
        "terminationGracePeriodSeconds": spec.termination_grace_period_seconds,
        # 训练容器与 Web 容器读写同一个 checkpoint 目录，但 uid 不同。
        # initContainer 只放开挂载目录的权限，不修改宿主机其它路径。
        "initContainers": [
            {
                "name": "init-permissions",
                "image": spec.image,
                "imagePullPolicy": "IfNotPresent",
                "command": [
                    "sh",
                    "-c",
                    "mkdir -p " + settings.checkpoints_mount_path
                    + " && chmod -R a+rwX " + settings.checkpoints_mount_path,
                ],
                "volumeMounts": [
                    {
                        "name": "checkpoints",
                        "mountPath": settings.checkpoints_mount_path,
                    }
                ],
            }
        ],
        "containers": [
            {
                "name": "pytorch",
                "image": spec.image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/bash", "-lc", spec.command],
                "env": _environment(spec, settings),
                "resources": {
                    "requests": {"nvidia.com/gpu": str(spec.gpus)},
                    "limits": {"nvidia.com/gpu": str(spec.gpus)},
                },
                "volumeMounts": _volume_mounts(spec, settings),
            }
        ],
        "volumes": _volumes(spec, settings),
    }


def render_pytorch_job(spec: SubmitSpec, submitted_at: str, settings: Settings) -> dict:
    labels = _labels(spec)
    return {
        "apiVersion": "kubeflow.org/v1",
        "kind": "PyTorchJob",
        "metadata": {
            "name": spec.name,
            "namespace": settings.namespace,
            "labels": labels,
            "annotations": _annotations(spec, submitted_at),
        },
        "spec": {
            "backoffLimit": spec.max_retries,
            "runPolicy": {"ttlSecondsAfterFinished": settings.job_ttl_seconds},
            "pytorchReplicaSpecs": {
                "Master": {
                    "replicas": 1,
                    "restartPolicy": "Never" if spec.max_retries == 0 else "OnFailure",
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": _training_pod_spec(spec, settings),
                    },
                }
            },
        },
    }
