"""数据集元数据的持久化与变更检测。

扫描结果存 SQLite（与历史库同一个文件），避免每次请求都重新扫盘：
``/datasets`` 目录里可能有几十个数百 MB 的数据集，反复扫描会拖垮 Web。

变更检测靠 ``dir_mtime``：数据集根目录的 mtime 变了（新增/删除文件、改子目录）
就认为需要重扫。注意 mtime 只能反映**顶层目录**的变化，深层次文件修改不一定更新它，
所以另外提供强制重扫的入口。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .scanner import DatasetIssue, DatasetMeta, scan_deep, scan_shallow, utc_now


class DatasetStore:
    """数据集元数据表。表结构与 v0.9 的 history 库共用一个 SQLite 文件。"""

    def __init__(self, db_path: str):
        self.path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._scan_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY,
                    meta_json TEXT NOT NULL,
                    dir_mtime REAL NOT NULL,
                    scanned_at TEXT NOT NULL,
                    scan_depth TEXT NOT NULL
                );
                """
            )

    # --- 读写 ---------------------------------------------------------
    def get(self, dataset_id: str) -> DatasetMeta | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT meta_json FROM datasets WHERE id = ?", (dataset_id,)
            ).fetchone()
        if row is None:
            return None
        return DatasetMeta.from_dict(json.loads(row["meta_json"]))

    def list_all(self) -> list[DatasetMeta]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT meta_json FROM datasets ORDER BY id"
            ).fetchall()
        return [DatasetMeta.from_dict(json.loads(row["meta_json"])) for row in rows]

    def save(self, meta: DatasetMeta) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO datasets(id, meta_json, dir_mtime, scanned_at, scan_depth)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    meta_json = excluded.meta_json,
                    dir_mtime = excluded.dir_mtime,
                    scanned_at = excluded.scanned_at,
                    scan_depth = excluded.scan_depth
                """,
                (meta.id, json.dumps(meta.as_dict(), ensure_ascii=False), meta.dir_mtime,
                 meta.scanned_at, meta.scan_depth),
            )

    def delete(self, dataset_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
        return cursor.rowcount > 0

    def delete_missing(self, existing_ids: set[str]) -> list[str]:
        """删除库里存在、但 /datasets 下已经不存在的记录。返回被删的 id。"""
        removed = []
        for meta in self.list_all():
            if meta.id not in existing_ids:
                self.delete(meta.id)
                removed.append(meta.id)
        return removed

    # --- 扫描 ---------------------------------------------------------
    def needs_rescan(self, root: Path) -> list[str]:
        """返回需要（重新）扫描的数据集 id。

        条件：目录存在但库里没有；或目录 mtime 与记录不一致。
        """
        from .scanner import list_dataset_dirs

        known = {meta.id: meta.dir_mtime for meta in self.list_all()}
        pending: list[str] = []
        for directory in list_dataset_dirs(root):
            try:
                mtime = directory.stat().st_mtime
            except OSError:
                continue
            if directory.name not in known or abs(known[directory.name] - mtime) > 1e-6:
                pending.append(directory.name)
        return pending

    def scan_one(self, root: Path, dataset_id: str, deep: bool = False) -> DatasetMeta:
        """扫描 root 下名为 dataset_id 的目录并落库。路径不存在时抛 FileNotFoundError。"""
        return self.scan_path(root / dataset_id, dataset_id, deep=deep)

    def scan_path(self, directory: Path, dataset_id: str, deep: bool = False) -> DatasetMeta:
        """扫描**任意已知路径**并落库。

        注册接口登记的目录不一定在扫描根下（见 PLATFORM_DATASET_ALLOWED_ROOTS），
        所以这里不把路径拼成 ``root / id``，而是直接用调用方校验过的绝对路径。
        """
        if not directory.is_dir():
            raise FileNotFoundError(f"dataset directory not found: {directory}")
        meta = scan_deep(directory, dataset_id) if deep else scan_shallow(directory, dataset_id)
        self.save(meta)
        return meta

    def scan_many(
        self,
        root: Path,
        dataset_ids: list[str] | None = None,
        deep: bool = False,
    ) -> list[DatasetMeta]:
        """扫描指定的（或所有已存在的）数据集。

        加锁避免两个请求同时扫描写库；单个数据集失败只记录 issue，不中断整批。
        """
        from .scanner import list_dataset_dirs

        with self._scan_lock:
            if dataset_ids is None:
                targets = [directory.name for directory in list_dataset_dirs(root)]
            else:
                targets = dataset_ids
            results: list[DatasetMeta] = []
            for dataset_id in targets:
                try:
                    results.append(self.scan_one(root, dataset_id, deep=deep))
                except FileNotFoundError:
                    self.delete(dataset_id)
                except Exception as exc:  # noqa: BLE001 - 单点失败不影响其它数据集
                    failed = DatasetMeta(
                        id=dataset_id, name=dataset_id, path=str(root / dataset_id), scanned_at=utc_now()
                    )
                    failed.status = "error"
                    failed.issues.append(DatasetIssue("error", "scan_failed", f"扫描失败：{exc}"))
                    self.save(failed)
                    results.append(failed)
            # 目录已被删除的数据集，从库里清掉
            existing = {directory.name for directory in list_dataset_dirs(root)} if dataset_ids is None else set()
            if existing:
                self.delete_missing(existing)
            return results

