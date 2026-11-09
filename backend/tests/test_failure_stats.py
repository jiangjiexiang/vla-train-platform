"""失败原因聚合测试。

运维真正关心的不是「这个任务为什么挂」，而是「最近是不是同一个原因反复挂」。
测试重点：聚合要准、无诊断的不能瞎归类、代表任务要能点进去。
"""

from __future__ import annotations

from app.services.failure_stats import summarize_failures


def test_counts_by_code():
    jobs = [
        {"name": "a", "status": "FAILED"},
        {"name": "b", "status": "FAILED"},
        {"name": "c", "status": "FAILED"},
    ]
    diagnoses = {
        "a": {"code": "shm_exhausted", "summary": "共享内存不足", "suggestion": "调大"},
        "b": {"code": "shm_exhausted", "summary": "共享内存不足", "suggestion": "调大"},
        "c": {"code": "oom", "summary": "显存不足", "suggestion": "降 bs"},
    }

    result = summarize_failures(jobs, diagnoses)

    assert result["total_failed"] == 3
    assert result["classified"] == 3
    # 出现多的排前面
    assert result["reasons"][0]["code"] == "shm_exhausted"
    assert result["reasons"][0]["count"] == 2
    assert result["reasons"][1]["code"] == "oom"


def test_only_failed_jobs_counted():
    """成功的任务不该出现在失败统计里。"""
    jobs = [
        {"name": "ok", "status": "SUCCEEDED"},
        {"name": "bad", "status": "FAILED"},
        {"name": "run", "status": "RUNNING"},
    ]
    diagnoses = {
        "ok": {"code": "some_code"},
        "bad": {"code": "oom"},
        "run": {"code": "another"},
    }

    result = summarize_failures(jobs, diagnoses)

    assert result["total_failed"] == 1
    assert [r["code"] for r in result["reasons"]] == ["oom"]


def test_missing_diagnosis_becomes_unknown():
    """没有诊断结论的不能瞎归类，必须单列出来。"""
    jobs = [
        {"name": "a", "status": "FAILED"},
        {"name": "b", "status": "FAILED"},
    ]
    diagnoses = {"a": {"code": "oom"}, "b": None}

    result = summarize_failures(jobs, diagnoses)

    assert result["total_failed"] == 2
    assert result["classified"] == 1
    assert result["unknown"] == 1
    assert [r["code"] for r in result["reasons"]] == ["oom"]


def test_example_job_points_to_real_task():
    """数字本身没法排查，必须能跳到具体任务看日志。"""
    jobs = [{"name": "task-123", "status": "FAILED"}]
    diagnoses = {"task-123": {"code": "shm_exhausted", "summary": "共享内存不足"}}

    result = summarize_failures(jobs, diagnoses)

    assert result["reasons"][0]["example_job"] == "task-123"


def test_first_occurrence_wins_as_example():
    """列表是倒序的，先出现的更新，用它当代表。"""
    jobs = [
        {"name": "newest", "status": "FAILED"},
        {"name": "older", "status": "FAILED"},
    ]
    diagnoses = {
        "newest": {"code": "oom"},
        "older": {"code": "oom"},
    }

    result = summarize_failures(jobs, diagnoses)

    assert result["reasons"][0]["example_job"] == "newest"


def test_respects_limit():
    jobs = [{"name": f"j{i}", "status": "FAILED"} for i in range(10)]
    diagnoses = {f"j{i}": {"code": f"code_{i}"} for i in range(10)}

    result = summarize_failures(jobs, diagnoses, limit=3)

    assert len(result["reasons"]) == 3


def test_empty_input():
    result = summarize_failures([], {})

    assert result["total_failed"] == 0
    assert result["reasons"] == []
    assert result["unknown"] == 0
