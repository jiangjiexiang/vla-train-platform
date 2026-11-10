"""TensorBoard events 读取测试（services/tensorboard.py + GET /jobs/{name}/tensorboard）。

用例分两层：
- 服务层直接读 events 目录：正常 / 缺失 / 空目录 / 截断 / 垃圾文件 / 抽稀 / 多文件；
- API 层验证鉴权与归属：非管理员只能看自己的任务，别人的任务返回 404 而不是 403。
"""

from __future__ import annotations

import time

import pytest
from app.config import Settings
from app.models import SubmitSpec
from app.services import tensorboard as tb

pytest.importorskip("tensorboard")

from tensorboard.compat.proto.event_pb2 import Event  # noqa: E402
from tensorboard.compat.proto.summary_pb2 import Summary  # noqa: E402
from tensorboard.summary.writer.event_file_writer import EventFileWriter  # noqa: E402

WALL = 1_760_000_000.0


@pytest.fixture
def root(tmp_path):
    return tmp_path / "checkpoints"


def write_events(root, job="job1", *, points=None, tag="loss", tags=None):
    """写一个 events 文件。points 是 [(step, value)]；tags 额外写入别的 tag。"""
    directory = tb.tensorboard_dir(root, job)
    directory.mkdir(parents=True, exist_ok=True)
    writer = EventFileWriter(str(directory))
    for step, value in points or []:
        writer.add_event(
            Event(
                wall_time=WALL + step,
                step=step,
                summary=Summary(value=[Summary.Value(tag=tag, simple_value=value)]),
            )
        )
    for extra_tag, extra_points in (tags or {}).items():
        for step, value in extra_points:
            writer.add_event(
                Event(
                    wall_time=WALL + step,
                    step=step,
                    summary=Summary(value=[Summary.Value(tag=extra_tag, simple_value=value)]),
                )
            )
    writer.close()
    return tb.events_files(directory)[-1]


# --- 服务层 -----------------------------------------------------------------


def test_reads_scalars_in_step_order(root):
    write_events(root, points=[(2, 0.5), (1, 0.9), (3, 0.25)])

    result = tb.read_scalars(root, "job1")

    assert result["path_exists"] is True
    assert result["tags"] == ["loss"]
    series = result["series"]["loss"]
    assert [point["step"] for point in series] == [1, 2, 3]
    assert series[1]["value"] == pytest.approx(0.5)
    # float32 存储后取近似
    assert series[0]["value"] == pytest.approx(0.9, rel=1e-6)
    assert series[0]["wall_time"] == pytest.approx(WALL + 1, rel=1e-6)
    assert result["total_points"] == 3
    assert result["truncated"] is False


def test_missing_directory_returns_path_exists_false(root):
    result = tb.read_scalars(root, "nope")

    assert result["path_exists"] is False
    assert result["tags"] == []
    assert result["series"] == {}
    assert result["files"] == []
    assert result["name"] == "nope"


def test_empty_directory_returns_path_exists_false(root):
    tb.tensorboard_dir(root, "job1").mkdir(parents=True)

    result = tb.read_scalars(root, "job1")

    assert result["path_exists"] is False
    assert result["series"] == {}


def test_only_whitelisted_tags_are_read(root):
    write_events(
        root,
        points=[(1, 0.9)],
        tags={"eval/acc": [(1, 0.1)], "extra/tag_17": [(1, 2.0)]},
    )

    result = tb.read_scalars(root, "job1")

    assert result["tags"] == ["loss"]
    assert set(result["series"]) == {"loss"}

    only_eval = tb.read_scalars(root, "job1", tags=["eval/acc"])
    assert only_eval["tags"] == ["eval/acc"]
    assert only_eval["series"]["eval/acc"][0]["value"] == pytest.approx(0.1, rel=1e-6)


def test_reads_all_hook_keys(root):
    """训练侧 metrics_hook 写的是裸键（loss / grad_norm / lr / update_s），一次全返回。"""
    directory = tb.tensorboard_dir(root, "job1")
    directory.mkdir(parents=True)
    writer = EventFileWriter(str(directory))
    for step in range(3):
        for key in ("loss", "grad_norm", "lr", "update_s"):
            writer.add_event(
                Event(
                    wall_time=WALL + step,
                    step=step,
                    summary=Summary(value=[Summary.Value(tag=key, simple_value=float(step) + 0.5)]),
                )
            )
    writer.close()

    result = tb.read_scalars(root, "job1")

    assert result["tags"] == ["loss", "grad_norm", "lr", "update_s"]
    for tag in result["tags"]:
        assert [point["step"] for point in result["series"][tag]] == [0, 1, 2]
    assert result["total_points"] == 12


def test_torn_tail_keeps_complete_records(root):
    """模拟「训练中正在写」：文件尾部多出半条记录，已 flush 的点必须照常返回。"""
    path = write_events(root, points=[(1, 0.9), (2, 0.8)])
    payload = path.read_bytes()
    # 尾部追加一段不完整的 record（只有长度前缀和半个 protobuf）
    with path.open("ab") as handle:
        handle.write(payload[-25:])

    result = tb.read_scalars(root, "job1")

    assert [point["step"] for point in result["series"]["loss"]] == [1, 2]


def test_garbage_file_yields_empty_series_not_error(root):
    directory = tb.tensorboard_dir(root, "job1")
    directory.mkdir(parents=True)
    (directory / "events.out.tfevents.1.host.0").write_bytes(b"\x00\xff" * 128)

    result = tb.read_scalars(root, "job1")

    # 有 events 文件但没有可读记录：返回空曲线，不抛异常
    assert result["path_exists"] is True
    assert result["tags"] == []
    assert result["series"] == {}


def test_unrelated_files_are_ignored(root):
    directory = tb.tensorboard_dir(root, "job1")
    directory.mkdir(parents=True)
    (directory / "notes.txt").write_text("hello", encoding="utf-8")

    result = tb.read_scalars(root, "job1")

    assert result["path_exists"] is False


def test_multiple_event_files_are_merged(root):
    write_events(root, points=[(1, 0.9)])
    # 第二次启动另起一个 events 文件（mtime 必须更晚，排序按 mtime）
    time.sleep(0.01)
    write_events(root, points=[(2, 0.7)])

    result = tb.read_scalars(root, "job1")

    assert len(result["files"]) == 2
    # 两个文件里都有 step=2 之外的点，合并后按 step 升序
    steps = [point["step"] for point in result["series"]["loss"]]
    assert steps == sorted(steps)
    assert 2 in steps


def test_limit_downsamples_and_keeps_endpoints(root):
    write_events(root, points=[(step, float(step)) for step in range(500)])

    result = tb.read_scalars(root, "job1", limit=10)

    series = result["series"]["loss"]
    assert len(series) == 10
    assert result["truncated"] is True
    # 抽稀保留首尾，而不是只留最后 10 个
    assert series[0]["step"] == 0
    assert series[-1]["step"] == 499
    assert [point["step"] for point in series] == sorted(point["step"] for point in series)


def test_live_writer_partial_flush_is_readable(root):
    """边写边读：writer 未 close 时，已 flush 的记录能读到，且不抛异常。"""
    directory = tb.tensorboard_dir(root, "job1")
    directory.mkdir(parents=True)
    writer = EventFileWriter(str(directory), flush_secs=1)
    try:
        for step in range(5):
            writer.add_event(
                Event(
                    wall_time=WALL + step,
                    step=step,
                    summary=Summary(value=[Summary.Value(tag="loss", simple_value=1.0 / (step + 1))]),
                )
            )
        writer.flush()
        result = tb.read_scalars(root, "job1")
    finally:
        writer.close()

    assert result["path_exists"] is True
    assert len(result["series"]["loss"]) == 5


# --- API 层 -----------------------------------------------------------------


def _settings(tmp_path, checkpoint_dir, **overrides) -> Settings:
    values = {
        "api_token": "test-token",
        "db_path": str(tmp_path / "tb.db"),
        "gpu_metrics_url": "",
        "checkpoint_host_path": str(checkpoint_dir),
    }
    values.update(overrides)
    return Settings(**values)


def _submit(service, name, owner="token"):
    """直接建任务：本文件只验证读取接口，提交链路由 test_api / test_auth 覆盖。"""
    spec = SubmitSpec(
        name=name,
        image="vla-smoke:latest",
        gpus=1,
        queue="default",
        priority="normal",
        command="python /app/train.py",
        env={},
        max_retries=0,
    )
    return service.submit(spec, owner)


def test_api_requires_login(app_client, tmp_path):
    settings = _settings(tmp_path, tmp_path / "checkpoints", auth_mode="ldap", auth_required=True, ldap_url="ldap://x")
    client, _service = app_client(settings)
    with client:
        response = client.get("/api/v1/jobs/job1/tensorboard")
    assert response.status_code == 401
    assert response.json() == {"detail": "Bearer token required"}


def test_api_returns_series_for_existing_job(app_client, tmp_path):
    checkpoints = tmp_path / "checkpoints"
    write_events(checkpoints, "job1", points=[(1, 0.9), (2, 0.4), (3, 0.3)])
    # auth_required=False 下不做鉴权、所有人都是 admin
    client, service = app_client(_settings(tmp_path, checkpoints, auth_required=False))
    _submit(service, "job1")
    with client:
        response = client.get("/api/v1/jobs/job1/tensorboard")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "job1"
    assert body["path_exists"] is True
    assert body["tags"] == ["loss"]
    assert [point["step"] for point in body["series"]["loss"]] == [1, 2, 3]


def test_api_missing_events_returns_path_exists_false(app_client, tmp_path):
    client, service = app_client(_settings(tmp_path, tmp_path / "checkpoints", auth_required=False))
    _submit(service, "job1")
    with client:
        body = client.get("/api/v1/jobs/job1/tensorboard").json()

    assert body["path_exists"] is False
    assert body["series"] == {}


def test_api_rejects_unknown_limit(app_client, tmp_path):
    client, service = app_client(_settings(tmp_path, tmp_path / "checkpoints", auth_required=False))
    _submit(service, "job1")
    with client:
        assert client.get("/api/v1/jobs/job1/tensorboard", params={"limit": 0}).status_code == 422
        assert client.get("/api/v1/jobs/job1/tensorboard", params={"limit": 100_001}).status_code == 422


def test_api_unknown_job_returns_404(app_client, tmp_path):
    client, _service = app_client(_settings(tmp_path, tmp_path / "checkpoints", auth_required=False))
    with client:
        response = client.get("/api/v1/jobs/never-existed/tensorboard")
    assert response.status_code == 404


def test_api_hides_other_users_jobs(app_client, tmp_path):
    """非管理员看别人的任务：404（不泄露任务名是否存在），看自己的正常。"""
    checkpoints = tmp_path / "checkpoints"
    write_events(checkpoints, "alice-job", points=[(1, 0.5)])
    write_events(checkpoints, "bob-job", points=[(1, 0.4)])
    client, service = app_client(_settings(tmp_path, checkpoints))
    alice = service.sessions.issue("alice", "user", 12)
    alice_headers = {"Authorization": f"Bearer {alice['token']}"}
    _submit(service, "alice-job", owner="alice")
    _submit(service, "bob-job", owner="bob")
    with client:
        assert client.get("/api/v1/jobs/alice-job/tensorboard", headers=alice_headers).status_code == 200
        denied = client.get("/api/v1/jobs/bob-job/tensorboard", headers=alice_headers)
        assert denied.status_code == 404
        # 别人的任务用 404 而不是 403：与 metrics / logs 同一套语义
        assert denied.json()["detail"].startswith("job ")
        assert client.get("/api/v1/jobs/never-existed/tensorboard", headers=alice_headers).status_code == 404

        # 管理员能看到所有人的
        admin = {"Authorization": "Bearer test-token"}
        assert client.get("/api/v1/jobs/bob-job/tensorboard", headers=admin).status_code == 200