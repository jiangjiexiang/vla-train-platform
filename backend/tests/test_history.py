from __future__ import annotations

import sqlite3

from app.history import HistoryStore
from app.models import SubmitSpec


def make_spec(name="exp001"):
    return SubmitSpec(name=name, image="vla-smoke:latest", gpus=1, command="true")


def test_submission_roundtrip(history):
    history.record_submission(make_spec(), "2026-01-01T00:00:00Z")
    spec, submitted_at = history.latest_submission("exp001")
    assert spec.image == "vla-smoke:latest"
    assert submitted_at == "2026-01-01T00:00:00Z"


def test_latest_submission_returns_newest(tmp_path):
    store = HistoryStore(str(tmp_path / "h.db"))
    store.record_submission(make_spec().model_copy(update={"command": "first"}), "2026-01-01T00:00:00Z")
    store.record_submission(make_spec().model_copy(update={"command": "second"}), "2026-01-02T00:00:00Z")
    spec, _ = store.latest_submission("exp001")
    assert spec.command == "second"


def test_cancel_is_idempotent(history):
    history.mark_cancelled(make_spec(), "2026-01-01T00:00:00Z")
    history.mark_cancelled(make_spec(), "2026-01-01T00:00:00Z")
    record = history.get_cancellation("exp001")
    assert record["status"] == "CANCELLED"
    assert len(history.list_cancellations()) == 1


def test_marks_terminal_status(history):
    history.mark_cancelled(make_spec(), "2026-01-01T00:00:00Z", status="SUCCEEDED")
    assert history.get_cancellation("exp001")["status"] == "SUCCEEDED"


def test_migrates_legacy_table_without_status(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE cancellations (
                name TEXT PRIMARY KEY,
                spec_json TEXT NOT NULL,
                created_at TEXT,
                cancelled_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO cancellations(name, spec_json, created_at, cancelled_at) VALUES (?, ?, ?, ?)",
            (
                "oldjob",
                make_spec("oldjob").model_dump_json(),
                "2026-01-01T00:00:00Z",
                "2026-01-01T01:00:00Z",
            ),
        )

    store = HistoryStore(path)
    record = store.get_cancellation("oldjob")
    assert record is not None
    assert record["status"] == "CANCELLED", "旧记录没有 status 列时应回退为 CANCELLED"


def test_unknown_cancellation_returns_none(history):
    assert history.get_cancellation("nope") is None
