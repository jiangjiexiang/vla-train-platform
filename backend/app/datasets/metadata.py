"""数据集扫描结果及 LeRobot info.json 元数据规范化。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .lerobot import read_task_strings


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


def normalize_metadata(meta: DatasetMeta, root: Path, info: dict[str, Any]) -> None:
    """将 info.json 中的标量、任务和特征写入扫描结果。"""
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


def _read_tasks(root: Path, info: dict[str, Any]) -> list[str]:
    """任务指令列表。优先真实读取，读不到时退回数量说明。"""
    tasks = read_task_strings(root)
    if tasks:
        return tasks
    total = _as_int(info.get("total_tasks"))
    return [] if total <= 0 else [f"{total} 个任务（未能解析出文本）"]


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

    for feature_name, prefix in (("observation.state", "state"), ("action", "action")):
        spec = features.get(feature_name)
        if not isinstance(spec, dict):
            continue
        shape = spec.get("shape")
        if isinstance(shape, list) and shape:
            setattr(meta, f"{prefix}_dim", int(shape[0]))
        names = spec.get("names")
        if isinstance(names, list):
            setattr(meta, f"{prefix}_names", [str(item) for item in names])


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
