"""训练耗时预估。

用户填 5000 步时最想知道的是「这要跑多久」。没有预估的话，
只能凭感觉——而感觉往往差一个数量级。

预估依据按可信度排序：
1. 同镜像、同 batch 的历史任务实测速度（最准）
2. 稍微宽松的默认值（没有历史时用）

刻意给的是区间而不是单点：训练速度和 batch、数据加载、
GPU 是否被别的进程占用都有关，给一个"预计 11 分 20 秒"反而误导。
"""

from __future__ import annotations

from typing import Any

# 没有历史数据时的兜底速度（步/秒）。取实测 bs=8 的 2.9 再打七折，
# 宁可高估等待时间，也不要让用户以为很快就完。
DEFAULT_STEPS_PER_SECOND = 2.0


def _fmt(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    return f"{minutes / 60:.1f} 小时"


def estimate_duration(
    *,
    steps: int,
    batch_size: int | None,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """估算训练时长。

    ``history`` 是历史任务，需要 ``steps``、``batch_size``、``duration_seconds``
    三个字段。只用"batch 相同"的样本，因为不同 batch 的速度差很多，
    混在一起算平均没有意义。
    """
    usable = [
        item
        for item in history
        if item.get("duration_seconds")
        and item.get("steps")
        # 至少跑过 50 步才有参考价值：启动开销会严重拉低短任务的速度
        and int(item["steps"]) >= 50
        and (batch_size is None or item.get("batch_size") == batch_size)
    ]

    if usable:
        speed = sum(int(item["steps"]) / float(item["duration_seconds"]) for item in usable) / len(usable)
        source = "history"
        samples = len(usable)
    else:
        speed = DEFAULT_STEPS_PER_SECOND
        source = "default"
        samples = 0

    # 速度有个下限：再慢也不至于 0.1 步/秒，避免除出天文数字
    speed = max(speed, 0.1)
    expected = steps / speed

    return {
        "estimated_seconds": round(expected),
        "estimated_text": _fmt(expected),
        # 区间取 ±40%：训练速度受数据加载和 GPU 争用影响很大
        "range_text": f"{_fmt(expected * 0.7)} ~ {_fmt(expected * 1.4)}",
        "steps_per_second": round(speed, 2),
        "source": source,
        "samples": samples,
    }
