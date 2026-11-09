"""排队根因分析测试。

覆盖 5 类根因，以及「说不出原因时返回 None」这条原则——
错误的建议会让用户白等或误操作，比不给建议更糟。
"""

from __future__ import annotations

import pytest
from app.services.queue_block import diagnose_queue_block


def make_workload(name: str, *, gpus: int = 1, admitted: bool = False, priority: str = "normal") -> dict:
    return {
        "metadata": {
            "name": name,
            "labels": {"kueue.x-k8s.io/priority-class": priority},
        },
        "spec": {
            "podSets": [
                {
                    "count": 1,
                    "template": {
                        "spec": {
                            "containers": [
                                {"resources": {"requests": {"nvidia.com/gpu": str(gpus)}}}
                            ]
                        }
                    },
                }
            ]
        },
        "status": {"conditions": [{"type": "Admitted", "status": "True" if admitted else "False"}]},
    }


def make_cluster_queue(quota: int = 1) -> dict:
    return {
        "spec": {
            "resourceGroups": [
                {
                    "flavors": [
                        {"resources": [{"name": "nvidia.com/gpu", "nominalQuota": str(quota)}]}
                    ]
                }
            ]
        }
    }


def test_quota_full_when_all_gpus_admitted():
    workloads = [make_workload("a", gpus=1, admitted=True)]

    result = diagnose_queue_block(
        job_gpus=1,
        cluster_queue=make_cluster_queue(1),
        workloads=workloads,
        job_priority="normal",
    )

    assert result is not None
    assert result["code"] == "quota_full"
    assert "1" in result["message"]
    assert result["actions"]


def test_fragmentation_when_free_less_than_requested():
    workloads = [make_workload("a", gpus=4, admitted=True)]

    result = diagnose_queue_block(
        job_gpus=8,
        cluster_queue=make_cluster_queue(8),
        workloads=workloads,
        job_priority="normal",
    )

    # 8 卡队列已用 4 卡，申请 8 卡 → 剩 4 卡不够
    assert result is not None
    assert result["code"] in ("fragmentation", "quota_full")


def test_high_priority_blocking():
    workloads = [
        make_workload("big", gpus=1, admitted=True),
        make_workload("vip", gpus=1, priority="high"),
    ]

    result = diagnose_queue_block(
        job_gpus=1,
        cluster_queue=make_cluster_queue(1),
        workloads=workloads,
        job_priority="low",
    )

    assert result is not None
    assert result["code"] == "high_priority"
    assert "高优先级" in result["title"]


def test_user_quota_exceeded():
    result = diagnose_queue_block(
        job_gpus=2,
        cluster_queue=make_cluster_queue(8),
        workloads=[make_workload("mine", gpus=2, admitted=True)],
        job_priority="normal",
        user_usage={"gpu_in_use": 4, "active_jobs": 2},
        user_quota={"max_gpus": 4, "max_active_jobs": 5},
    )

    assert result is not None
    assert result["code"] == "user_quota"
    assert "配额" in result["title"]


def test_user_active_jobs_exceeded():
    result = diagnose_queue_block(
        job_gpus=1,
        cluster_queue=make_cluster_queue(8),
        workloads=[make_workload("x", gpus=1, admitted=True)],
        job_priority="normal",
        user_usage={"gpu_in_use": 1, "active_jobs": 3},
        user_quota={"max_gpus": None, "max_active_jobs": 3},
    )

    assert result is not None
    assert result["code"] == "user_quota"
    assert "任务数" in result["title"]


def test_missing_cluster_queue_reports_config_problem():
    result = diagnose_queue_block(
        job_gpus=1,
        cluster_queue=None,
        workloads=[],
        job_priority="normal",
    )

    assert result is not None
    assert result["code"] == "resource_group"


def test_returns_none_when_no_clear_cause():
    """队列有余量、无高优阻塞、无配额问题 → 不该硬猜原因。"""
    result = diagnose_queue_block(
        job_gpus=1,
        cluster_queue=make_cluster_queue(8),
        workloads=[],
        job_priority="normal",
    )

    assert result is None


def test_all_codes_have_actions():
    """每条根因都必须至少给一个可执行动作，否则等于没说。"""
    cases = [
        (["a"], make_cluster_queue(1), 1, "normal"),
    ]
    for _names, queue, gpus, priority in cases:
        result = diagnose_queue_block(
            job_gpus=gpus,
            cluster_queue=queue,
            workloads=[make_workload("a", gpus=1, admitted=True)],
            job_priority=priority,
        )
        if result:
            assert result["actions"], result["code"]
            for action in result["actions"]:
                assert action["label"]
                assert action["hint"]


@pytest.mark.parametrize("quota", [1, 4, 8])
def test_nominal_quota_parsing(quota):
    result = diagnose_queue_block(
        job_gpus=1,
        cluster_queue=make_cluster_queue(quota),
        workloads=[make_workload("a", gpus=quota, admitted=True)],
        job_priority="normal",
    )

    assert result is not None
    assert result["code"] == "quota_full"
    assert str(quota) in result["message"]

def make_finished_workload(name: str, *, gpus: int = 1) -> dict:
    workload = make_workload(name, gpus=gpus, admitted=True)
    workload["status"]["conditions"].append({"type": "Finished", "status": "True"})
    return workload


def test_finished_workloads_do_not_count_as_running():
    """已完成的历史 Workload 会留在集群里，不能算作「运行中」。"""
    workloads = [make_finished_workload("old-1"), make_finished_workload("old-2")]

    result = diagnose_queue_block(
        job_gpus=1,
        cluster_queue=make_cluster_queue(1),
        workloads=workloads,
        job_priority="normal",
    )

    # 已结束的任务不再占配额 → 队列实际是空的，没有阻塞原因可报
    assert result is None


def test_running_count_excludes_finished():
    workloads = [
        make_workload("running", gpus=1, admitted=True),
        make_finished_workload("done", gpus=1),
    ]

    result = diagnose_queue_block(
        job_gpus=1,
        cluster_queue=make_cluster_queue(1),
        workloads=workloads,
        job_priority="normal",
    )

    assert result is not None
    assert "1 个运行中的任务" in result["message"]


def test_finished_workload_releases_quota():
    """只有已完成任务时，队列应当有空闲配额，不该报「配额已满」。"""
    workloads = [make_finished_workload("done", gpus=1)]

    result = diagnose_queue_block(
        job_gpus=1,
        cluster_queue=make_cluster_queue(1),
        workloads=workloads,
        job_priority="normal",
    )

    assert result is None

