"""数据库恢复工具。

备份的意义在于能恢复。但平台侧没有恢复入口——真出事的时候，运维只能
自己去 `cp` 文件，还不确定该停哪些服务、旧的 WAL 要不要一起删。

这个工具做三件事，每一步都对"出错时会怎样"有明确交代：

1. ``--check``：验证某个数据库文件是否可读且完整。不需要停服务，
   适合在恢复前先确认备份是好的——恢复一个坏备份比不恢复更糟。
2. ``--list``：列出可用备份，带完整性和行数。选哪一份凭这个判断，
   而不是凭文件名猜。
3. ``--restore``：把指定备份换进去。会先把当前库另存一份（万一备份是坏的，
   还能退回），然后原子替换。

恢复必须停服务：SQLite 在进程持有连接时替换文件，旧进程会继续用已删除的
inode 写，看起来正常但数据全丢。所以这里不做"热恢复"。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# 这两个是**宿主机**路径，不是容器内路径。
#
# 容器里 /data/history 挂的是宿主机的 /data/vla-platform/history，
# 容器内看到的 /data/history/platform.db 对应宿主机的
# /data/vla-platform/history/platform.db。运维在宿主机上跑这个脚本时
# 必须用宿主机的真实路径，否则会报"文件不存在"——我第一次跑的时候就
# 拿容器路径查了宿主机，白紧张了一下。
DEFAULT_DB = Path("/data/vla-platform/history/platform.db")
DEFAULT_BACKUP_DIR = Path("/data/vla-platform/history/backups")
# 需要检查的关键表：缺了这些说明备份不完整
REQUIRED_TABLES = ("submissions", "audit_log", "cancellations")


def check_db(path: Path) -> dict:
    """检查一个数据库文件是否可用。"""
    if not path.exists():
        return {"ok": False, "error": f"文件不存在：{path}"}
    try:
        # 只读打开，避免顺手改了被检查的文件
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                return {"ok": False, "error": f"完整性检查失败：{integrity}"}
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            counts = {}
            for table in REQUIRED_TABLES:
                if table in tables:
                    counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            missing = [t for t in REQUIRED_TABLES if t not in tables]
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "integrity": integrity,
        "counts": counts,
        "missing_tables": missing,
        "bytes": path.stat().st_size,
    }


def list_backups(backup_dir: Path) -> list[dict]:
    if not backup_dir.is_dir():
        return []
    items = []
    for path in sorted(backup_dir.glob("platform-*.db"), reverse=True):
        info = check_db(path)
        items.append({"path": str(path), "name": path.name, **info})
    return items


def restore(backup: Path, db: Path, *, force: bool = False) -> dict:
    """用备份替换当前数据库。

    替换前先把当前库另存一份：万一选中的备份其实是坏的，
    这一步让操作可回退。
    """
    info = check_db(backup)
    if not info["ok"] and not force:
        return {"ok": False, "error": f"备份不可用，拒绝恢复：{info['error']}。确认要用请加 --force"}

    if db.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safety = db.with_name(f"{db.name}.before-restore-{stamp}")
        try:
            shutil.copy2(db, safety)
        except OSError as exc:
            return {"ok": False, "error": f"保存当前库失败，中止恢复：{exc}"}
    else:
        safety = None

    # WAL / SHM 必须一起清掉：留着旧的 WAL 会让新库读到不属于它的页
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db) + suffix)
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError as exc:
                return {"ok": False, "error": f"清理 {sidecar} 失败：{exc}"}

    tmp = db.with_name(f"{db.name}.restoring")
    try:
        shutil.copy2(backup, tmp)
        os.replace(tmp, db)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "error": f"替换失败：{exc}"}

    return {"ok": True, "restored_from": str(backup), "safety_copy": str(safety) if safety else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据库备份检查与恢复")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", metavar="DB", help="检查某个数据库文件")
    group.add_argument("--list", action="store_true", help="列出备份并逐个检查")
    group.add_argument("--restore", metavar="BACKUP", help="用指定备份恢复")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--force", action="store_true", help="即使备份检查不通过也强行恢复")
    args = parser.parse_args(argv)

    if args.list:
        items = list_backups(Path(args.backup_dir))
        if not items:
            print(f"{args.backup_dir} 下没有备份")
            return 0
        for item in items:
            status = "OK  " if item["ok"] else "坏  "
            counts = item.get("counts") or {}
            detail = "  ".join(f"{k}={v}" for k, v in counts.items())
            print(f"  {status} {item['name']:40} {item['bytes']:>10} 字节  {detail}")
            if not item["ok"]:
                print(f"       {item.get('error')}")
        return 0

    if args.check:
        info = check_db(Path(args.check))
        print(f"文件: {args.check}")
        print(f"可用: {info['ok']}")
        if info["ok"]:
            print(f"完整性: {info['integrity']}")
            for table, count in info["counts"].items():
                print(f"  {table}: {count}")
            if info["missing_tables"]:
                print(f"缺少表: {info['missing_tables']}")
        else:
            print(f"错误: {info['error']}")
        return 0 if info["ok"] else 1

    result = restore(Path(args.restore), Path(args.db), force=args.force)
    if result["ok"]:
        print(f"已从 {result['restored_from']} 恢复")
        if result["safety_copy"]:
            print(f"恢复前的库另存为 {result['safety_copy']}")
        print("\n记得重启服务：旧进程仍持有已删除文件的句柄。")
        print("  kubectl -n vla-platform rollout restart deploy/vla-web")
        return 0
    print(f"恢复失败：{result['error']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
