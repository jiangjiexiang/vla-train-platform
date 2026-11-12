"""LeRobot 数据集扫描编排与公开兼容入口。

平台的数据集就是 ``/datasets`` 下的子目录，每个子目录是一个 LeRobot 数据集。
扫描分两档：浅扫描只读 ``meta/info.json`` 和目录结构，供列表页使用；深扫描额外
检查 parquet、stats.json 与视频文件，在后台执行。具体格式解析、元数据规范化、
问题检测和目录遍历分别由同包模块负责。
"""

from __future__ import annotations

import json
from pathlib import Path

from .filesystem import directory_size as directory_size
from .filesystem import list_dataset_dirs as list_dataset_dirs
from .issues import ISSUE_LEVELS as ISSUE_LEVELS
from .issues import REQUIRED_FEATURES as REQUIRED_FEATURES
from .issues import append_basic_issues, append_deep_issues, derive_status
from .lerobot import STATIC_DIM_RATIO as STATIC_DIM_RATIO
from .lerobot import detect_format as detect_format
from .lerobot import read_parquet_metadata as read_parquet_metadata
from .lerobot import read_stats_summary as read_stats_summary
from .lerobot import read_task_strings as read_task_strings
from .metadata import DatasetIssue as DatasetIssue
from .metadata import DatasetMeta as DatasetMeta
from .metadata import normalize_metadata
from .metadata import utc_now as utc_now


def scan_shallow(root: Path, dataset_id: str | None = None) -> DatasetMeta:
    """只读 meta/info.json，目标 2 秒内完成。"""
    meta = DatasetMeta(
        id=dataset_id or root.name,
        name=root.name,
        path=str(root),
        scanned_at=utc_now(),
        scan_depth="shallow",
    )
    try:
        meta.dir_mtime = root.stat().st_mtime
    except OSError:
        meta.dir_mtime = 0.0

    info_path = root / "meta" / "info.json"
    if not info_path.is_file():
        meta.status = "unusable"
        meta.issues.append(
            DatasetIssue("error", "missing_info", "缺少 meta/info.json，不是有效的 LeRobot 数据集目录")
        )
        return meta

    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta.status = "unusable"
        meta.issues.append(DatasetIssue("error", "invalid_info", "meta/info.json 不是合法 JSON，文件可能损坏"))
        return meta

    meta.format = detect_format(root)
    normalize_metadata(meta, root, info)

    # 体积统计放最后，超时也不影响上面的元数据
    meta.size_bytes = directory_size(root)
    append_basic_issues(meta, root)
    meta.status = derive_status(meta)
    return meta


def scan_deep(root: Path, dataset_id: str | None = None) -> DatasetMeta:
    """在浅扫描基础上做更深的检查：parquet 列、视频文件、stats.json。"""
    meta = scan_shallow(root, dataset_id)
    meta.scan_depth = "deep"
    if meta.status == "unusable" and any(issue.code in ("missing_info", "invalid_info") for issue in meta.issues):
        return meta

    append_deep_issues(meta, root)
    meta.stats = read_stats_summary(root)
    meta.status = derive_status(meta)
    return meta


def scan_all(root: Path, deep: bool = False) -> list[DatasetMeta]:
    """扫描根目录下所有数据集。单个数据集异常不影响其他数据集。"""
    results: list[DatasetMeta] = []
    for directory in list_dataset_dirs(root):
        try:
            results.append(scan_deep(directory) if deep else scan_shallow(directory))
        except Exception as exc:  # noqa: BLE001 - 单个数据集的问题不能拖垮整次扫描
            broken = DatasetMeta(id=directory.name, name=directory.name, path=str(directory), scanned_at=utc_now())
            broken.status = "error"
            broken.issues.append(DatasetIssue("error", "scan_failed", f"扫描失败：{exc}"))
            results.append(broken)
    return results
