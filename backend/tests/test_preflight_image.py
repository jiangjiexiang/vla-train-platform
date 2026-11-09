"""预检的镜像可用性检查。

Web 进程查不到节点上的镜像，只能按历史推断。这个检查防的是一个真实事故：
用了已删除镜像的任务被 Kueue 准入、占住唯一 GPU，ImagePullBackOff 一直重试，
后面所有任务永久排队，而用户只看到「排队中」。
"""

from __future__ import annotations

import pytest
from app.services.preflight import preflight


@pytest.fixture
def dataset_for_preflight() -> dict:
    return {
        "id": "pick1028",
        "format": "lerobot-v3.0",
        "path": "/datasets/pick1028",
        "summary": {
            "episodes": 143,
            "frames": 60470,
            "cameras": ["top", "left", "right"],
            "state_dim": 14,
            "action_dim": 14,
        },
    }


@pytest.fixture
def recipe_for_preflight() -> dict:
    return {
        "id": "smolvla-finetune",
        "name": "SmolVLA 微调",
        "image": "vla-smolvla:latest",
        "dataset_formats": ["lerobot-v3.0"],
        "template": {"params": []},
    }


# ---------------------------------------------------------------------------
# 镜像可用性：Web 查不到节点镜像，只能靠历史推断。这个检查防的是
# 「用了已删除镜像的任务被准入、占住 GPU、ImagePullBackOff 卡死整个队列」。
# ---------------------------------------------------------------------------
def _image_checks(checks: list) -> list:
    return [c for c in checks if c["code"] == "image_unknown"]


def test_unknown_image_warns(dataset_for_preflight, recipe_for_preflight):
    result = preflight(
        dataset=dataset_for_preflight,
        recipe=recipe_for_preflight,
        gpu_total=1,
        gpu_allocated=0,
        requested_gpus=1,
        incoming_image="vla-never-used:latest",
        known_images=[],
    )

    checks = _image_checks(result["checks"])
    assert len(checks) == 1
    # 只是告警，不能挡住正常提交（刚构建好的镜像也该能用）
    assert checks[0]["level"] == "warning"


def test_known_image_passes(dataset_for_preflight, recipe_for_preflight):
    result = preflight(
        dataset=dataset_for_preflight,
        recipe=recipe_for_preflight,
        gpu_total=1,
        gpu_allocated=0,
        requested_gpus=1,
        incoming_image="vla-smolvla:latest",
        known_images=[{"image": "vla-smolvla:latest"}],
    )

    assert _image_checks(result["checks"]) == []


def test_short_name_gets_hint(dataset_for_preflight, recipe_for_preflight):
    """短名镜像最容易去远端拉然后 403，建议要具体。"""
    result = preflight(
        dataset=dataset_for_preflight,
        recipe=recipe_for_preflight,
        gpu_total=1,
        gpu_allocated=0,
        requested_gpus=1,
        incoming_image="vla-smoke:latest",
        known_images=[],
    )

    check = _image_checks(result["checks"])[0]
    assert "docker.io/library" in check["suggestion"]


def test_no_image_no_check(dataset_for_preflight, recipe_for_preflight):
    result = preflight(
        dataset=dataset_for_preflight,
        recipe=recipe_for_preflight,
        gpu_total=1,
        gpu_allocated=0,
        requested_gpus=1,
        incoming_image=None,
        known_images=[],
    )

    assert _image_checks(result["checks"]) == []
