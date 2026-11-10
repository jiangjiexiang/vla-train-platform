"""失败诊断。

企业级平台与「能跑起来」的脚本的差别，很大一部分在于**失败时说人话**。
用户看到 `Error: CUDA out of memory` 或者 `ImagePullBackOff` 时，需要知道
「为什么」和「怎么办」，而不是只有一坨日志。

本模块把 Pod 事件、容器退出码、状态消息归一到一组稳定的诊断码上，
每条诊断带三条信息：
  - ``code``：稳定的机器码，前端可用它选图标、做过滤
  - ``summary``：一句话说清发生了什么
  - ``suggestion``：具体该怎么做（可执行的动作，不是「请检查配置」）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# 诊断码 → (摘要模板, 建议)
# 顺序即优先级：同一批信号里命中更靠前的那个更可能是根因。
DIAGNOSES: dict[str, tuple[str, str]] = {
    "image_pull": (
        "镜像拉取失败",
        "确认镜像已导入 K3s：`buildah push ... docker-archive:` 后执行 `k3s ctr -n k8s.io images import`；"
        "镜像名要写全（vla-smoke:latest），不带 registry 前缀时 K8s 按 docker.io/library/ 查找",
    ),
    "image_pull_backoff": (
        "镜像拉取失败",
        "镜像 {image} 在节点上不存在：buildah 构建出的镜像只在 buildah 的存储里，"
        "K3s/containerd 看不到，构建成功不等于能跑。导入后重试："
        "`buildah push {image} docker-archive:/tmp/buildah-export.tar`，"
        "`k3s ctr -n k8s.io images import /tmp/buildah-export.tar`，"
        "`k3s ctr -n k8s.io images ls | grep {image}` 确认已存在。"
        "镜像名要带完整标签（:latest）；不带标签时 kubelet 按 docker.io/library/ 去公网拉，"
        "被防火墙挡住就是 403",
    ),
    "oom_gpu": (
        "GPU 显存不足",
        "减小 batch size 或开启梯度累积；也可尝试减小输入分辨率、冻结视觉编码器，"
        "或换用 LoRA 等参数高效微调方式",
    ),
    "oom_container": (
        "容器内存超限被杀",
        "训练进程占用超过 Pod 的 memory limit。调小 batch/num_workers，或联系管理员提高限额",
    ),
    "dataset_missing": (
        "数据集路径不存在",
        "容器内 /datasets/<id> 不存在。确认数据集已放到宿主机上的数据集根目录"
        "（deploy 里的 data/datasets，不是容器内路径）下，"
        "并在数据集页重新扫描",
    ),
    "dataset_empty": (
        "数据集目录为空或不可读",
        "检查宿主机上该目录的权限与内容；bind mount 重启后会失效，需要重新挂载",
    ),
    "checkpoint_write": (
        "checkpoint 写入失败",
        "/checkpoints 不可写。确认宿主机 /data/vla-platform/checkpoints 对容器用户可写",
    ),
    "python_error": (
        "训练脚本抛异常",
        "查看日志末尾的 traceback 定位出错行；常见原因是不兼容的参数名、缺依赖、权重路径不对",
    ),
    "module_not_found": (
        "缺少 Python 依赖",
        "镜像里没有安装该模块。检查镜像的 requirements，注意镜像版本是否与被训练代码匹配",
    ),
    "permission_denied": (
        "权限不足",
        "容器内用户无权访问该路径或文件。检查宿主机目录属主（非 root 运行的镜像需要用 UID 而非 root 授权）",
    ),
    "evicted": (
        "Pod 被驱逐",
        "节点资源不足（磁盘或内存）。清理磁盘，或减小任务规模后重试",
    ),
    "config_error": (
        "参数配置错误",
        "数据集与配方/模型的参数不匹配。检查数据集格式版本、动作维度、相机数量是否被训练脚本接受",
    ),
    "hf_auth": (
        "缺少 HuggingFace 凭据",
        "训练脚本尝试访问 HuggingFace Hub 但没有 token，最常见的是训练**结束后**自动推送模型。"
        "平台上提交任务不需要推送，检查配方是否带 --push_to_hub true，或镜像是否已默认关闭。",
    ),
    "hf_download": (
        "模型权重下载失败",
        "下载预训练权重时网络出错或被限速。内网环境建议把权重预置进镜像，"
        "或确认 HF_ENDPOINT 指向可用的镜像站。",
    ),
    "output_dir_exists": (
        "输出目录已存在",
        "训练框架要求输出目录不存在（否则拒绝覆盖），重试同一个任务时容易遇到。"
        "清理该任务产物后重试，或换一个任务名。",
    ),
    "missing_required_arg": (
        "训练参数缺失",
        "训练框架报缺少必填参数。通常意味着配方里的参数名与框架版本不匹配，"
        "对照 docs 下的参数调研修正配方。",
    ),
    "shm_exhausted": (
        "共享内存不足",
        "DataLoader 用 /dev/shm 在进程间传 batch，容器默认只有 64MB，"
        "worker 一多就会触发 Bus error。平台默认已给到 4Gi；"
        "若仍出现，把 PLATFORM_SHM_SIZE 调大，或降低 num_workers。",
    ),
    "torchcodec": (
        "视频解码后端不可用",
        "torchcodec 在当前 torch 版本上加载失败。加 --dataset.video_backend=pyav 可绕过；"
        "镜像若已注入 torchcodec guard 则不会出现。",
    ),
    "nccl": (
        "分布式通信失败",
        "多卡/多机 NCCL 初始化失败。检查各节点 NCCL 与 torch 版本是否一致、GPU 是否都可访问",
    ),
}


@dataclass
class Diagnosis:
    code: str
    summary: str
    suggestion: str
    # 命中的原始信号，便于排查时对照日志
    evidence: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "summary": self.summary,
            "suggestion": self.suggestion,
            "evidence": self.evidence,
        }


# 日志/事件文本 → 诊断码的匹配规则。按顺序匹配，先命中先用。
LOG_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 下面几条来自真实跑 SmolVLA 训练时遇到的失败，不是推测的
    ("hf_auth", re.compile(r"401 Client Error|Unauthorized for url.*huggingface|Invalid username or password", re.I)),
    (
        # Bus error / SIGBUS 在 DataLoader 里几乎只有一种原因：共享内存不够。
        # 必须排在 python_error 前面，否则会被吞成"训练脚本抛异常"，
        # 而真正的建议（调 shm 或减 worker）完全给不出来。
        "shm_exhausted",
        re.compile(
            r"Bus error|signal: Bus|insufficient shared memory|out of shared memory",
            re.I,
        ),
    ),
    ("torchcodec", re.compile(r"Could not load libtorchcodec", re.I)),
    ("output_dir_exists", re.compile(r"already exists and resume is False", re.I)),
    ("missing_required_arg", re.compile(r"Missing required field", re.I)),
    ("hf_download", re.compile(r"huggingface_hub.*(?:Connection|Timeout)|Couldn't reach|Max retries exceeded", re.I)),
    # K8s 的事件消息通常是 "Failed to pull image ..."，没有 ErrImagePull 字样，也要认出来
    ("image_pull_backoff", re.compile(r"ImagePullBackOff|ErrImagePull|Failed to pull image|manifest unknown", re.I)),
    ("oom_gpu", re.compile(r"CUDA out of memory|CUDA OutOfMemoryError|torch\.cuda\.OutOfMemoryError", re.I)),
    ("oom_container", re.compile(r"OOMKilled", re.I)),
    ("module_not_found", re.compile(r"ModuleNotFoundError|No module named", re.I)),
    (
        "dataset_missing",
        re.compile(
            r"No such file or directory[^\n]*/datasets|Dataset.*not found|dataset_root.*does not exist",
            re.I,
        ),
    ),
    ("checkpoint_write", re.compile(r"Read-only file system|Permission denied[^\n]*/checkpoints", re.I)),
    ("permission_denied", re.compile(r"PermissionError|Permission denied", re.I)),
    ("nccl", re.compile(r"NCCL.*(error|fail)|unhandled cuda error|Connection refused.*node", re.I)),
    ("config_error", re.compile(r"ValueError:[^\n]*(dimension|shape|mismatch)|KeyError: '[a-z_.]+'", re.I)),
    ("python_error", re.compile(r"Traceback \(most recent call last\)", re.I)),
]


# 容器处于 waiting 态时，reason 本身就是根因，不需要再猜日志。
# 实测事故：镜像只在 buildah 里有、没导入 k3s，Pod 卡在 ImagePullBackOff，
# 平台却把「读日志 400」显示成 K8s 报错，用户完全看不出是镜像的问题。
WAITING_REASON_CODES: list[tuple[str, re.Pattern[str]]] = [
    (
        "image_pull_backoff",
        re.compile(
            r"ImagePullBackOff|ErrImagePull|InvalidImageName|ErrImageNeverPull", re.I
        ),
    ),
    # CrashLoopBackOff 只说明「起来就挂、K8s 在退避重试」，原因得看日志。
    # 借用 oom_container 会被说成「内存超限」，所以复用最贴近的现有码，
    # 再用 WAITING_SUMMARIES / WAITING_SUGGESTIONS 覆盖文案。
    ("python_error", re.compile(r"CrashLoopBackOff", re.I)),
    (
        "config_error",
        re.compile(r"CreateContainerConfigError|CreateContainerError", re.I),
    ),
    ("evicted", re.compile(r"Evicted", re.I)),
]

# 同一诊断码在不同 waiting reason 下要说不同的话：CrashLoopBackOff 与
# 「脚本抛异常」都是代码问题，但前者的处理动作是「先取消任务、别让它继续占 GPU」。
WAITING_SUMMARIES: dict[str, str] = {
    "python_error": "容器反复启动失败退出（CrashLoopBackOff）",
    "config_error": "容器配置错误，无法创建",
}

WAITING_SUGGESTIONS: dict[str, str] = {
    "python_error": (
        "容器启动后立刻退出，Kubernetes 正在退避重试，任务会一直占着 GPU 配额。"
        "先取消任务，再看「事件」里的退出原因/退出码，或日志末尾的 traceback"
    ),
    "config_error": "容器引用的 ConfigMap / Secret / 挂载在集群里不存在或字段缺失。联系管理员核对任务模板",
}


def diagnose(
    *,
    status: str,
    reason: str | None = None,
    message: str | None = None,
    log_tail: str | None = None,
    events: list[dict[str, Any]] | None = None,
    waiting_reason: str | None = None,
    image: str | None = None,
) -> dict[str, Any] | None:
    """根据任务状态、退出原因、日志尾部与事件推断诊断。

    ``waiting_reason`` 是 Pod 的 ``containerStatuses[].state.waiting.reason``
    （ImagePullBackOff / CrashLoopBackOff / ...）。容器没起来时根本没有日志
    可读，这条是**唯一**的可靠信号，所以单独传进来、优先级最高。

    ``image`` 用于把具体镜像名写进建议里——「镜像拉取失败」不点名等于没说。

    返回 ``None`` 表示没有可诊断的失败信号（例如任务成功或还在排队）。
    """
    if status not in ("FAILED", "UNKNOWN"):
        return None

    # 0) 容器 waiting：镜像拉不到时 Pod 永远不会有日志。
    if waiting_reason:
        for code, pattern in WAITING_REASON_CODES:
            if pattern.search(waiting_reason):
                evidence = waiting_reason.strip()[:200]
                if message and message.strip() and message.strip() not in evidence:
                    evidence = f"{evidence} | {message.strip()[:160]}"
                return _build(code, evidence, image=image, waiting=True)

    # 1) 事件里的明确信号（K8s 层面的问题优先）
    for event in events or []:
        text = f"{event.get('reason') or ''} {event.get('message') or ''}"
        for code, pattern in LOG_PATTERNS:
            if pattern.search(text):
                return _build(code, text.strip()[:200], image=image)

    # 2) 状态里的 reason / message
    for field_value in (reason, message):
        if not field_value:
            continue
        for code, pattern in LOG_PATTERNS:
            if pattern.search(field_value):
                return _build(code, field_value.strip()[:200], image=image)

    # 3) 日志尾部（最不可靠但信息最全，放最后）
    if log_tail:
        for code, pattern in LOG_PATTERNS:
            match = pattern.search(log_tail)
            if match:
                start = max(0, match.start() - 120)
                return _build(
                    code, log_tail[start : match.end() + 120].strip(), image=image
                )

    # 4) 兜底：知道失败了但说不出原因
    return _build(
        "python_error",
        message or reason or "任务以非零状态退出，日志中没有匹配到已知错误模式",
        image=image,
    )


def _build(
    code: str, evidence: str, image: str | None = None, waiting: bool = False
) -> dict[str, Any]:
    summary, suggestion = DIAGNOSES.get(code, DIAGNOSES["python_error"])
    if waiting:
        summary = WAITING_SUMMARIES.get(code, summary)
        suggestion = WAITING_SUGGESTIONS.get(code, suggestion)
    if "{image}" in suggestion:
        image = (image or "").strip() or "<镜像名>"
        tag = re.split(r"[/:]", image)[-1] or "image"
        suggestion = suggestion.format(image=image, tag=tag)
    return Diagnosis(code=code, summary=summary, suggestion=suggestion, evidence=evidence).as_dict()
