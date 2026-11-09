from __future__ import annotations

import json

from ..config import Settings
from ..models import SubmitSpec

APP_LABELS = {
    "app.kubernetes.io/name": "vla-train-platform",
    "app.kubernetes.io/managed-by": "vla-train-platform",
}
# 用户自定义标签写进注解而不是 Kubernetes label：自由文本不参与调度，
# 放到 label 上会受 63 字符/键值规则限制，也会影响选择器和 Kueue 计算。
USER_LABEL_ANNOTATION_PREFIX = "vla-platform/user-label-"
OWNER_ANNOTATION = "vla-platform/owner"


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
    if spec.owner:
        # 单独的 owner 注解：不解析整个 spec 也能按提交者筛选对象。
        annotations[OWNER_ANNOTATION] = spec.owner
    for key, value in sorted(spec.labels.items()):
        annotations[f"{USER_LABEL_ANNOTATION_PREFIX}{key}"] = value
    environment = [
        {"name": name, "value": value}
        for name, value in sorted(spec.env.items())
        if name != "CHECKPOINT_DIR"
    ]
    environment.append({"name": "CHECKPOINT_DIR", "value": f"/checkpoints/{spec.name}"})
    # 指标 hook 用 JOB_NAME 拼出 metrics.jsonl 的路径。此前没注入，hook 拿不到任务名
    # 就只能放弃写指标——这也是真实训练一直看不到 loss 曲线的原因之一。
    environment.append({"name": "JOB_NAME", "value": spec.name})

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
            # 任务结束后自动删除对象。不设的话已完成的 PyTorchJob 会永久留在
            # 集群里（实测堆积了 30 个），kubectl 输出越来越难读，也占 API 内存。
            # 产物在 /checkpoints 下独立保存，删对象不影响训练结果。
            "runPolicy": {
                "ttlSecondsAfterFinished": settings.job_ttl_seconds,
            },
            "pytorchReplicaSpecs": {
                "Master": {
                    "replicas": 1,
                    "restartPolicy": "Never" if spec.max_retries == 0 else "OnFailure",
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "runtimeClassName": "nvidia",
                            "terminationGracePeriodSeconds": spec.termination_grace_period_seconds,
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