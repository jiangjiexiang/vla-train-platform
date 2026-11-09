"""作业阶段时间轴测试。

时间轴的价值在于「看清卡在哪一步」，所以测试重点是把各阶段的状态推断准确：
没有 Pod 时后面的阶段必须是 pending，不能凭空「已完成」。
"""

from __future__ import annotations

from app.services.timeline import STAGES, build_timeline


def make_job(created: str | None = "2026-01-01T00:00:00Z", conditions=None) -> dict:
    job = {"metadata": {"name": "job1"}, "status": {}}
    if created:
        job["metadata"]["creationTimestamp"] = created
    if conditions is not None:
        job["status"]["conditions"] = conditions
    return job


def make_workload(admitted: bool, created: str = "2026-01-01T00:00:01Z") -> dict:
    return {
        "metadata": {"name": "j1-wl", "creationTimestamp": created},
        "status": {
            "conditions": [
                {
                    "type": "Admitted",
                    "status": "True" if admitted else "False",
                    "lastTransitionTime": "2026-01-01T00:00:02Z",
                }
            ]
        },
    }


def make_pod(running: bool = True) -> dict:
    state = {"running": {"startedAt": "2026-01-01T00:00:10Z"}} if running else {}
    return {
        "metadata": {"name": "job1-master-0", "creationTimestamp": "2026-01-01T00:00:05Z"},
        "status": {"phase": "Running", "container_statuses": [{"name": "pytorch", "state": state}]},
    }


def stage_states(result: dict) -> dict[str, str]:
    return {item["stage"]: item["state"] for item in result["items"]}


def test_all_stages_present():
    result = build_timeline(job=make_job(), workload=None, pod=None, status="QUEUED")

    assert [item["stage"] for item in result["items"]] == [stage for stage, _ in STAGES]


def test_just_submitted_is_current_stage():
    """刚提交、还没进队列：submitted 就是「当前阶段」，不是已完成。"""
    result = build_timeline(job=make_job(), workload=None, pod=None, status="QUEUED")

    states = stage_states(result)
    assert states["submitted"] == "current"
    assert states["queued"] == "pending"
    assert states["running"] == "pending"
    assert result["current_stage"] == "submitted"


def test_queued_but_not_admitted():
    result = build_timeline(job=make_job(), workload=make_workload(False), pod=None, status="QUEUED")

    states = stage_states(result)
    assert states["submitted"] == "done"
    assert states["queued"] == "current"
    assert states["admitted"] == "pending"
    assert result["current_stage"] == "queued"


def test_admitted_without_pod():
    result = build_timeline(job=make_job(), workload=make_workload(True), pod=None, status="QUEUED")

    states = stage_states(result)
    assert states["queued"] == "done"
    assert states["admitted"] == "current"
    # 没有 Pod 就不能认为镜像已拉取
    assert states["image"] == "pending"
    assert states["running"] == "pending"


def test_running_with_pod():
    result = build_timeline(
        job=make_job(
            conditions=[{"type": "Running", "status": "True", "lastTransitionTime": "2026-01-01T00:00:10Z"}]
        ),
        workload=make_workload(True),
        pod=make_pod(),
        status="RUNNING",
    )

    states = stage_states(result)
    assert states["admitted"] == "done"
    assert states["container"] == "done"
    assert states["running"] in ("current", "done")
    assert states["finished"] == "pending"


def test_succeeded_marks_finished_done():
    result = build_timeline(
        job=make_job(
            conditions=[{"type": "Succeeded", "status": "True", "lastTransitionTime": "2026-01-01T01:00:00Z"}]
        ),
        workload=make_workload(True),
        pod=make_pod(),
        status="SUCCEEDED",
    )

    states = stage_states(result)
    assert states["finished"] == "done"


def test_failed_marks_finished_failed():
    result = build_timeline(
        job=make_job(
            conditions=[{"type": "Failed", "status": "True", "lastTransitionTime": "2026-01-01T01:00:00Z"}]
        ),
        workload=make_workload(True),
        pod=make_pod(),
        status="FAILED",
    )

    states = stage_states(result)
    assert states["finished"] == "failed"


def test_current_stage_points_at_admitted_when_no_pod():
    """已准入但 Pod 还没建出来 → 卡在 admitted，不能跳到 image。"""
    result = build_timeline(job=make_job(), workload=make_workload(True), pod=None, status="QUEUED")

    assert result["current_stage"] == "admitted"


def test_timestamps_are_not_fabricated():
    """取不到时间的阶段，at 必须是 None，不能编。"""
    result = build_timeline(job=make_job(), workload=make_workload(True), pod=None, status="QUEUED")

    image = next(item for item in result["items"] if item["stage"] == "image")
    assert image["at"] is None


def test_handles_missing_creation_timestamp():
    result = build_timeline(job=make_job(created=None), workload=None, pod=None, status="QUEUED")

    submitted = next(item for item in result["items"] if item["stage"] == "submitted")
    assert submitted["at"] is None
    assert submitted["state"] == "pending"


def test_handles_datetime_objects():
    """K8s 客户端有时返回 datetime 而不是字符串。"""
    from datetime import datetime, timezone

    job = make_job(created=None)
    job["metadata"]["creationTimestamp"] = datetime(2026, 1, 1, tzinfo=timezone.utc)

    result = build_timeline(job=job, workload=None, pod=None, status="QUEUED")

    submitted = next(item for item in result["items"] if item["stage"] == "submitted")
    assert isinstance(submitted["at"], str)
    assert "2026" in submitted["at"]


def test_empty_objects_do_not_crash():
    result = build_timeline(job={}, workload=None, pod=None, status="QUEUED")

    assert len(result["items"]) == len(STAGES)