"""历史任务清理的规划逻辑测试。

清理是不可逆操作，测试的重点不是「会不会删」，而是**什么情况下不该删**：
运行中的任务、最近的任务、时间解析不出来的任务，都必须被保住。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.cleanup import TERMINAL_STATUSES, plan_cleanup

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def make(name: str, *, status: str = "SUCCEEDED", days_ago: float = 10, finished: bool = True) -> dict:
    when = NOW - timedelta(days=days_ago)
    job = {"name": name, "status": status, "created_at": when.isoformat()}
    if finished:
        job["finished_at"] = when.isoformat()
    return job


def test_deletes_old_terminal_jobs():
    jobs = [make("old-1", days_ago=30), make("old-2", days_ago=20)]

    plan = plan_cleanup(jobs, older_than_days=7, keep_latest=0, now=NOW)

    assert sorted(plan.job_names) == ["old-1", "old-2"]


def test_keeps_running_jobs():
    """运行中的任务即使创建很早也必须保留——用户正在等结果。"""
    jobs = [make("running-1", status="RUNNING", days_ago=30)]

    plan = plan_cleanup(jobs, older_than_days=7, keep_latest=0, now=NOW)

    assert plan.job_names == []
    assert plan.keep == ["running-1"]


def test_keeps_recent_jobs():
    jobs = [make("recent", days_ago=1)]

    plan = plan_cleanup(jobs, older_than_days=7, keep_latest=0, now=NOW)

    assert plan.job_names == []


def test_keep_latest_protects_newest():
    """最新的 N 个永远保留，避免清完发现最近的记录也没了。"""
    jobs = [make(f"job-{i}", days_ago=30 + i) for i in range(5)]

    plan = plan_cleanup(jobs, older_than_days=7, keep_latest=2, now=NOW)

    # job-0 和 job-1 最新（days_ago 最小），应被保住
    assert "job-0" not in plan.job_names
    assert "job-1" not in plan.job_names
    assert "job-2" in plan.job_names


def test_all_terminal_statuses_eligible():
    jobs = [make(f"{s.lower()}", status=s, days_ago=30) for s in TERMINAL_STATUSES]

    plan = plan_cleanup(jobs, older_than_days=7, keep_latest=0, now=NOW)

    assert len(plan.job_names) == len(TERMINAL_STATUSES)


def test_falls_back_to_created_at():
    """老记录可能没有 finished_at，用 created_at 判断。"""
    jobs = [make("no-finished", days_ago=30, finished=False)]

    plan = plan_cleanup(jobs, older_than_days=7, keep_latest=0, now=NOW)

    assert plan.job_names == ["no-finished"]


def test_unparseable_time_is_reported_not_deleted():
    jobs = [{"name": "broken", "status": "SUCCEEDED", "created_at": "not-a-date"}]

    plan = plan_cleanup(jobs, older_than_days=7, keep_latest=0, now=NOW)

    assert plan.job_names == []
    assert any("broken" in error for error in plan.errors)


def test_checkpoints_only_when_requested():
    jobs = [make("old", days_ago=30)]

    without = plan_cleanup(jobs, older_than_days=7, keep_latest=0, include_checkpoints=False, now=NOW)
    with_ckpt = plan_cleanup(jobs, older_than_days=7, keep_latest=0, include_checkpoints=True, now=NOW)

    assert without.checkpoints == []
    assert with_ckpt.checkpoints == ["old"]


def test_handles_z_suffix_and_datetime_objects():
    jobs = [
        {"name": "z", "status": "SUCCEEDED", "created_at": "2026-05-01T00:00:00Z"},
        {"name": "dt", "status": "SUCCEEDED", "finished_at": datetime(2026, 5, 1, tzinfo=timezone.utc)},
    ]

    plan = plan_cleanup(jobs, older_than_days=7, keep_latest=0, now=NOW)

    assert sorted(plan.job_names) == ["dt", "z"]


def test_empty_input():
    plan = plan_cleanup([], older_than_days=7, now=NOW)

    assert plan.as_dict()["total_jobs"] == 0
    assert plan.as_dict()["total_checkpoints"] == 0
