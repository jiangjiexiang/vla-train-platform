"""训练耗时预估测试。

用户填 5000 步时最想知道要跑多久。预估不准比没有更糟——
所以这里重点测「什么时候不该用历史数据」。
"""

from __future__ import annotations

from app.services.estimate import DEFAULT_STEPS_PER_SECOND, estimate_duration


def test_uses_history_when_available():
    # 实测：3000 步 1020 秒 -> 2.94 步/秒
    history = [{"steps": 3000, "batch_size": 8, "duration_seconds": 1020}]

    result = estimate_duration(steps=2000, batch_size=8, history=history)

    assert result["source"] == "history"
    assert result["samples"] == 1
    # 2000 / 2.94 ≈ 680 秒；允许小幅误差
    assert 600 <= result["estimated_seconds"] <= 760


def test_falls_back_without_history():
    result = estimate_duration(steps=2000, batch_size=8, history=[])

    assert result["source"] == "default"
    assert result["steps_per_second"] == DEFAULT_STEPS_PER_SECOND


def test_ignores_different_batch_size():
    """不同 batch 的速度差很多，混在一起算平均没有意义。"""
    history = [{"steps": 3000, "batch_size": 64, "duration_seconds": 3000}]

    result = estimate_duration(steps=1000, batch_size=8, history=history)

    assert result["source"] == "default"


def test_ignores_very_short_runs():
    """跑 5 步的任务主要时间花在启动上，速度严重偏低，不能当基准。"""
    history = [{"steps": 5, "batch_size": 8, "duration_seconds": 35}]

    result = estimate_duration(steps=1000, batch_size=8, history=history)

    assert result["source"] == "default"


def test_skips_samples_without_duration():
    history = [
        {"steps": 3000, "batch_size": 8, "duration_seconds": None},
        {"steps": None, "batch_size": 8, "duration_seconds": 100},
    ]

    result = estimate_duration(steps=500, batch_size=8, history=history)

    assert result["source"] == "default"


def test_range_is_wider_than_point_estimate():
    """训练速度受数据加载和 GPU 争用影响大，只给单点会误导。"""
    result = estimate_duration(steps=1000, batch_size=8, history=[])

    assert result["range_text"] != result["estimated_text"]
    assert "~" in result["range_text"]


def test_speed_has_floor():
    """病态历史（几乎不动）不能除出天文数字。"""
    history = [{"steps": 100, "batch_size": 8, "duration_seconds": 100000}]

    result = estimate_duration(steps=1000, batch_size=8, history=history)

    assert result["estimated_seconds"] < 10**7


def test_formatting_units():
    """秒 / 分钟 / 小时三种单位都要能正常显示。"""
    fast = estimate_duration(steps=50, batch_size=8, history=[])
    slow = estimate_duration(steps=100000, batch_size=8, history=[])

    assert "秒" in fast["estimated_text"] or "分钟" in fast["estimated_text"]
    assert "小时" in slow["estimated_text"]
