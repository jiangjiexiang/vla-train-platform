"""训练产物（checkpoint）列举。

训练结束后用户需要知道「产出了什么、多大、什么时候写的」。
Web 以只读方式挂载了 checkpoint 目录，这里按需扫描，不做缓存——
checkpoint 目录通常只有几项，扫一次的开销远小于维护缓存的一致性问题。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 训练框架常见的权重后缀；其余文件当作「其他产物」列出但不标为权重
WEIGHT_SUFFIXES = (".pt", ".pth", ".safetensors", ".ckpt", ".bin", ".onnx")
# 单个任务最多列这么多项：多了也没人看，还会拖慢响应
MAX_ENTRIES = 200


def list_checkpoints(checkpoint_root: str | Path, job_name: str) -> dict[str, Any]:
    """列出某个任务的 checkpoint 文件。

    返回 ``{"exists", "path", "total_bytes", "items": [...]}``。
    目录不存在时返回 exists=False 而不是抛错——任务可能还在跑，或产物被外部清理。
    """
    job_dir = Path(checkpoint_root) / job_name
    if not job_dir.is_dir():
        return {"exists": False, "path": str(job_dir), "total_bytes": 0, "items": [], "truncated": False}

    items: list[dict[str, Any]] = []
    total_bytes = 0
    truncated = False
    # 只扫一层子目录（step_000500/ 这种），再深就和产物组织方式强绑定，容易失效
    for entry in sorted(job_dir.rglob("*")):
        if len(items) >= MAX_ENTRIES:
            truncated = True
            break
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        size = stat.st_size
        total_bytes += size
        relative = entry.relative_to(job_dir).as_posix()
        items.append(
            {
                "name": relative,
                "size_bytes": size,
                "modified_at": _iso(stat.st_mtime),
                # 指标文件不是模型权重，前端可以据此分组展示
                "is_weights": entry.suffix.lower() in WEIGHT_SUFFIXES,
            }
        )

    items.sort(key=lambda item: item["modified_at"] or "", reverse=True)
    return {
        "exists": True,
        "path": str(job_dir),
        "total_bytes": total_bytes,
        "items": items,
        "truncated": truncated,
    }


def _iso(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
