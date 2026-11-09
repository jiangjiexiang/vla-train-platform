"""LeRobot 数据集扫描器测试。

用临时目录构造各种数据集形态（正常 / 缺文件 / 损坏），验证扫描结果的
元数据提取与 issue 分级。真实数据集（pick1028）的扫描结果另有集成检查，
这里只测纯逻辑，保证不依赖机器上的实际数据。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.datasets.scanner import (
    DatasetMeta,
    detect_format,
    directory_size,
    list_dataset_dirs,
    read_parquet_metadata,
    scan_deep,
    scan_shallow,
)


def make_info(**overrides):
    info = {
        "codebase_version": "v3.0",
        "robot_type": "dual_arm_mobile",
        "total_episodes": 143,
        "total_frames": 60470,
        "total_tasks": 2,
        "chunks_size": 1000,
        "fps": 30,
        "features": {
            "observation.state": {"dtype": "float32", "shape": [14], "names": [f"s{i}" for i in range(14)]},
            "action": {"dtype": "float32", "shape": [14], "names": [f"a{i}" for i in range(14)]},
            "observation.images.camera1": {"dtype": "video", "shape": [480, 640, 3]},
            "observation.images.camera2": {"dtype": "video", "shape": [480, 640, 3]},
        },
    }
    info.update(overrides)
    return info


def build_dataset(root: Path, info=None, *, raw_info=None, dirs=("data", "videos"), files=None) -> Path:
    dataset = root / "ds"
    (dataset / "meta").mkdir(parents=True)
    if raw_info is not None:
        (dataset / "meta" / "info.json").write_text(raw_info, encoding="utf-8")
    elif info is not None:
        (dataset / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")
    for name in dirs:
        (dataset / name).mkdir(parents=True, exist_ok=True)
    for relative, content in (files or {}).items():
        target = dataset / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return dataset


# --------------------------------------------------------------------------
# 基础扫描
# --------------------------------------------------------------------------
def test_shallow_scan_extracts_metadata(tmp_path):
    dataset = build_dataset(tmp_path, make_info())

    meta = scan_shallow(dataset, "pick1028")

    assert meta.id == "pick1028"
    assert meta.format == "lerobot-v3.0"
    assert meta.episodes == 143
    assert meta.frames == 60470
    assert meta.fps == 30.0
    assert meta.robot_type == "dual_arm_mobile"
    assert meta.cameras == ["observation.images.camera1", "observation.images.camera2"]
    assert meta.image_size == [480, 640]
    assert meta.state_dim == 14
    assert meta.action_dim == 14
    # 时长为 frames / fps，保留 1 位小数（展示用，不追求精确）
    assert meta.duration_seconds == 2015.7
    assert meta.state_names[0] == "s0"
    assert meta.action_names[0] == "a0"


def test_shallow_scan_marks_missing_info_as_unusable(tmp_path):
    dataset = build_dataset(tmp_path, None)

    meta = scan_shallow(dataset)

    assert meta.status == "unusable"
    assert any(issue.code == "missing_info" for issue in meta.issues)


def test_shallow_scan_marks_invalid_json_as_unusable(tmp_path):
    dataset = build_dataset(tmp_path, raw_info="{not json")

    meta = scan_shallow(dataset)

    assert meta.status == "unusable"
    assert any(issue.code == "invalid_info" for issue in meta.issues)


def test_shallow_scan_is_ready_for_valid_dataset(tmp_path):
    dataset = build_dataset(tmp_path, make_info())

    meta = scan_shallow(dataset)

    assert meta.status == "ready"
    assert meta.issues == []


# --------------------------------------------------------------------------
# 问题分级
# --------------------------------------------------------------------------
def test_missing_state_feature_is_error(tmp_path):
    info = make_info()
    del info["features"]["observation.state"]
    dataset = build_dataset(tmp_path, info)

    meta = scan_shallow(dataset)

    codes = {(issue.level, issue.code) for issue in meta.issues}
    assert ("error", "missing_feature") in codes
    assert meta.status == "unusable"


def test_no_episodes_is_error(tmp_path):
    dataset = build_dataset(tmp_path, make_info(total_episodes=0))

    meta = scan_shallow(dataset)

    assert ("error", "no_episode") in {(issue.level, issue.code) for issue in meta.issues}


def test_tiny_dataset_is_warning(tmp_path):
    dataset = build_dataset(tmp_path, make_info(total_frames=500))

    meta = scan_shallow(dataset)

    assert ("warning", "tiny_dataset") in {(issue.level, issue.code) for issue in meta.issues}
    assert meta.status == "warning"


def test_no_camera_is_warning(tmp_path):
    info = make_info()
    info["features"] = {
        "observation.state": {"shape": [7]},
        "action": {"shape": [7]},
    }
    dataset = build_dataset(tmp_path, info)

    meta = scan_shallow(dataset)

    assert ("warning", "no_camera") in {(issue.level, issue.code) for issue in meta.issues}
    assert meta.cameras == []


def test_dim_mismatch_is_info_only(tmp_path):
    info = make_info()
    info["features"]["observation.state"] = {"shape": [7]}
    info["features"]["action"] = {"shape": [14]}
    dataset = build_dataset(tmp_path, info)

    meta = scan_shallow(dataset)

    assert ("info", "dim_mismatch") in {(issue.level, issue.code) for issue in meta.issues}
    # info 不该把 status 拉低
    assert meta.status == "ready"


# --------------------------------------------------------------------------
# 深扫描
# --------------------------------------------------------------------------
def test_deep_scan_flags_missing_data_files(tmp_path):
    dataset = build_dataset(tmp_path, make_info())

    meta = scan_deep(dataset)

    assert ("error", "no_data_file") in {(issue.level, issue.code) for issue in meta.issues}
    assert meta.scan_depth == "deep"


def test_deep_scan_flags_missing_videos_dir(tmp_path):
    dataset = build_dataset(tmp_path, make_info(), dirs=("data",))

    meta = scan_deep(dataset)

    assert ("error", "missing_videos_dir") in {(issue.level, issue.code) for issue in meta.issues}


def test_missing_stats_is_info_not_warning(tmp_path):
    """stats.json 缺失由平台在训练时自动计算，不应拦住用户。"""
    dataset = build_dataset(tmp_path, make_info())

    meta = scan_deep(dataset)

    assert ("info", "missing_stats") in {(issue.level, issue.code) for issue in meta.issues}


def test_deep_scan_does_not_reraise_for_broken_info(tmp_path):
    dataset = build_dataset(tmp_path, None)

    meta = scan_deep(dataset)

    assert meta.status == "unusable"


# --------------------------------------------------------------------------
# 格式识别
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("version", "has_chunks", "expected"),
    [
        ("v3.0", True, "lerobot-v3.0"),
        ("v2.1", True, "lerobot-v2.1"),
        ("v2.0", False, "lerobot-v2.0"),
    ],
)
def test_detect_format(tmp_path, version, has_chunks, expected):
    info = {"codebase_version": version}
    if has_chunks:
        info["chunks_size"] = 1000
    dataset = build_dataset(tmp_path, info)

    assert detect_format(dataset) == expected


def test_detect_format_without_info(tmp_path):
    (tmp_path / "ds").mkdir()

    assert detect_format(tmp_path / "ds") == "unknown"


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def test_directory_size_sums_files(tmp_path):
    dataset = build_dataset(tmp_path, make_info(), files={"data/a.bin": b"x" * 100, "data/b.bin": b"y" * 50})
    expected = sum(f.stat().st_size for f in dataset.rglob("*") if f.is_file())

    assert directory_size(dataset) == expected
    assert expected >= 150


def test_read_parquet_metadata_rejects_non_parquet(tmp_path):
    target = tmp_path / "fake.parquet"
    target.write_bytes(b"not a parquet file at all")

    assert read_parquet_metadata(target) is None


def test_read_parquet_metadata_handles_missing_file(tmp_path):
    assert read_parquet_metadata(tmp_path / "missing.parquet") is None


def test_list_dataset_dirs_skips_hidden_and_temp(tmp_path):
    for name in ("alpha", ".hidden", "beta.tmp", "gamma"):
        (tmp_path / name).mkdir()
    (tmp_path / "afile").write_text("x")

    names = [path.name for path in list_dataset_dirs(tmp_path)]

    assert names == ["alpha", "gamma"]


def test_list_dataset_dirs_missing_root(tmp_path):
    assert list_dataset_dirs(tmp_path / "nope") == []


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------
def test_meta_roundtrips_through_dict(tmp_path):
    meta = scan_shallow(build_dataset(tmp_path, make_info()), "pick1028")

    restored = DatasetMeta.from_dict(meta.as_dict())

    assert restored.id == meta.id
    assert restored.episodes == meta.episodes
    assert restored.cameras == meta.cameras
    assert restored.as_dict() == meta.as_dict()

def test_deep_scan_flags_missing_quantiles(tmp_path):
    """π0.5 的 QUANTILES 归一化需要 q01/q99，缺失时应提前提示。"""
    dataset = build_dataset(tmp_path, make_info())
    (dataset / "meta" / "stats.json").write_text(
        json.dumps({"action": {"count": [1], "min": [-1], "max": [1]}}), encoding="utf-8"
    )

    meta = scan_deep(dataset)

    assert ("info", "missing_quantiles") in {(issue.level, issue.code) for issue in meta.issues}


def test_deep_scan_accepts_stats_with_quantiles(tmp_path):
    dataset = build_dataset(tmp_path, make_info())
    (dataset / "meta" / "stats.json").write_text(
        json.dumps({"action": {"count": [1], "min": [-1], "max": [1], "q01": [-0.9], "q99": [0.9]}}),
        encoding="utf-8",
    )

    meta = scan_deep(dataset)

    assert not any(issue.code == "missing_quantiles" for issue in meta.issues)


def test_shallow_scan_flags_leftover_backup_dirs(tmp_path):
    """meta.bak 这类残留目录会让按 glob 统计的工具重复计数。"""
    dataset = build_dataset(tmp_path, make_info())
    (dataset / "meta.bak").mkdir()

    meta = scan_shallow(dataset)

    assert ("info", "leftover_backup") in {(issue.level, issue.code) for issue in meta.issues}

def test_deep_scan_extracts_stats_summary(tmp_path):
    """stats.json 的分布摘要用于判断数据质量（某个关节是否几乎不动）。"""
    dataset = build_dataset(tmp_path, make_info())
    (dataset / "meta" / "stats.json").write_text(
        json.dumps(
            {
                "action": {
                    "min": [-1.0, 0.5, 0.0],
                    "max": [1.0, 0.5000001, 2.0],
                    "mean": [0.0, 0.5, 1.0],
                    "std": [0.5, 0.0000001, 0.6],
                }
            }
        ),
        encoding="utf-8",
    )

    meta = scan_deep(dataset)

    assert meta.stats is not None
    action = meta.stats["action"]
    assert len(action["dims"]) == 3
    # 第 1 维极差几乎为 0，应标记为 static
    assert 1 in action["static_dims"]
    assert 0 not in action["static_dims"]
    assert action["dims"][0]["range"] == 2.0


def test_deep_scan_without_stats_returns_none(tmp_path):
    dataset = build_dataset(tmp_path, make_info())

    meta = scan_deep(dataset)

    assert meta.stats is None


def test_read_stats_summary_tolerates_malformed(tmp_path):
    dataset = build_dataset(tmp_path, make_info())
    (dataset / "meta" / "stats.json").write_text(
        json.dumps({"action": {"min": "not a list"}}), encoding="utf-8"
    )

    from app.datasets.scanner import read_stats_summary

    assert read_stats_summary(dataset) is None

