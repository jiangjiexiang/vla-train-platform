"""训练指标读取测试（services/metrics.py）。"""

from __future__ import annotations

import json

import pytest
from app.services.metrics import append_metrics, metrics_path, read_metrics


@pytest.fixture
def root(tmp_path):
    return tmp_path / "checkpoints"


def test_read_metrics_returns_empty_when_missing(root):
    result = read_metrics(root, "job1")

    assert result["items"] == []
    assert result["path_exists"] is False
    assert result["total_points"] == 0


def test_read_metrics_parses_and_sorts(root):
    append_metrics(root, "job1", [{"step": 2, "loss": 0.5}, {"step": 1, "loss": 0.9}])

    result = read_metrics(root, "job1")

    assert [item["step"] for item in result["items"]] == [1, 2]
    assert result["items"][1]["loss"] == 0.5
    assert result["path_exists"] is True


def test_read_metrics_skips_torn_last_line(root):
    path = metrics_path(root, "job1")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"step": 1, "loss": 0.9}) + "\n" + '{"step": 2, "lo',
        encoding="utf-8",
    )

    result = read_metrics(root, "job1")

    assert [item["step"] for item in result["items"]] == [1]


def test_read_metrics_keeps_last_value_for_duplicate_step(root):
    append_metrics(root, "job1", [{"step": 1, "loss": 0.9}, {"step": 1, "loss": 0.1}])

    result = read_metrics(root, "job1")

    assert len(result["items"]) == 1
    assert result["items"][0]["loss"] == 0.1


def test_read_metrics_limits_points(root):
    append_metrics(root, "job1", [{"step": i, "loss": float(i)} for i in range(100)])

    result = read_metrics(root, "job1", limit=10)

    assert len(result["items"]) == 10
    assert result["total_points"] == 100
    assert result["truncated"] is True
    # 保留最新的点
    assert result["items"][-1]["step"] == 99


def test_read_metrics_ignores_non_numeric_fields(root):
    append_metrics(root, "job1", [{"step": 1, "loss": 0.5, "note": "hi", "flag": True, "timestamp": "2026-01-01"}])

    point = read_metrics(root, "job1")["items"][0]

    assert point["loss"] == 0.5
    assert "note" not in point
    assert "flag" not in point
    assert point["timestamp"] == "2026-01-01"


def test_read_metrics_skips_records_without_step(root):
    append_metrics(root, "job1", [{"loss": 0.5}, {"step": 3, "loss": 0.2}])

    result = read_metrics(root, "job1")

    assert [item["step"] for item in result["items"]] == [3]


def test_read_metrics_only_tails_large_file(root):
    """文件很大时只读末尾，不应把整个文件读进内存。"""
    path = metrics_path(root, "big")
    path.parent.mkdir(parents=True)
    lines = [json.dumps({"step": i, "loss": 0.1, "pad": "x" * 100}) for i in range(20000)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = read_metrics(root, "big", limit=5, max_bytes=64 * 1024)

    assert len(result["items"]) == 5
    # 只读末尾，所以 total_points 远小于全部 20000
    assert 0 < result["total_points"] < 20000
    assert result["items"][-1]["step"] == 19999


def test_append_metrics_creates_parent(root):
    append_metrics(root, "new-job", [{"step": 1}])

    assert (root / "new-job" / "metrics.jsonl").is_file()