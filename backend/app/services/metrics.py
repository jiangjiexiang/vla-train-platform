"""训练指标读取（loss 曲线）。

约定：训练脚本把每步指标写到 ``/checkpoints/<job>/metrics.jsonl``，每行一个 JSON 对象：

```json
{"step": 100, "loss": 0.0234, "lr": 0.0001, "epoch": 0.12}
```

平台经 Web 容器内挂载的 checkpoints_mount_path（默认 /checkpoints）读同一个文件，
因此不依赖训练容器里的进程是否还活着，任务结束后曲线仍然可查。
注意：宿主机路径（checkpoint_host_path）只用于构造训练 Job 的 hostPath，
容器里不存在那个前缀，读文件一律用挂载路径。

实现要点：
- 只读文件末尾 ``max_bytes`` 字节，避免大文件把内存吃满；
- 最后一行可能正在写入，解析失败时跳过而不是报错；
- 按 ``step`` 去重并升序返回，方便前端直接画折线。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
MAX_POINTS = 5000


def metrics_path(checkpoint_root: str | Path, job_name: str) -> Path:
    return Path(checkpoint_root) / job_name / "metrics.jsonl"


def read_metrics(
    checkpoint_root: str | Path,
    job_name: str,
    *,
    limit: int = 500,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """读取某个任务的指标。

    返回 ``{"name", "items", "total_points", "truncated", "path_exists"}``。
    文件不存在时返回空 items 而不是报错：老任务、刚开始的任务都没有这个文件。
    """
    limit = max(1, min(int(limit), MAX_POINTS))
    path = metrics_path(checkpoint_root, job_name)
    result: dict[str, Any] = {
        "name": job_name,
        "items": [],
        "total_points": 0,
        "truncated": False,
        "path_exists": path.is_file(),
    }
    if not path.is_file():
        return result

    lines = _tail_lines(path, max_bytes)
    points: dict[int, dict[str, Any]] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            # 正在写入的最后一行会被截断，跳过即可
            continue
        if not isinstance(record, dict):
            continue
        step = record.get("step")
        if not isinstance(step, (int, float)):
            continue
        # 同一个 step 以最后一次为准（训练脚本可能重跑某步）
        points[int(step)] = _normalize_point(record, int(step))

    ordered = [points[step] for step in sorted(points)]
    result["total_points"] = len(ordered)
    if len(ordered) > limit:
        ordered = ordered[-limit:]
        result["truncated"] = True
    result["items"] = ordered
    return result


def _normalize_point(record: dict[str, Any], step: int) -> dict[str, Any]:
    """只保留数值型字段，避免把任意结构透传给前端。"""
    point: dict[str, Any] = {"step": step}
    for key, value in record.items():
        if key == "step":
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            point[key] = float(value)
    if "timestamp" in record and isinstance(record["timestamp"], str):
        point["timestamp"] = record["timestamp"]
    return point


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    """读文件末尾若干字节，返回完整行（丢掉可能被截断的第一行）。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()  # 丢弃半行
            data = handle.read()
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def append_metrics(checkpoint_root: str | Path, job_name: str, records: list[dict[str, Any]]) -> int:
    """把指标追加到文件（供训练脚本的辅助工具与测试使用）。返回写入行数。"""
    path = metrics_path(checkpoint_root, job_name)
    os.makedirs(path.parent, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(records)