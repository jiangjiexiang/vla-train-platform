"""任务终态通知测试。

这个功能的失败模式全部是"静默"的：webhook 挂了没人发现、同一个任务
通知了三次、用户取消也刷屏、进程重启把历史任务重发一遍。所以这里重点
不是"消息能发出去"，而是这几种静默失败都不会发生。

不打真实请求：Notifier 支持注入 opener，测试里替换掉。
"""

from __future__ import annotations

import json
import threading
import urllib.error

import pytest
from app.config import Settings
from app.services.job_watcher import JobNotificationStore, JobWatcher, start_job_watcher
from app.services.notifier import Notifier

JOB = {
    "name": "vla-run-001",
    "status": "SUCCEEDED",
    "owner": "alice",
    "image": "vla-smolvla:latest",
    "queue": "default",
    "gpus": 1,
    "created_at": "2026-01-01T01:00:00Z",
    "started_at": "2026-01-01T01:01:00Z",
    "finished_at": "2026-01-01T02:30:30Z",
    "duration_seconds": 5370,
    "message": "PyTorchJob vla-run-001 is successfully completed.",
    "checkpoint_dir": "/checkpoints/vla-run-001",
}


class FakeResponse:
    """最小化的 urlopen 返回值，支持 with 与 read()。"""

    def __init__(self, body: bytes = b'{"errcode": 0, "errmsg": "ok"}', status: int = 200) -> None:
        self.body = body
        self.status = status

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc) -> None:
        return None


class Recorder:
    """记录每次发送的 (url, payload)，并按顺序返回预设结果。"""

    def __init__(self, *results) -> None:
        self.calls: list[tuple[str, dict, object]] = []
        self.timeouts: list[float] = []
        self.results = list(results)
        self.index = 0

    def __call__(self, request, timeout=None):
        self.timeouts.append(timeout)
        payload = json.loads(request.data.decode("utf-8"))
        self.calls.append((request.full_url, payload, request))
        result = self.results[min(self.index, len(self.results) - 1)] if self.results else FakeResponse()
        self.index += 1
        if isinstance(result, BaseException):
            raise result
        return result


def make_settings(**overrides) -> Settings:
    base = {"api_token": "t", "auth_required": False, "db_path": ":memory:"}
    base.update(overrides)
    return Settings(**base)


# ----------------------------------------------------------------------
# 1. 三种渠道的 payload 格式
# ----------------------------------------------------------------------
def test_dingtalk_payload_shape(tmp_path):
    settings = make_settings(dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=x")
    opener = Recorder()
    notifier = Notifier(settings, opener=opener)

    results = notifier.notify_job(JOB, "SUCCEEDED")

    assert [r["ok"] for r in results] == [True]
    url, payload, request = opener.calls[0]
    assert "oapi.dingtalk.com" in url
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"]["title"] == "训练任务成功：vla-run-001"
    text = payload["markdown"]["text"]
    assert text.startswith("### 训练任务成功：vla-run-001")
    assert "alice" in text
    assert "1 小时 29 分 30 秒" in text
    assert request.get_header("Content-type") == "application/json; charset=utf-8"


def test_wecom_payload_shape(tmp_path):
    settings = make_settings(wecom_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k")
    opener = Recorder()
    notifier = Notifier(settings, opener=opener)

    notifier.notify_job(JOB, "FAILED")

    _, payload, _ = opener.calls[0]
    assert payload["msgtype"] == "markdown"
    # 企微的 markdown 只接受 content；带 title 会被拒
    assert set(payload["markdown"]) == {"content"}
    assert payload["markdown"]["content"].startswith("### 训练任务失败：vla-run-001")


def test_generic_payload_is_structured_json(tmp_path):
    settings = make_settings(generic_webhook="https://hooks.example.com/vla")
    opener = Recorder()
    notifier = Notifier(settings, opener=opener)

    notifier.notify_job(JOB, "FAILED")

    _, payload, _ = opener.calls[0]
    assert payload["event"] == "FAILED"
    assert payload["job"]["name"] == "vla-run-001"
    assert payload["job"]["status"] == "FAILED"
    assert payload["job"]["owner"] == "alice"
    assert payload["job"]["duration_seconds"] == 5370
    # text 是去掉 markdown 标记的纯文本，接收端可直接转发
    assert "**" not in payload["text"]
    assert "训练任务失败：vla-run-001" in payload["text"]


def test_all_three_channels_send_once_each():
    settings = make_settings(
        dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=x",
        wecom_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k",
        generic_webhook="https://hooks.example.com/vla",
    )
    opener = Recorder()
    notifier = Notifier(settings, opener=opener)

    results = notifier.notify_job(JOB, "SUCCEEDED")

    assert len(opener.calls) == 3
    assert [r["channel"] for r in results] == ["dingtalk", "wecom", "generic"]
    assert all(r["ok"] for r in results)


# ----------------------------------------------------------------------
# 2. 未配置时不发送
# ----------------------------------------------------------------------
def test_no_webhook_configured_sends_nothing():
    opener = Recorder()
    notifier = Notifier(make_settings(), opener=opener)

    assert notifier.enabled is False
    assert notifier.notify_job(JOB, "SUCCEEDED") == []
    assert opener.calls == []


def test_start_watcher_is_noop_without_webhooks(tmp_path):
    """没配 webhook 时不能起线程、不能建表。"""

    class FakeApp:
        state = type("S", (), {})()

    class ExplodingService:
        @property
        def kubernetes(self):
            raise AssertionError("没有配置 webhook 时不应触碰 K8s")

    db = tmp_path / "platform.db"
    settings = make_settings(db_path=str(db))
    app = FakeApp()

    assert start_job_watcher(app, ExplodingService(), Notifier(settings), settings) is None
    assert not hasattr(app.state, "notify_thread")
    assert not hasattr(app.state, "notify_stop")
    assert not db.exists()


# ----------------------------------------------------------------------
# 3. 发送失败不抛异常
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "failure, needle",
    [
        (urllib.error.URLError("connection refused"), "connection refused"),
        (TimeoutError("timed out"), "timed out"),
        (OSError("network unreachable"), "network unreachable"),
    ],
)
def test_transport_error_is_swallowed(failure, needle):
    settings = make_settings(generic_webhook="https://hooks.example.com/vla")
    notifier = Notifier(settings, opener=Recorder(failure))

    results = notifier.notify_job(JOB, "FAILED")

    assert results[0]["ok"] is False
    assert needle in results[0]["detail"]


def test_http_error_is_reported_not_raised():
    settings = make_settings(generic_webhook="https://hooks.example.com/vla")

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 500, "Internal Server Error", {}, None)

    results = Notifier(settings, opener=opener).notify_job(JOB, "FAILED")

    assert results[0]["ok"] is False
    assert "HTTP 500" in results[0]["detail"]


def test_business_errcode_on_http_200_counts_as_failure():
    """钉钉/企微用 200 + errcode 报错，不识别会误判为已送达。"""
    settings = make_settings(dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=x")
    body = json.dumps({"errcode": 310000, "errmsg": "keywords not in content"}).encode()
    results = Notifier(settings, opener=Recorder(FakeResponse(body))).notify_job(JOB, "FAILED")

    assert results[0]["ok"] is False
    assert "310000" in results[0]["detail"]


def test_default_timeout_is_applied():
    settings = make_settings(generic_webhook="https://hooks.example.com/vla", notify_timeout_seconds=5.0)
    opener = Recorder()
    Notifier(settings, opener=opener).notify_job(JOB, "SUCCEEDED")

    # 不设超时的 webhook 会把整轮轮询挂死
    assert opener.timeouts == [5.0]


# ----------------------------------------------------------------------
# 4. 成功/失败触发，取消不触发
# ----------------------------------------------------------------------
def test_succeeded_and_failed_are_sent_cancelled_is_not():
    settings = make_settings(generic_webhook="https://hooks.example.com/vla")
    opener = Recorder()
    notifier = Notifier(settings, opener=opener)

    notifier.notify_job(JOB, "RUNNING")
    notifier.notify_job(JOB, "SUCCEEDED")
    notifier.notify_job(JOB, "FAILED")
    assert notifier.notify_job(JOB, "CANCELLED") == []
    assert notifier.notify_job(JOB, "QUEUED") == []

    assert len(opener.calls) == 3
    assert [payload["event"] for _, payload, _ in opener.calls] == ["RUNNING", "SUCCEEDED", "FAILED"]


def test_dingtalk_signing_appends_timestamp_and_sign():
    settings = make_settings(
        dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=abc",
        dingtalk_secret="SECtestsecret",
    )
    opener = Recorder()
    notifier = Notifier(settings, opener=opener)
    results = notifier.notify_job(JOB, "RUNNING")
    assert results and results[0]["ok"] is True
    called_url = opener.calls[0][0]
    assert "&timestamp=" in called_url and "&sign=" in called_url


# ----------------------------------------------------------------------
# 5. 轮询：不重复通知
# ----------------------------------------------------------------------
class FakeKubernetes:
    def __init__(self, jobs: list[dict], pods_by_job: dict | None = None) -> None:
        self.jobs = jobs
        self.pods = pods_by_job or {}
        self.list_calls = 0
        self.fail = False

    def list_jobs(self):
        self.list_calls += 1
        if self.fail:
            raise RuntimeError("kube api down")
        return self.jobs

    def list_job_pods(self, name):
        return self.pods.get(name, [])

    def list_workloads(self):
        return []


class FakeService:
    def __init__(self, kube: FakeKubernetes) -> None:
        self.kubernetes = kube

    def describe_job(self, job):
        name = job["metadata"]["name"]
        return {
            "name": name,
            "status": "SUCCEEDED",
            "owner": job["metadata"].get("annotations", {}).get("owner", ""),
            "image": "img",
            "queue": "default",
            "gpus": 1,
            "duration_seconds": 60,
            "message": "",
            "checkpoint_dir": f"/checkpoints/{name}",
        }


class RecordingNotifier:
    def __init__(self, ok: bool = True) -> None:
        self.sent: list[tuple[str, str]] = []
        self.ok = ok

    def notify_job(self, job, event):
        self.sent.append((job["name"], event))
        return [{"channel": "generic", "ok": self.ok, "detail": ""}]


def _pytorchjob(name: str, cond_type: str | None = None) -> dict:
    conditions = []
    if cond_type:
        conditions.append({"type": cond_type, "status": "True", "lastTransitionTime": "2026-01-01T02:00:00Z"})
    return {"metadata": {"name": name}, "status": {"conditions": conditions}}


def _watcher(tmp_path, kube, notifier, *, primed: bool = True) -> JobWatcher:
    store = JobNotificationStore(str(tmp_path / "platform.db"))
    watcher = JobWatcher(FakeService(kube), notifier, store, interval=0.01)
    watcher._primed = primed
    return watcher


def test_watcher_notifies_terminal_job_once(tmp_path):
    kube = FakeKubernetes([_pytorchjob("job-a", "Succeeded")])
    notifier = RecordingNotifier()
    watcher = _watcher(tmp_path, kube, notifier)

    assert watcher.scan_once()["notified"] == 1
    assert watcher.scan_once()["notified"] == 0
    assert notifier.sent == [("job-a", "SUCCEEDED")]


def test_watcher_ignores_running_and_cancelled(tmp_path):
    kube = FakeKubernetes([_pytorchjob("running"), _pytorchjob("cancel", "JobSuspended")])
    notifier = RecordingNotifier()
    _watcher(tmp_path, kube, notifier).scan_once()

    assert notifier.sent == []


def test_watcher_does_not_renotify_after_restart(tmp_path):
    kube = FakeKubernetes([_pytorchjob("job-a", "Succeeded")])
    first = RecordingNotifier()
    _watcher(tmp_path, kube, first).scan_once()
    assert first.sent

    # 模拟进程重启：新的 store，同一个数据库
    second = RecordingNotifier()
    _watcher(tmp_path, kube, second).scan_once()
    assert second.sent == []


def test_watcher_does_not_backfill_jobs_already_done_at_startup(tmp_path):
    kube = FakeKubernetes([_pytorchjob("old-job", "Succeeded")])
    notifier = RecordingNotifier()
    watcher = _watcher(tmp_path, kube, notifier, primed=False)

    watcher.scan_once()  # 第一轮只建快照
    watch = _pytorchjob("new-job", "Failed")
    kube.jobs.append(watch)
    watcher.scan_once()

    assert notifier.sent == [("new-job", "FAILED")]


def test_watcher_retries_when_every_channel_fails(tmp_path):
    kube = FakeKubernetes([_pytorchjob("job-a", "Failed")])
    notifier = RecordingNotifier(ok=False)
    watcher = _watcher(tmp_path, kube, notifier)

    assert watcher.scan_once()["failed"] == 1
    notifier.ok = True
    assert watcher.scan_once()["notified"] == 1
    assert len(notifier.sent) == 2


def test_watcher_survives_kubernetes_errors(tmp_path):
    kube = FakeKubernetes([_pytorchjob("job-a", "Succeeded")])
    kube.fail = True
    notifier = RecordingNotifier()
    watcher = _watcher(tmp_path, kube, notifier)

    assert watcher.scan_once()["failed"] == 1
    kube.fail = False
    assert watcher.scan_once()["notified"] == 1


def test_watcher_prunes_records_for_deleted_jobs(tmp_path):
    kube = FakeKubernetes([_pytorchjob("job-a", "Succeeded")])
    watcher = _watcher(tmp_path, kube, RecordingNotifier())
    watcher.scan_once()
    assert watcher.store.has("job-a")

    kube.jobs = []  # K8s 对象被 ttlSecondsAfterFinished 回收
    watcher.scan_once()
    assert not watcher.store.has("job-a")


# ----------------------------------------------------------------------
# 6. 线程本身
# ----------------------------------------------------------------------
def test_start_watcher_starts_thread_and_stops_cleanly(tmp_path):
    class FakeApp:
        state = type("S", (), {})()

    settings = make_settings(
        db_path=str(tmp_path / "platform.db"),
        generic_webhook="https://hooks.example.com/vla",
        notify_interval_seconds=0.05,
    )
    kube = FakeKubernetes([_pytorchjob("job-a", "Succeeded")])
    notifier = RecordingNotifier()
    app = FakeApp()

    watcher = start_job_watcher(app, FakeService(kube), notifier, settings)
    assert watcher is not None
    assert app.state.notify_thread.daemon is True
    assert app.state.notify_thread.is_alive()

    app.state.notify_stop.set()
    app.state.notify_thread.join(timeout=5)
    assert not app.state.notify_thread.is_alive()


def test_notifier_never_raises_even_for_broken_channel():
    """任何渠道内部抛出非预期异常，都不能冒到轮询线程里。"""
    settings = make_settings(dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=x")

    class Bad:
        def __call__(self, *a, **kw):
            raise ValueError("boom")

    notifier = Notifier(settings)
    notifier._send_dingtalk = Bad()  # type: ignore[method-assign]

    results = notifier.notify_job(JOB, "FAILED")
    assert results[0]["ok"] is False and "boom" in results[0]["detail"]


def test_invalid_webhook_url_is_reported():
    settings = make_settings(generic_webhook="not-a-url")
    results = Notifier(settings, opener=Recorder()).notify_job(JOB, "SUCCEEDED")
    assert results[0]["ok"] is False


def test_settings_parses_webhook_env(monkeypatch):
    monkeypatch.setenv("PLATFORM_AUTH_REQUIRED", "false")
    monkeypatch.setenv("PLATFORM_DINGTALK_WEBHOOK", " https://oapi.dingtalk.com/robot/send?access_token=x ")
    monkeypatch.setenv("PLATFORM_NOTIFY_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("PLATFORM_NOTIFY_TIMEOUT_SECONDS", "3")

    settings = Settings.from_env()

    assert settings.dingtalk_webhook == "https://oapi.dingtalk.com/robot/send?access_token=x"
    assert settings.notify_enabled is True
    assert settings.notify_interval_seconds == 15
    assert settings.notify_timeout_seconds == 3


def test_settings_notify_disabled_by_default(monkeypatch):
    monkeypatch.setenv("PLATFORM_AUTH_REQUIRED", "false")
    for name in ("PLATFORM_DINGTALK_WEBHOOK", "PLATFORM_WECOM_WEBHOOK", "PLATFORM_GENERIC_WEBHOOK"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.notify_enabled is False
    assert settings.notify_interval_seconds == 30


def test_cooperative_stop_prevents_first_scan():
    """停止了就不该再扫——避免关停时还在发通知。"""
    sent = []
    stop = threading.Event()
    stop.set()

    class Service:
        class kubernetes:  # noqa: N801
            @staticmethod
            def list_jobs():
                sent.append("listed")
                return []

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = JobNotificationStore(str(f"{tmp}/p.db"))
        JobWatcher(Service(), RecordingNotifier(), store, interval=0.01).run(stop)

    assert sent == []