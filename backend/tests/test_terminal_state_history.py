"""任务跑完后对象被 ttl 回收，平台要能说出它到底是成功还是失败。

PyTorchJob 带 ttlSecondsAfterFinished，跑完一小时后对象被彻底删掉。
之前平台只存了「提交了什么」，没存「最后怎么样了」——对象一没，
任务详情就变成 GONE，用户看不出成功还是失败。

现在 get_job 读到终态时顺手落库，_vanished_response 优先用它。
但有一条底线：真不知道的时候仍然要说 GONE，不能编一个终态出来。
"""

from __future__ import annotations

from test_platform import add_pod, make_spec


def test_succeeded_survives_object_collection(service, kube):
    """读到 SUCCEEDED 后对象消失，状态仍是 SUCCEEDED。"""
    service.submit(make_spec())
    add_pod(kube, "exp001", phase="Succeeded")
    kube.jobs["exp001"]["status"] = {
        "conditions": [{"type": "Succeeded", "status": "True", "lastTransitionTime": "2026-11-10T10:00:00Z"}]
    }

    first = service.get_job("exp001")
    assert first["status"] == "SUCCEEDED"

    # 模拟 ttl 回收
    del kube.jobs["exp001"]
    kube.pods.clear()

    again = service.get_job("exp001")
    assert again["status"] == "SUCCEEDED", "对象被回收后不该退回未知"


def test_failed_survives_object_collection(service, kube):
    """读到 FAILED 后对象消失，状态仍是 FAILED。"""
    service.submit(make_spec())
    add_pod(kube, "exp001", phase="Failed")
    kube.jobs["exp001"]["status"] = {
        "conditions": [
            {"type": "Failed", "status": "True", "lastTransitionTime": "2026-11-10T10:00:00Z",
             "message": "pod failed"}
        ]
    }

    assert service.get_job("exp001")["status"] == "FAILED"

    del kube.jobs["exp001"]
    kube.pods.clear()

    again = service.get_job("exp001")
    assert again["status"] == "FAILED"


def test_unknown_outcome_still_says_gone(service, kube):
    """从没读到过终态 = 真的不知道，必须说 GONE，不能编。"""
    service.submit(make_spec())
    add_pod(kube, "exp001", phase="Running")
    assert service.get_job("exp001")["status"] == "RUNNING"

    # 对象被手工删掉，平台从没看到它结束
    del kube.jobs["exp001"]
    kube.pods.clear()

    gone = service.get_job("exp001")
    assert gone["status"] == "GONE", "不知道就是不知道，不能猜成 SUCCEEDED/FAILED"


def test_running_does_not_get_recorded(service, kube):
    """RUNNING 不该被当成终态记下来。"""
    service.submit(make_spec())
    add_pod(kube, "exp001", phase="Running")
    service.get_job("exp001")

    del kube.jobs["exp001"]
    kube.pods.clear()

    assert service.get_job("exp001")["status"] == "GONE"
