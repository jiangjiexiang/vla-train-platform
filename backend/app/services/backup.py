"""数据库备份。

平台的全部管理数据都在这一个 SQLite 文件里：审计日志、提交历史、取消记录、
用户配额。它现在是 620KB 的单文件，没有任何备份——文件损坏或被误删，
这些记录就全没了。

关键点：**不能直接 cp 文件**。SQLite 用 WAL 或回滚日志时，正在写入的数据库
文件本身可能不是一致的快照；直接复制得到的副本有可能打不开，
或者缺最后几个事务。正确做法是用 sqlite3 的在线备份 API，
它会在读事务里生成一致快照，不需要停服务。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 备份文件名里的时间戳格式：20260101T120000Z，可直接按名字排序
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def backup_path(backup_dir: Path, moment: datetime | None = None) -> Path:
    moment = moment or datetime.now(timezone.utc)
    return backup_dir / f"platform-{moment.strftime(STAMP_FORMAT)}.db"


def create_backup(db_path: Path, backup_dir: Path) -> dict[str, Any]:
    """生成一份一致性备份，返回文件信息。

    用 ``sqlite3.Connection.backup()`` 而不是 shutil.copy：
    后者在数据库正被写入时可能拿到半个事务。
    """
    if not db_path.exists():
        return {"ok": False, "error": f"数据库不存在：{db_path}"}

    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_path(backup_dir)

    # 目标已存在（同一秒内重复调用）时加后缀，避免覆盖已有备份
    index = 1
    while target.exists():
        target = backup_path(backup_dir).with_name(f"{target.stem}-{index}.db")
        index += 1

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as source:
            with sqlite3.connect(target) as dest:
                source.backup(dest)
    except sqlite3.Error as exc:
        # 失败时不留半成品文件：一个打不开的 .db 比没有更糟，
        # 因为它会出现在备份列表里让人误以为有备份
        try:
            target.unlink()
        except OSError:
            pass
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "path": str(target),
        "bytes": target.stat().st_size,
    }


def prune_backups(backup_dir: Path, keep: int) -> dict[str, Any]:
    """只保留最近 keep 份备份。

    备份不设上限会把磁盘吃满——一个 620KB 的库每天备一次是小事，
    但加上 checkpoint 目录一起长就不一定了。keep 至少为 1。
    """
    keep = max(1, int(keep))
    if not backup_dir.is_dir():
        return {"removed": [], "kept": 0, "failed": []}

    files = sorted(
        (p for p in backup_dir.glob("platform-*.db") if p.is_file()),
        key=lambda p: p.name,
        reverse=True,
    )
    removed: list[str] = []
    failed: list[dict[str, str]] = []
    for path in files[keep:]:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError as exc:
            # 以前这里是静默 continue：权限不足时删不掉却报"保留 N 份"，
            # 于是备份目录无限增长而没人知道。实测普通用户对 10001 属主的
            # 目录就是删不了，但接口显示一切正常。
            failed.append({"name": path.name, "error": str(exc)})
    return {
        "removed": removed,
        "kept": min(len(files), keep),
        "failed": failed,
    }


def list_backups(backup_dir: Path) -> list[dict[str, Any]]:
    if not backup_dir.is_dir():
        return []
    items = []
    for path in sorted(backup_dir.glob("platform-*.db"), reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({"name": path.name, "bytes": stat.st_size, "modified": stat.st_mtime})
    return items
