"""提交前预检测试。

核心原则：**该拦的必须拦，不该拦的绝不错伤**。
误伤「GPU 当前被占满」会让排队功能失去意义——那正是队列存在的理由。
"""

from __future__ import annotations

from app.services.preflight import preflight


def good_dataset(**overrides):
    dataset = {
        "id": "pick1028",
        "format": "lerobot-v3.0",
        "status": "ready",
        "episodes": 143,
        "frames": 60470,
        "cameras": ["observation.images.camera1"],
        "issues": [],
        "stats": {"action": {"dims": []}},
    }
    dataset.update(overrides)
    return dataset


def good_recipe(**overrides):
    recipe = {
        "id": "smolvla",
        "name": "SmolVLA 微调",
        "dataset_formats": ["lerobot-v3.0"],
        "gpu_count": 1,
        "estimated_vram_gb": 12,
    }
    recipe.update(overrides)
    return recipe


def run(dataset=None, recipe=None, total=1, allocated=0, gpus=1):
    return preflight(
        dataset=dataset or good_dataset(),
        recipe=recipe or good_recipe(),
        gpu_total=total,
        gpu_allocated=allocated,
        requested_gpus=gpus,
    )


def codes(result):
    return {check["code"] for check in result["checks"]}


def test_clean_dataset_passes():
    result = run()

    assert result["ok"] is True
    assert result["summary"]["errors"] == 0


def test_unusable_dataset_blocks_submit():
    dataset = good_dataset(
        status="unusable",
        issues=[{"level": "error", "code": "missing_info", "message": "缺少 meta/info.json"}],
    )

    result = run(dataset=dataset)

    assert result["ok"] is False
    assert "dataset_unusable" in codes(result)
    check = next(c for c in result["checks"] if c["code"] == "dataset_unusable")
    assert check["suggestion"]


def test_format_mismatch_blocks_submit():
    result = run(dataset=good_dataset(format="lerobot-v2.1"))

    assert result["ok"] is False
    assert "format_mismatch" in codes(result)


def test_no_camera_blocks_submit():
    result = run(dataset=good_dataset(cameras=[]))

    assert result["ok"] is False
    assert "no_camera" in codes(result)


def test_warning_dataset_does_not_block():
    dataset = good_dataset(
        status="warning",
        issues=[{"level": "warning", "code": "tiny_dataset", "message": "样本很少"}],
    )

    result = run(dataset=dataset)

    assert result["ok"] is True
    assert "dataset_warning" in codes(result)


def test_few_episodes_is_warning_only():
    result = run(dataset=good_dataset(episodes=5))

    assert result["ok"] is True
    assert "few_episodes" in codes(result)


def test_requesting_more_gpus_than_cluster_blocks():
    result = run(total=1, gpus=4)

    assert result["ok"] is False
    assert "gpu_exceeds_total" in codes(result)


def test_busy_cluster_does_not_block():
    """GPU 当前全被占用不是错误——排队正是为此存在。"""
    result = run(total=1, allocated=1, gpus=1)

    assert result["ok"] is True
    assert "will_queue" in codes(result)


def test_zero_gpu_cluster_is_not_an_error():
    """测试环境没有 GPU 上报时不能误伤。"""
    result = run(total=0, allocated=0, gpus=1)

    assert result["ok"] is True
    assert "gpu_total_unknown" in codes(result)


def test_missing_quantiles_is_info():
    dataset = good_dataset(
        issues=[{"level": "info", "code": "missing_quantiles", "message": "缺 q01/q99"}]
    )

    result = run(dataset=dataset)

    assert result["ok"] is True
    check = next(c for c in result["checks"] if c["code"] == "missing_quantiles")
    assert "pi0" in check["suggestion"] or "π0" in check["suggestion"]


def test_checks_all_have_suggestions():
    """每条检查都必须告诉用户怎么办。"""
    dataset = good_dataset(
        status="unusable",
        issues=[{"level": "error", "code": "x", "message": "坏了"}],
    )
    result = run(dataset=dataset, total=1, gpus=8)

    for check in result["checks"]:
        assert check["message"], check["code"]
        assert check["suggestion"], check["code"]


def test_summary_counts_match():
    dataset = good_dataset(status="warning", episodes=3)
    result = run(dataset=dataset, total=1, allocated=1)

    assert result["summary"]["errors"] == sum(1 for c in result["checks"] if c["level"] == "error")
    assert result["summary"]["warnings"] == sum(1 for c in result["checks"] if c["level"] == "warning")