"""LeRobot 数据集完整性、训练兼容性与数据文件问题检测。"""

from __future__ import annotations

import json
from pathlib import Path

from .lerobot import read_parquet_metadata
from .metadata import DatasetIssue, DatasetMeta

# LeRobot 必备的非图像特征；缺任何一个都不算可训练
REQUIRED_FEATURES = ("observation.state", "action")

ISSUE_LEVELS = ("error", "warning", "info")


def append_basic_issues(meta: DatasetMeta, root: Path) -> None:
    missing = [name for name in REQUIRED_FEATURES if getattr(meta, f"{name.split('.')[-1]}_dim", 0) <= 0]
    if missing:
        meta.issues.append(
            DatasetIssue("error", "missing_feature", f"缺少必需的字段：{', '.join(missing)}，模型无法训练")
        )
    if not meta.cameras:
        meta.issues.append(
            DatasetIssue("warning", "no_camera", "没有图像/视频特征（observation.images.*），视觉策略将无法训练")
        )
    if meta.episodes <= 0:
        meta.issues.append(DatasetIssue("error", "no_episode", "数据集没有任何 episode，请检查采集流程"))
    if meta.frames <= 0:
        meta.issues.append(DatasetIssue("error", "no_frame", "数据集没有任何帧"))
    elif meta.frames < 1000:
        meta.issues.append(
            DatasetIssue("warning", "tiny_dataset", f"只有 {meta.frames} 帧，样本过少，训练效果可能很差")
        )
    if meta.state_dim and meta.action_dim and meta.state_dim != meta.action_dim:
        meta.issues.append(
            DatasetIssue(
                "info",
                "dim_mismatch",
                f"state 维度 {meta.state_dim} 与 action 维度 {meta.action_dim} 不同，"
                "部分策略（如 SmolVLA）要求两者一致",
            )
        )
    data_dir = root / "data"
    if not data_dir.is_dir():
        meta.issues.append(DatasetIssue("error", "missing_data_dir", "缺少 data/ 目录，数据集不完整"))
    # 手工备份目录（meta.bak / data.bak 等）会让按 glob 统计的工具重复计数
    for leftover in ("meta.bak", "data.bak", "videos.bak"):
        if (root / leftover).exists():
            meta.issues.append(
                DatasetIssue(
                    "info",
                    "leftover_backup",
                    f"发现残留备份目录 {leftover}/，部分工具按 glob 统计时会重复计数，建议移出数据集目录",
                )
            )


def derive_status(meta: DatasetMeta) -> str:
    levels = {issue.level for issue in meta.issues}
    if "error" in levels:
        return "unusable"
    if "warning" in levels:
        return "warning"
    return "ready"


def append_deep_issues(meta: DatasetMeta, root: Path) -> None:
    _check_data_files(meta, root)
    _check_video_files(meta, root)
    _check_stats(meta, root)


def _check_data_files(meta: DatasetMeta, root: Path) -> None:
    data_dir = root / "data"
    if not data_dir.is_dir():
        return
    parquet_files = sorted(data_dir.glob("chunk-*/*.parquet"))[:3]
    if not parquet_files:
        meta.issues.append(
            DatasetIssue("error", "no_data_file", "data/ 下没有任何 parquet 文件，数据集不完整或采集尚未完成")
        )
        return
    parsed = read_parquet_metadata(parquet_files[0])
    if parsed is None:
        meta.issues.append(
            DatasetIssue("warning", "data_unreadable", f"无法解析 {parquet_files[0].name} 的元数据，文件可能损坏")
        )
        return
    columns = set(parsed.get("columns") or [])
    if columns:
        missing = set(REQUIRED_FEATURES) - columns
        if missing:
            meta.issues.append(
                DatasetIssue("error", "missing_column", f"数据文件缺少列：{', '.join(sorted(missing))}")
            )


def _check_video_files(meta: DatasetMeta, root: Path) -> None:
    videos_dir = root / "videos"
    if not meta.cameras:
        return
    if not videos_dir.is_dir():
        meta.issues.append(
            DatasetIssue("error", "missing_videos_dir", "info.json 声明了视频特征，但缺少 videos/ 目录")
        )
        return
    for camera in meta.cameras:
        camera_dir = videos_dir / camera
        if not camera_dir.is_dir():
            meta.issues.append(DatasetIssue("error", "missing_camera_video", f"相机 {camera} 没有视频目录"))
            continue
        mp4 = next(camera_dir.glob("chunk-*/*.mp4"), None)
        if mp4 is None:
            meta.issues.append(DatasetIssue("warning", "empty_camera_video", f"相机 {camera} 目录里没有视频文件"))
        elif mp4.stat().st_size == 0:
            meta.issues.append(DatasetIssue("warning", "zero_video", f"相机 {camera} 的视频文件为空"))


def _check_stats(meta: DatasetMeta, root: Path) -> None:
    stats_path = root / "meta" / "stats.json"
    if not stats_path.is_file():
        meta.issues.append(
            DatasetIssue(
                "info",
                "missing_stats",
                "缺少 meta/stats.json，训练时平台会自动计算归一化统计",
            )
        )
        return
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta.issues.append(
            DatasetIssue("warning", "invalid_stats", "meta/stats.json 不是合法 JSON，训练时会重新计算")
        )
        return
    if not isinstance(stats, dict) or "action" not in stats:
        meta.issues.append(
            DatasetIssue("info", "incomplete_stats", "stats.json 缺少 action 的归一化统计，训练时会补齐")
        )
        return
    # π0/π0.5 的 QUANTILES 归一化需要 q01/q99；缺失会在训练启动时直接抛 ValueError。
    # 这是「数据集 ↔ 模型兼容性」里最容易被忽略的阻塞点，提前标出来。
    action_stats = stats.get("action")
    if isinstance(action_stats, dict) and "q01" not in action_stats:
        meta.issues.append(
            DatasetIssue(
                "info",
                "missing_quantiles",
                "stats.json 没有 q01/q99 分位数统计，无法直接用于 π0.5 的 QUANTILES 归一化；"
                "需要先跑 lerobot 的 augment_dataset_quantile_stats.py",
            )
        )
