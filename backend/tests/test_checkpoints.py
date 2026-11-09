"""checkpoint 列举测试。

训练产物的列举看起来简单，但有两个容易忽略的点：
产物目录可能不存在（任务还在跑），以及不该把 metrics.jsonl 当成模型权重。
"""

from __future__ import annotations

import os

from app.services.checkpoints import WEIGHT_SUFFIXES, list_checkpoints


def test_missing_directory_returns_empty(tmp_path):
    result = list_checkpoints(tmp_path, "no-such-job")

    assert result["exists"] is False
    assert result["items"] == []
    assert result["total_bytes"] == 0


def test_lists_files_with_size(tmp_path):
    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    (job_dir / "model.pt").write_bytes(b"x" * 100)

    result = list_checkpoints(tmp_path, "job1")

    assert result["exists"] is True
    assert result["total_bytes"] == 100
    item = result["items"][0]
    assert item["name"] == "model.pt"
    assert item["size_bytes"] == 100
    assert item["is_weights"] is True


def test_metrics_file_is_not_weights(tmp_path):
    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    (job_dir / "metrics.jsonl").write_text("{}\n", encoding="utf-8")

    item = list_checkpoints(tmp_path, "job1")["items"][0]

    assert item["is_weights"] is False


def test_lists_nested_checkpoint_dirs(tmp_path):
    """checkpoint 常按 step 分子目录，要能列出来。"""
    job_dir = tmp_path / "job1"
    (job_dir / "step_000500").mkdir(parents=True)
    (job_dir / "step_000500" / "model.safetensors").write_bytes(b"y" * 50)

    items = list_checkpoints(tmp_path, "job1")["items"]

    assert items[0]["name"] == "step_000500/model.safetensors"
    assert items[0]["is_weights"] is True


def test_all_weight_suffixes_recognized(tmp_path):
    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    for suffix in WEIGHT_SUFFIXES:
        (job_dir / f"model{suffix}").write_bytes(b"z")

    items = list_checkpoints(tmp_path, "job1")["items"]

    assert all(item["is_weights"] for item in items)


def test_sorted_newest_first(tmp_path):
    import time

    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    old = job_dir / "old.pt"
    old.write_bytes(b"a")
    time.sleep(0.01)
    new = job_dir / "new.pt"
    new.write_bytes(b"b")
    # 显式设置时间，避免文件系统精度问题
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    items = list_checkpoints(tmp_path, "job1")["items"]

    assert items[0]["name"] == "new.pt"
