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
    # 容器内路径一律取 checkpoints_mount_path：实战中用户会把产物挂到别的前缀，
    # 写死 "/checkpoints" 会让训练进程写到容器临时层里，任务一结束产物就没了。
    checkpoint_dir = f"{settings.checkpoints_mount_path.rstrip('/')}/{spec.name}"
    environment.append({"name": "CHECKPOINT_DIR", "value": checkpoint_dir})
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
                            # 训练容器和 Web 容器的 uid 不一样，而它们读写同一个
                            # checkpoint 目录：训练写、Web 读和清理。
                            #
                            # 训练镜像没有固定用户，按 Dockerfile 默认跑成 root，
                            # 写出来的 model.safetensors 是 root:root 600；Web 以
                            # 10001 运行，读不了——指标空、权重的下载按钮点下去 403。
                            # 反过来把目录属主改成 Web 的 uid 也不行：训练就写不进去。
                            #
                            # 与其在两边反复 chown（每换一次就有一边坏掉），
                            # 不如在容器起来之前把权限放开一次。initContainer 以
                            # root 跑，chmod 之后训练进程什么都写得了，Web 也读得到。
                            # 只作用于挂进来的那一个目录，不动宿主机上别的东西。
                            "initContainers": [
                                {
                                    "name": "init-permissions",
                                    "image": spec.image,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": [
                                        "sh",
                                        "-c",
                                        # 目录可能还不存在，-p 顺手建出来。
                                        # chmod 而不是 chown：不关心谁写的，
                                        # 只要求两边都能读写。
                                        "mkdir -p " + settings.checkpoints_mount_path
                                        + " && chmod -R a+rwX " + settings.checkpoints_mount_path,
                                    ],
                                    "volumeMounts": [
                                        {
                                            "name": "checkpoints",
                                            "mountPath": settings.checkpoints_mount_path,
                                        },
                                    ],
                                },
                            ],
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
                                        # 容器默认 /dev/shm 只有 64MB。DataLoader 用共享内存
                                        # 传 batch，num_workers 一多就 Bus error——
                                        # 实测 batch=8/workers=4 必定崩溃。
                                        {"name": "dshm", "mountPath": "/dev/shm"},
                                        {
                                            "name": "datasets",
                                            "mountPath": "/datasets",
                                            "readOnly": True,
                                        },
                                        {
                                            "name": "checkpoints",
                                            "mountPath": settings.checkpoints_mount_path,
                                            "readOnly": False,
                                        },
                                        # 训练代码从宿主机挂进来（可选）。
                                        #
                                        # 默认 compose 用镜像里的代码：镜像自包含，
                                        # 拿到别的机器上也能直接跑。
                                        # 但迭代时「改一行 train_vla.py → 重建 10GB 镜像
                                        # → 导入 k3s」要十几分钟，实在受不了。
                                        # settings.training_code_host_path 非空时改成挂载，
                                        # 改完只需重建 PyTorchJob。
                                        # 训练代码整目录挂载（可选）。settings 为空时
                                        # 用镜像里的代码，镜像自包含，拿到别的机器也能跑。
                                        # 迭代阶段把它指到 <项目>/images/smolvla-train，
                                        # 改 train_vla.py 就不必重建 10GB 镜像。
                                        *(
                                            [{"name": "training-code", "mountPath": "/app", "readOnly": True}]
                                            if settings.training_code_host_path
                                            else []
                                        ),
                                    ],
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "dshm",
                                    "emptyDir": {"medium": "Memory", "sizeLimit": settings.shm_size},
                                },
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
                                *(
                                    [
                                        {
                                            "name": "training-code",
                                            "hostPath": {
                                                "path": settings.training_code_host_path,
                                                "type": "Directory",
                                            },
                                        }
                                    ]
                                    if settings.training_code_host_path
                                    else []
                                ),
                            ],
                        },
                    },
                }
            },
        },
    }