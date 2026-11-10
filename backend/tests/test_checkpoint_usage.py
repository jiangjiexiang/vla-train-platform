"""产物占用统计测试。

清理前最想知道的是「磁盘都花在哪了」。这个接口代替挨个 du，
也顺便让用户看到哪些任务占了空间。
"""

from __future__ import annotations

from app.services.platform import _dir_usage


def test_counts_files_and_bytes(tmp_path):
    job = tmp_path / "job-a"
    (job / "run" / "checkpoints" / "001000").mkdir(parents=True)
    (job / "run" / "checkpoints" / "001000" / "model.safetensors").write_bytes(b"x" * 1000)
    (job / "metrics.jsonl").write_text("x" * 24)

    size, files = _dir_usage(job)

    assert files == 2
    assert size == 1024


def test_empty_dir(tmp_path):
    job = tmp_path / "empty"
    job.mkdir()

    size, files = _dir_usage(job)

    assert files == 0
    assert size == 0


def test_ignores_symlinks(tmp_path):
    """软链接不重复计数，否则 last -> 001000 会让占用翻倍。"""
    job = tmp_path / "job"
    real = job / "001000"
    real.mkdir(parents=True)
    (real / "model.bin").write_bytes(b"x" * 500)
    (job / "last").symlink_to("001000")

    size, files = _dir_usage(job)

    assert files == 1
    assert size == 500


def test_broken_symlink_does_not_crash(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "dangling").symlink_to("nonexistent")

    size, files = _dir_usage(job)

    assert size == 0
    assert files == 0
