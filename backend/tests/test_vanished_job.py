"""任务对象消失后仍能查到提交内容。

背景：ttlSecondsAfterFinished 到期回收、节点重启或手工删除都会让 PyTorchJob
对象消失。早先这种情况直接 404，用户看到任务「凭空消失」且没有任何解释，
排障无从下手。history 里的提交记录还在，应该利用起来。
"""

from __future__ import annotations

import pytest
from app.history import HistoryStore
from app.models import SubmitSpec


@pytest.fixture
def store(tmp_path) -> HistoryStore:
    return HistoryStore(str(tmp_path / "history.db"))


def _spec(name: str = "gone-job") -> SubmitSpec:
    return SubmitSpec(
        name=name,
        image="vla-smolvla:latest",
        gpus=1,
        command="python /app/train_vla.py --steps 10",
    )


def test_recovers_submission_of_deleted_job(store: HistoryStore):
    store.record_submission(_spec(), "2026-01-01T00:00:00Z")

    found = store.find_submission("gone-job")

    assert found is not None
    spec, submitted_at = found
    assert spec.image == "vla-smolvla:latest"
    assert spec.command.startswith("python /app/train_vla.py")
    assert submitted_at == "2026-01-01T00:00:00Z"


def test_unknown_name_returns_none(store: HistoryStore):
    assert store.find_submission("never-existed") is None


def test_returns_latest_when_resubmitted(store: HistoryStore):
    """同一个名字可能被重复提交（旧对象已回收），要拿到最近那次。"""
    store.record_submission(_spec(), "2026-01-01T00:00:00Z")
    store.record_submission(
        SubmitSpec(
            name="gone-job",
            image="vla-smolvla:v2",
            gpus=1,
            command="python /app/train_vla.py --steps 99",
        ),
        "2026-01-02T00:00:00Z",
    )

    found = store.find_submission("gone-job")

    assert found is not None
    assert found[0].image == "vla-smolvla:v2"
    assert found[1] == "2026-01-02T00:00:00Z"
