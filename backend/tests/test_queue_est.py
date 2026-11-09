from __future__ import annotations


# ---------------------------------------------------------------------------
# 排队位置：Kueue 的 Workload 名是 "pytorchjob-<任务名>-<后缀>"，不是任务名本身。
# 早先拿 workload 名去查 key 是 workload 名的字典，而调用方传的是任务名，
# 于是 queue_position 恒为 None——用户排队时看不到自己排第几，只能干等。
# ---------------------------------------------------------------------------
def _pending_workload(name: str, created: str) -> dict:
    return {
        "metadata": {"name": name, "creationTimestamp": created},
        "status": {"conditions": [{"type": "Pending", "status": "True"}]},
    }


def test_queue_position_uses_workload_name_not_job_name():
    from app.services.queue_est import queue_info

    first = _pending_workload("pytorchjob-job-a-7ad95", "2026-01-01T00:00:00Z")
    second = _pending_workload("pytorchjob-job-b-e0667", "2026-01-01T00:01:00Z")
    workloads = [first, second]

    info_first = queue_info(
        workload=first, workloads=workloads, status="QUEUED",
        ahead=[], durations={}, recipe=None, elapsed_seconds=None,
    )
    info_second = queue_info(
        workload=second, workloads=workloads, status="QUEUED",
        ahead=[], durations={}, recipe=None, elapsed_seconds=None,
    )

    assert info_first["queue_position"] == 1
    assert info_second["queue_position"] == 2


def test_queue_position_none_when_not_queued():
    from app.services.queue_est import queue_info

    workload = _pending_workload("pytorchjob-job-a-7ad95", "2026-01-01T00:00:00Z")

    info = queue_info(
        workload=workload, workloads=[workload], status="RUNNING",
        ahead=[], durations={}, recipe=None, elapsed_seconds=None,
    )

    assert info["queue_position"] is None


def test_queue_position_none_without_workload():
    """任务还没被 Kueue 建出 Workload 时不能报错。"""
    from app.services.queue_est import queue_info

    info = queue_info(
        workload=None, workloads=[], status="QUEUED",
        ahead=[], durations={}, recipe=None, elapsed_seconds=None,
    )

    assert info["queue_position"] is None
