"""数据库备份测试。

平台的全部管理数据（审计、提交历史、配额）就在一个 SQLite 文件里。
没有备份的话，文件损坏或被误删就全没了。这里的重点不是"能复制文件"，
而是"复制出来的东西真的能用"。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.services.backup import create_backup, list_backups, prune_backups


def _make_db(path, rows: int = 10) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        for i in range(rows):
            conn.execute("INSERT INTO t (v) VALUES (?)", (f"row{i}",))
        conn.commit()


def test_backup_is_readable_and_complete(tmp_path):
    db = tmp_path / "platform.db"
    _make_db(db, rows=25)

    result = create_backup(db, tmp_path / "backups")

    assert result["ok"] is True
    # 关键：备份必须能独立打开，并且数据完整
    with sqlite3.connect(result["path"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 25
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_missing_database_reports_error(tmp_path):
    result = create_backup(tmp_path / "nope.db", tmp_path / "backups")

    assert result["ok"] is False
    assert "不存在" in result["error"]


def test_failed_backup_leaves_no_stub_file(tmp_path):
    """失败时不能留下打不开的 .db —— 那种文件会让人误以为有备份。"""
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a database at all")
    bdir = tmp_path / "backups"

    result = create_backup(bad, bdir)

    assert result["ok"] is False
    # 要么目录不存在，要么里面没有残留文件
    assert not bdir.exists() or list_backups(bdir) == []


def test_prune_keeps_newest(tmp_path):
    db = tmp_path / "platform.db"
    _make_db(db)
    bdir = tmp_path / "backups"
    for _ in range(5):
        create_backup(db, bdir)

    result = prune_backups(bdir, keep=2)

    remaining = list_backups(bdir)
    assert len(remaining) == 2
    assert result["kept"] == 2
    assert len(result["removed"]) == 3


def test_prune_never_removes_everything(tmp_path):
    """keep 写成 0 也要留一份——备份全删掉等于没有备份。"""
    db = tmp_path / "platform.db"
    _make_db(db)
    bdir = tmp_path / "backups"
    for _ in range(3):
        create_backup(db, bdir)

    prune_backups(bdir, keep=0)

    assert len(list_backups(bdir)) == 1


def test_prune_on_missing_dir_is_safe(tmp_path):
    result = prune_backups(tmp_path / "no-such-dir", keep=3)

    assert result["removed"] == []
    assert result["kept"] == 0


def test_list_backups_sorted_newest_first(tmp_path):
    db = tmp_path / "platform.db"
    _make_db(db)
    bdir = tmp_path / "backups"
    for _ in range(3):
        create_backup(db, bdir)

    items = list_backups(bdir)

    names = [item["name"] for item in items]
    assert names == sorted(names, reverse=True)


def test_repeated_backups_do_not_overwrite(tmp_path):
    """同一秒内连续备份不能互相覆盖，否则只剩最后一份。"""
    db = tmp_path / "platform.db"
    _make_db(db)
    bdir = tmp_path / "backups"

    first = create_backup(db, bdir)
    second = create_backup(db, bdir)

    assert first["path"] != second["path"]
    assert len(list_backups(bdir)) == 2


def test_prune_reports_permission_failures(tmp_path, monkeypatch):
    """删不掉时必须报告，不能静默 continue。

    实测踩过：备份文件属主是容器内的 10001，运维用普通用户跑清理时
    一个都删不掉，但接口返回「保留 2 份」——看起来清理了，实际全在，
    备份目录会无限增长而没人发现。
    """
    from app.services import backup as backup_module

    bdir = tmp_path / "backups"
    bdir.mkdir()
    for i in range(4):
        (bdir / f"platform-2026010{i}T000000Z.db").write_bytes(b"x")

    def boom(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "unlink", boom)

    result = backup_module.prune_backups(bdir, keep=1)

    assert result["removed"] == []
    assert len(result["failed"]) == 3
    assert "Permission denied" in result["failed"][0]["error"]


def test_prune_result_always_has_failed_key(tmp_path):
    """结构稳定：没有失败也要有空列表，调用方不用做存在性判断。"""
    from app.services.backup import prune_backups

    empty = prune_backups(tmp_path / "nope", keep=3)
    assert empty["failed"] == []
