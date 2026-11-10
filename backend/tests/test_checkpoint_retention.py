"""checkpoint 保留策略测试。

背景：实测单个 SmolVLA checkpoint 约 1.3GB。按 save_freq=1000 推算，
12 小时训练会写出约 40 个，合计 52GB，足以把磁盘打满。
但直接删旧的是错的——续训要最近的、最终产物要最好的。

测试重点全是「什么不能删」。
"""

from __future__ import annotations

import pytest
from app.services.checkpoint_retention import (
    apply_retention,
    list_checkpoints,
    plan_retention,
)


@pytest.fixture
def ckpt_root(tmp_path):
    root = tmp_path / "checkpoints"
    root.mkdir()
    for step in (1000, 2000, 3000, 4000, 5000):
        d = root / f"{step:06d}"
        d.mkdir()
        (d / "model.safetensors").write_bytes(b"x" * 128)
    return root


def test_list_only_numeric_dirs(ckpt_root):
    """lerobot 在 checkpoints/ 下还会放 last 之类的东西，不能当 checkpoint 计数。"""
    (ckpt_root / "last").mkdir()
    (ckpt_root / "notes.txt").write_text("x")

    found = list_checkpoints(ckpt_root)

    assert [step for step, _ in found] == [1000, 2000, 3000, 4000, 5000]


def test_keep_last_n(ckpt_root):
    plan = plan_retention(ckpt_root, keep_last_n=2)

    removed = sorted(item["step"] for item in plan["remove"])
    kept = sorted(item["step"] for item in plan["keep"])
    assert kept == [4000, 5000]
    assert removed == [1000, 2000, 3000]


def test_keep_last_n_zero_still_keeps_newest(ckpt_root):
    """配置写 0 也不能把刚训出来的结果删掉——那是最坏的情况。"""
    plan = plan_retention(ckpt_root, keep_last_n=0)

    assert 5000 in [item["step"] for item in plan["keep"]]


def test_keep_best_protects_lowest_loss(ckpt_root):
    """训练到最后 loss 未必最低，用户真正想留的是效果最好的那个。"""
    plan = plan_retention(ckpt_root, keep_last_n=1, keep_best=True, best_step=1000)

    kept = [item["step"] for item in plan["keep"]]
    assert 1000 in kept, "最好的 checkpoint 被删了"
    assert 5000 in kept, "最新的 checkpoint 被删了"


def test_keep_best_without_hint_keeps_newest(ckpt_root):
    """没有 loss 数据时选最保险的，而不是瞎猜。"""
    plan = plan_retention(ckpt_root, keep_last_n=1, keep_best=True, best_step=None)

    assert [item["step"] for item in plan["keep"]] == [5000]


def test_apply_reports_saved_bytes(ckpt_root):
    plan = plan_retention(ckpt_root, keep_last_n=1)

    assert plan["saved_bytes"] > 0


def test_apply_actually_removes(ckpt_root):
    plan = plan_retention(ckpt_root, keep_last_n=1)

    result = apply_retention(ckpt_root, plan)

    assert sorted(result["removed"]) == [1000, 2000, 3000, 4000]
    assert sorted(p.name for p in ckpt_root.iterdir()) == ["005000"]


def test_empty_root(tmp_path):
    root = tmp_path / "nonexistent"

    plan = plan_retention(root, keep_last_n=3)

    assert plan["remove"] == []
    assert plan["keep"] == []


def test_fewer_than_keep_removes_nothing(tmp_path):
    root = tmp_path / "checkpoints"
    root.mkdir()
    (root / "001000").mkdir()

    plan = plan_retention(root, keep_last_n=3)

    assert plan["remove"] == []
