from __future__ import annotations

import json

from ..config import Settings
from ..models import SubmitSpec


APP_LABELS = {
    "app.kubernetes.io/name": "vla-train-platform",
    "app.kubernetes.io/managed-by": "vla-train-platform",
}


def render_pytorch_job(spec: SubmitSpec, submitted_at: str, settings: Settings) -> dict:
    labels = {
        **APP_LABELS,
        "vla-platform/job-name": spec.name,
        "kueue.x-k8s.io/queue-name": spec.queue,
        "kueue.x-k8s.io/priority-class": spec.priority,
    }
    annotations = {
        "vla-platform/submitted-at": submitted_at,
        "vla-platform/spec": json.dumps(
            spec.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
        ),
    }
    environment = [
        {"name": name, "value": value}
        for name, value in sorted(spec.env.items())
        if name != "CHECKPOINT_DIR"
    ]
    environment.append({"name": "CHECKPOINT_DIR", "value": f"/checkpoints/{spec.name}"})

    return {
        "apiVersion": "kubeflow.org/v1",
        "kind": "PyTorchJob",
        "metadata": {
            "name": spec.name,
            "namespace": settings.namespace,
            "labels": labels,
            "annotations": annotations,
        },
        "spec": {
            "backoffLimit": spec.max_retries,
            "pytorchReplicaSpecs": {
                "Master": {
                    "replicas": 1,
                    "restartPolicy": "Never" if spec.max_retries == 0 else "OnFailure",
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "runtimeClassName": "nvidia",
                            "containers": [
                                {
                                    "name": "pytorch",
                                    "image": spec.image,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": ["/bin/bash", "-lc", spec.command],
                                    "env": environment,
                                    "resources": {
                                        "requests": {"nvidia.com/gpu": str(spec.gpus)},
                                        "limits": {"nvidia.com/gpu": str(spec.gpus)},
                                    },
                                    "volumeMounts": [
                                        {
                                            "name": "datasets",
                                            "mountPath": "/datasets",
                                            "readOnly": True,
                                        },
                                        {
                                            "name": "checkpoints",
                                            "mountPath": "/checkpoints",
                                            "readOnly": False,
                                        },
                                    ],
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "datasets",
                                    "hostPath": {
                                        "path": settings.dataset_host_path,
                                        "type": "Directory",
                                    },
                                },
                                {
                                    "name": "checkpoints",
                                    "hostPath": {
                                        "path": settings.checkpoint_host_path,
                                        "type": "Directory",
                                    },
                                },
                            ],
                        },
                    },
                }
            },
        },
    }
