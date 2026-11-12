"""数据集目录遍历与体积统计。"""

from __future__ import annotations

import os
import time
from pathlib import Path


def directory_size(path: Path, max_seconds: float = 5.0) -> int:
    """统计目录大小。超过 max_seconds 就提前返回已统计部分，避免大目录拖死请求。"""
    total = 0
    deadline = time.monotonic() + max_seconds
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue
        if time.monotonic() > deadline:
            break
    return total


def list_dataset_dirs(root: Path) -> list[Path]:
    """列出 /datasets 下的数据集目录，跳过隐藏目录与临时目录。"""
    if not root.is_dir():
        return []
    result = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name.endswith((".tmp", ".partial")):
            continue
        result.append(entry)
    return result
