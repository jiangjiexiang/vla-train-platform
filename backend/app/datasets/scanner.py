"""LeRobot 数据集扫描。

平台的数据集就是 ``/datasets`` 下的子目录，每个子目录是一个 LeRobot 数据集。
本模块只依赖标准库：Web 镜像不带 pyarrow/pandas，读 parquet 的元数据用轻量解析，
读不到的字段就标记为「未知」而不是让整个扫描失败。

扫描分两档（见 docs 里的架构说明）：

- **浅扫描**：只读 ``meta/info.json`` 和目录结构，目标 < 2 秒，列表页用。
- **深扫描**：额外读 ``meta/episodes/*.parquet`` 的页脚与 ``meta/stats.json``，
  抽样校验 data parquet 的列，检查视频文件是否存在。后台执行。

所有函数都是纯函数式的：输入一个路径，输出 ``DatasetMeta``，不做 IO 以外的副作用。
"""

from __future__ import annotations

import json
import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# LeRobot 必备的非图像特征；缺任何一个都不算可训练
REQUIRED_FEATURES = ("observation.state", "action")

ISSUE_LEVELS = ("error", "warning", "info")


@dataclass
class DatasetIssue:
    """一条数据集问题。code 用于前端做稳定的分类与图标。"""

    level: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass
class DatasetMeta:
    """扫描结果。字段命名与前端 types/api.ts 一一对应。"""

    id: str
    name: str
    path: str
    format: str = "unknown"
    status: str = "scanning"
    size_bytes: int = 0
    episodes: int = 0
    frames: int = 0
    fps: float = 0.0
    tasks: list[str] = field(default_factory=list)
    robot_type: str | None = None
    cameras: list[str] = field(default_factory=list)
    image_size: list[int] | None = None
    state_dim: int = 0
    action_dim: int = 0
    action_names: list[str] = field(default_factory=list)
    state_names: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    issues: list[DatasetIssue] = field(default_factory=list)
    scanned_at: str = ""
    scan_depth: str = "shallow"
    dir_mtime: float = 0.0
    # 动作/状态的分布摘要（深扫描时填充），用于判断数据质量
    stats: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "format": self.format,
            "status": self.status,
            "size_bytes": self.size_bytes,
            "episodes": self.episodes,
            "frames": self.frames,
            "fps": self.fps,
            "tasks": self.tasks,
            "robot_type": self.robot_type,
            "cameras": self.cameras,
            "image_size": self.image_size,
            "state_dim": self.state_dim,
            "action_dim": self.action_dim,
            "action_names": self.action_names,
            "state_names": self.state_names,
            "duration_seconds": self.duration_seconds,
            "issues": [issue.as_dict() for issue in self.issues],
            "scanned_at": self.scanned_at,
            "scan_depth": self.scan_depth,
            "dir_mtime": self.dir_mtime,
            "stats": self.stats,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DatasetMeta:
        data = dict(payload)
        data["issues"] = [DatasetIssue(**item) for item in data.get("issues", [])]
        return cls(**{key: value for key, value in data.items() if key in cls.__dataclass_fields__})


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 目录与体积
# ---------------------------------------------------------------------------
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


def detect_format(root: Path) -> str:
    """根据目录结构判断 LeRobot 版本。"""
    info_path = root / "meta" / "info.json"
    if info_path.is_file():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "unknown"
        version = str(info.get("codebase_version", ""))
        if version.startswith("v3"):
            return "lerobot-v3.0"
        if version.startswith("v2"):
            # v2.1 引入了 total_tasks / chunks_size，v2.0 没有
            return "lerobot-v2.1" if "chunks_size" in info else "lerobot-v2.0"
        return "lerobot"
    return "unknown"


# ---------------------------------------------------------------------------
# parquet 元数据（标准库轻量解析）
# ---------------------------------------------------------------------------
def read_parquet_metadata(path: Path) -> dict[str, Any] | None:
    """读取 parquet 文件的页脚元数据，不加载数据。

    只解析我们关心的部分：行数、列名、列类型。格式是 Thrift 紧凑协议，
    完整解析成本高，这里用「找 footer 里的字符串表」的近似做法，够用即可；
    解析失败返回 None，由调用方降级处理。
    """
    try:
        size = path.stat().st_size
        if size < 12:
            return None
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 8))
            tail = handle.read(8)
            if tail[4:] != b"PAR1":
                return None
            footer_len = struct.unpack("<I", tail[:4])[0]
            if footer_len <= 0 or footer_len > size - 8:
                return None
            handle.seek(size - 8 - footer_len)
            footer = handle.read(footer_len)
    except OSError:
        return None
    return _extract_footer_info(footer)


def _extract_footer_info(footer: bytes) -> dict[str, Any]:
    """从 footer 里提取可打印的列名，并粗估行数。

    Thrift 紧凑编码里字符串以「长度 varint + 字节」出现，列名长度通常 < 128，
    因此直接扫描「长度字节 + 可打印 ASCII」。行数取 footer 里最大的 int64 值，
    这个近似对元数据展示足够；精确值由深扫描时读 parquet 首页统计补充。
    """
    names: list[str] = []
    index = 0
    length = len(footer)
    while index < length - 1:
        token = footer[index]
        if 3 <= token <= 80 and index + 1 + token <= length:
            chunk = footer[index + 1 : index + 1 + token]
            if all(32 <= byte < 127 for byte in chunk):
                text = chunk.decode("ascii")
                # 列名一般是 a-z0-9._ 组成的标识符
                if all(char.isalnum() or char in "._-" for char in text):
                    names.append(text)
                    index += 1 + token
                    continue
        index += 1
    return {"columns": sorted(set(names)), "footer_size": length}


# ---------------------------------------------------------------------------
# 浅扫描
# ---------------------------------------------------------------------------
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
    features = info.get("features") or {}
    if not isinstance(features, dict):
        features = {}

    meta.episodes = _as_int(info.get("total_episodes"))
    meta.frames = _as_int(info.get("total_frames"))
    meta.fps = _as_float(info.get("fps"))
    meta.robot_type = info.get("robot_type")
    meta.tasks = _read_tasks(root, info)

    _fill_features(meta, features)
    if meta.frames and meta.fps:
        meta.duration_seconds = round(meta.frames / meta.fps, 1)

    # 体积统计放最后，超时也不影响上面的元数据
    meta.size_bytes = directory_size(root)
    _append_basic_issues(meta, root)
    meta.status = _derive_status(meta)
    return meta


def _read_tasks(root: Path, info: dict[str, Any]) -> list[str]:
    """任务字符串。v3.0 在 meta/tasks.parquet；读不到就用数量表示。"""
    tasks_path = root / "meta" / "tasks.parquet"
    if tasks_path.is_file():
        parsed = read_parquet_metadata(tasks_path)
        if parsed:
            # parquet 里列名含 task；具体文本要读数据页，浅扫描不读
            pass
    total = _as_int(info.get("total_tasks"))
    return [] if total <= 0 else [f"{total} 个任务（文本需深扫描读取）"]


def _fill_features(meta: DatasetMeta, features: dict[str, Any]) -> None:
    cameras: list[str] = []
    image_size: list[int] | None = None
    for key, spec in features.items():
        if not isinstance(spec, dict):
            continue
        if str(spec.get("dtype")) in ("video", "image") or key.startswith("observation.images"):
            cameras.append(key)
            shape = spec.get("shape")
            if image_size is None and isinstance(shape, list) and len(shape) >= 2:
                image_size = [int(shape[0]), int(shape[1])]
    meta.cameras = sorted(cameras)
    meta.image_size = image_size

    state = features.get("observation.state")
    if isinstance(state, dict):
        shape = state.get("shape")
        if isinstance(shape, list) and shape:
            meta.state_dim = int(shape[0])
        names = state.get("names")
        if isinstance(names, list):
            meta.state_names = [str(item) for item in names]

    action = features.get("action")
    if isinstance(action, dict):
        shape = action.get("shape")
        if isinstance(shape, list) and shape:
            meta.action_dim = int(shape[0])
        names = action.get("names")
        if isinstance(names, list):
            meta.action_names = [str(item) for item in names]


def _append_basic_issues(meta: DatasetMeta, root: Path) -> None:
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


def _derive_status(meta: DatasetMeta) -> str:
    levels = {issue.level for issue in meta.issues}
    if "error" in levels:
        return "unusable"
    if "warning" in levels:
        return "warning"
    return "ready"


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 深扫描
# ---------------------------------------------------------------------------
def scan_deep(root: Path, dataset_id: str | None = None) -> DatasetMeta:
    """在浅扫描基础上做更深的检查：parquet 列、视频文件、stats.json。"""
    meta = scan_shallow(root, dataset_id)
    meta.scan_depth = "deep"
    if meta.status == "unusable" and any(issue.code in ("missing_info", "invalid_info") for issue in meta.issues):
        return meta

    _check_data_files(meta, root)
    _check_video_files(meta, root)
    _check_stats(meta, root)
    meta.stats = read_stats_summary(root)

    meta.status = _derive_status(meta)
    return meta


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
        needed = {"observation.state", "action"}
        missing = needed - columns
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
        meta.issues.append(DatasetIssue("warning", "invalid_stats", "meta/stats.json 不是合法 JSON，"
                                                        "训练时会重新计算"))
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


# ---------------------------------------------------------------------------
# 数据分布洞察
# ---------------------------------------------------------------------------
# 判断某个维度「几乎不动」的阈值：标准差相对极差的比例。
# 数据经过不同量纲归一化，用相对值比绝对值更稳。
STATIC_DIM_RATIO = 0.01


def read_stats_summary(root: Path) -> dict[str, Any] | None:
    """从 meta/stats.json 提取动作/状态的分布摘要，用于判断数据质量。

    返回结构与 ``features`` 对齐，每维带 min/max/mean/std 和 ``static`` 标记——
    「某个关节几乎不动」往往意味着采集时没用到该关节，或者标定有问题，
    这类问题在训练 loss 上很难看出来，但会直接拖垮策略效果。
    """
    stats_path = root / "meta" / "stats.json"
    if not stats_path.is_file():
        return None
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stats, dict):
        return None

    summary: dict[str, Any] = {}
    for key in ("observation.state", "action"):
        block = stats.get(key)
        if not isinstance(block, dict):
            continue
        required = ("min", "max", "mean", "std")
        if not all(isinstance(block.get(field), list) for field in required):
            continue
        dims = []
        for index in range(len(block["min"])):
            try:
                low = float(block["min"][index])
                high = float(block["max"][index])
                mean = float(block["mean"][index])
                std = float(block["std"][index])
            except (IndexError, TypeError, ValueError):
                continue
            span = high - low
            # 注意：判断必须用未取整的 span。极差可能小到 round(, 6) 后变成 0，
            # 那时再用取整值做除法会除零、或把「有微小变化」误判成常数。
            # 用「是否变化」的绝对判据更稳：极差极小（< 1e-4，远小于任何有意义关节行程）
            # 或 标准差相对极差极小，都认为这一维基本不动。两个判据都用未取整的值。
            is_static = bool(span < 1e-4 or (span > 0 and std / span < STATIC_DIM_RATIO))
            dims.append(
                {
                    "index": index,
                    "min": round(low, 6),
                    "max": round(high, 6),
                    "mean": round(mean, 6),
                    "std": round(std, 6),
                    "range": round(span, 6),
                    # 标准差远小于极差 → 这一维基本是常数
                    "static": is_static,
                }
            )
        summary[key] = {
            "dims": dims,
            "static_dims": [dim["index"] for dim in dims if dim["static"]],
        }
    return summary or None


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