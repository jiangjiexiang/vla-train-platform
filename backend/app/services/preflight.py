"""提交前预检。

调研里最一致的一条实践（SageMaker、veMLP、PAI 都这么做）：**把错误挡在入队之前**。
让任务进队列、跑起来、再因为「数据集没 finalize」失败，会浪费一次 GPU 调度，
用户还得自己看日志才知道哪里错。

本模块在提交前做一次同步校验（目标 < 2 秒），每条不通过都给出**可修复的建议**，
而不是只说「不合法」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 显存估算的安全系数：4090 48G，但留一部分给 dataloader 与 CUDA context
VRAM_SAFETY_RATIO = 0.8


@dataclass
class Check:
    """一条预检结果。error 会阻止提交，warning 只提示。"""

    level: str  # error | warning | info
    code: str
    message: str
    suggestion: str

    def as_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "suggestion": self.suggestion,
        }


def preflight(
    *,
    dataset: dict[str, Any],
    recipe: dict[str, Any],
    gpu_total: int,
    gpu_allocated: int,
    requested_gpus: int,
) -> dict[str, Any]:
    """提交前检查。返回 ``{"ok": bool, "checks": [...]}``。

    ``ok`` 为 False 时调用方应拒绝提交（通常是 422）。
    这里只做**不依赖训练框架**的通用检查；框架特有的检查放在配方里。
    """
    checks: list[Check] = []
    checks.extend(_check_dataset(dataset))
    checks.extend(_check_compatibility(dataset, recipe))
    checks.extend(_check_resources(gpu_total, gpu_allocated, requested_gpus, recipe))

    errors = [check for check in checks if check.level == "error"]
    return {
        "ok": not errors,
        "checks": [check.as_dict() for check in checks],
        "summary": {
            "errors": len(errors),
            "warnings": sum(1 for check in checks if check.level == "warning"),
        },
    }


def _check_dataset(dataset: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    status = dataset.get("status")
    if status in ("unusable", "error"):
        errors = [
            issue for issue in dataset.get("issues", [])
            if issue.get("level") == "error"
        ]
        detail = "；".join(issue["message"] for issue in errors[:3]) or "数据集不可用"
        checks.append(
            Check(
                "error",
                "dataset_unusable",
                f"数据集 {dataset.get('id')} 不可用：{detail}",
                "按数据集页面的提示修复后重新扫描；常见原因是缺 meta/info.json、没有 data 文件或视频缺失",
            )
        )
    elif status == "warning":
        warnings = [
            issue["message"]
            for issue in dataset.get("issues", [])
            if issue.get("level") == "warning"
        ]
        checks.append(
            Check(
                "warning",
                "dataset_warning",
                f"数据集有 {len(warnings)} 条警告：{'；'.join(warnings[:3])}",
                "通常可以继续训练，但如果效果不好先检查这些警告项",
            )
        )

    episodes = int(dataset.get("episodes") or 0)
    frames = int(dataset.get("frames") or 0)
    if episodes and episodes < 10:
        checks.append(
            Check(
                "warning",
                "few_episodes",
                f"只有 {episodes} 个 episode，样本偏少",
                "少样本可以微调，但容易过拟合；建议至少几十个 episode，或降低训练步数并加密验证",
            )
        )
    if frames and frames < 1000:
        checks.append(
            Check(
                "warning",
                "few_frames",
                f"只有 {frames} 帧数据",
                "数据量过少，训练很可能不收敛。先补充采集再训练",
            )
        )

    if not dataset.get("cameras"):
        checks.append(
            Check(
                "error",
                "no_camera",
                "数据集没有图像，无法训练视觉-语言-动作模型",
                "检查采集时是否开启了相机录制；info.json 里应有 observation.images.* 特征",
            )
        )

    # 分位数统计：π0/π0.5 的 QUANTILES 归一化需要 q01/q99。
    # 扫描器已经把缺分位数记成 info 级 issue，这里只是把它提升成提交前的显式提示。
    codes = {issue.get("code") for issue in dataset.get("issues", [])}
    if "missing_quantiles" in codes:
        checks.append(
            Check(
                "info",
                "missing_quantiles",
                "数据集没有 q01/q99 分位数统计",
                "训练 π0/π0.5 前需要先跑 lerobot 的 augment_dataset_quantile_stats.py；"
                "SmolVLA 等不使用分位数归一化的模型不受影响",
            )
        )
    return checks


def _check_compatibility(dataset: dict[str, Any], recipe: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    dataset_format = str(dataset.get("format") or "")
    supported = list(recipe.get("dataset_formats") or [])
    if supported and dataset_format not in supported:
        checks.append(
            Check(
                "error",
                "format_mismatch",
                f"配方「{recipe.get('name')}」不支持数据集格式 {dataset_format}",
                f"可选配方：{', '.join(supported)}。若要用当前配方，需要先转换数据集格式",
            )
        )
    return checks


def _check_resources(
    gpu_total: int,
    gpu_allocated: int,
    requested_gpus: int,
    recipe: dict[str, Any],
) -> list[Check]:
    checks: list[Check] = []
    # 申请超过集群总数确实是错误（永远排不上）；但「当前全被占用」不是错误——
    # 那正是队列存在的意义，改成信息提示。
    if gpu_total and requested_gpus > gpu_total:
        checks.append(
            Check(
                "error",
                "gpu_exceeds_total",
                f"申请的 {requested_gpus} 卡超过集群总数 {gpu_total} 卡",
                "减小 GPU 数，或联系管理员扩容",
            )
        )
    elif gpu_total == 0:
        # 测试环境或集群尚未就绪：不拦截，避免误伤
        checks.append(
            Check(
                "info",
                "gpu_total_unknown",
                "未读取到集群 GPU 总数，跳过资源检查",
                "如果任务一直排队，检查集群 GPU 是否正常上报",
            )
        )

    estimated = recipe.get("estimated_vram_gb")
    if isinstance(estimated, (int, float)) and gpu_total:
        # 显存是每卡容量，这里无法直接知道单卡容量，用配方的估算值做提示
        checks.append(
            Check(
                "info",
                "vram_estimate",
                f"该配方预计占用约 {estimated} GB 显存",
                "如果运行时报 CUDA out of memory，先减小 batch size",
            )
        )

    free = max(gpu_total - gpu_allocated, 0)
    if free < requested_gpus:
        checks.append(
            Check(
                "info",
                "will_queue",
                f"当前空闲 {free} 卡，本次需要 {requested_gpus} 卡，任务会先排队",
                "这是正常情况，前面任务结束后会自动开始；可在任务页查看排队位置",
            )
        )
    return checks