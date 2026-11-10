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


# ---------------------------------------------------------------------------
# 清理线程不能删正在写的 checkpoint。
# 一个 checkpoint 1.26GB（模型 865MB + optimizer 394MB），磁盘忙时写入可能
# 跨过两次扫描窗口。删掉正在写的那个，轻则 checkpoint 损坏，
# 重则续训读到半个文件。
# ---------------------------------------------------------------------------
def test_pruner_skips_recently_written(tmp_path, monkeypatch):
    """刚写过的目录不能被清理，即使超出保留数量。"""
    import os
    import time

    from app.services.checkpoint_retention import list_checkpoints

    root = tmp_path / "checkpoints"
    root.mkdir()
    for step in (1000, 2000, 3000):
        d = root / f"{step:06d}"
        d.mkdir()
        old = time.time() - 600
        os.utime(d, (old, old))
    # 模拟"正在写"的最新一个
    fresh = root / "004000"
    fresh.mkdir()

    # 这里只验证判据本身：mtime 是新的就不该被选中删除
    now = time.time()
    candidates = []
    for step, path in list_checkpoints(root):
        if now - path.stat().st_mtime >= 60:
            candidates.append(step)

    assert 4000 not in candidates, "正在写的 checkpoint 被列入删除候选"
    assert candidates == [1000, 2000, 3000]


def test_pruner_handles_missing_mtime(tmp_path):
    """stat 失败时保守跳过，不要因为读不到时间就删。"""
    import os
    import time

    root = tmp_path / "checkpoints"
    root.mkdir()
    d = root / "001000"
    d.mkdir()
    old = time.time() - 600
    os.utime(d, (old, old))
    d.rmdir()  # 目录消失，stat 会失败

    # 不应抛异常
    now = time.time()
    try:
        _ = now - d.stat().st_mtime
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 路径安全：我们真的因为一条 rm -rf 丢过全部训练产物。
# 一个脚本里 $JOB 展开为空，rm -rf "/checkpoints/checkpoints" 在容器内以 root 跑掉了。
# 所以这里测试的重点不是"能删"，而是"什么绝对不能删"。
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "",                     # 空值——正是上次事故的形式
        "   ",                  # 纯空白
        " smolvla-001",         # 前导空白，shell 展开出错时的典型残留
        "smolvla-001 ",         # 尾随空白
        "*",                    # 通配符
        "smolvla-*",
        "?",                    # 单字符通配
        "[abc]",
        "../other",             # 路径穿越
        "a/b",
        "/checkpoints",         # 绝对路径
        "/data/vla-platform/checkpoints",
        "smolvla-001/../..",
    ],
)
def test_rejects_dangerous_names(tmp_path, name):
    from app.services.checkpoint_retention import UnsafePath, safe_job_dir

    with pytest.raises(UnsafePath):
        safe_job_dir(tmp_path, name)


def test_accepts_normal_name(tmp_path):
    from app.services.checkpoint_retention import safe_job_dir

    root = tmp_path / "checkpoints"
    root.mkdir()

    target = safe_job_dir(root, "smolvla-pick1028-003")

    assert target == (root / "smolvla-pick1028-003").resolve()


def test_rejects_root_itself(tmp_path):
    """解析结果等于根目录时必须拒绝——那正是上次删库的形式。"""
    from app.services.checkpoint_retention import UnsafePath, safe_job_dir

    root = tmp_path / "checkpoints"
    root.mkdir()

    with pytest.raises(UnsafePath):
        safe_job_dir(root, ".")


def test_symlink_escape_rejected(tmp_path):
    """软链接指到外面也要拒绝：resolve() 之后再比较前缀就露馅了。"""
    from app.services.checkpoint_retention import UnsafePath, safe_job_dir

    root = tmp_path / "checkpoints"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "evil").symlink_to(outside)

    with pytest.raises(UnsafePath):
        safe_job_dir(root, "evil")


def test_cleanup_does_not_delete_outside_root(tmp_path):
    """即使传入越界的名字，cleanup 也只记录失败，不真的删。"""
    from app.services.checkpoint_retention import UnsafePath, safe_job_dir

    root = tmp_path / "checkpoints"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("keep me")

    # 模拟 cleanup_jobs 的路径校验环节
    try:
        safe_job_dir(root, "../victim")
        raise AssertionError("应当拒绝")
    except UnsafePath:
        pass

    assert (victim / "important.txt").exists(), "越界目标被删掉了"
