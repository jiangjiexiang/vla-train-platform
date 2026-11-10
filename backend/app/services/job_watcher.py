"""任务状态轮询：盯着 K8s 里的 PyTorchJob，跑到终态时发一条通知。

为什么需要单独的线程：
平台原本没有任何后台轮询——列表页是用户打开时才查 K8s。可通知的前提
是「任务结束的那一刻有人知道」，不能等人打开页面。所以这里起一个后台
线程，定时扫一遍 namespace 里的 PyTorchJob。

几个必须处理好的点：

1. **不等状态跃迁**：我们只关心终态（Succeeded/Failed）。已有任务在
   平台启动时可能已经是终态了，那种不该补发通知，否则每次重启都会
   把历史任务重新刷一遍群。所以启动后的第一轮扫描只记快照、不发通知
   （``prime_first_scan``）。

2. **不重复通知**：进程重启后内存里的集合就没了。已通知的任务名写进
   SQLite（history 库里的 job_notifications 表），重启后照样跳过。

3. **不发 CANCELLED**：用户主动取消是预期内的，发通知是噪音。

4. **不能拖垮自己**：K8s 查询失败、webhook 超时、数据库写失败，
   任何一个异常都只记日志，线程继续下一轮。一轮的耗时有上限
   （webhook 有 timeout），不会无限挂起。

5. **没配 webhook 就完全不起作用**：``start_job_watcher()`` 在
   ``settings.notify_enabled`` 为假时直接返回 None——不起线程、
   不建表、不碰 K8s。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("vla-platform")

TERMINAL_STATUSES = ("SUCCEEDED", "FAILED")


class JobNotificationStore:
    """已通知任务的持久化记录（history 库里的 job_notifications 表）。

    只存任务名 + 事件 + 时间。按任务名判重而不是按 (name, event)：
    同名任务重新提交在平台上意味着 K8s 对象已经没了（提交会校验重名），
    所以一个名字只会有一个终态。
    """

    def __init__(self, path: str) -> None:
        import sqlite3
        from pathlib import Path

        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=10) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS job_notifications (
                    name TEXT PRIMARY KEY,
                    event TEXT NOT NULL,
                    notified_at TEXT NOT NULL
                );
                """
            )

    def _connect(self):
        import sqlite3

        return sqlite3.connect(self.path, timeout=10)

    def has(self, name: str) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM job_notifications WHERE name = ?", (name,)).fetchone()
        return row is not None

    def record(self, name: str, event: str, notified_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO job_notifications(name, event, notified_at) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    event = excluded.event,
                    notified_at = excluded.notified_at
                """,
                (name, event, notified_at),
            )

    def prune(self, keep_names: set[str]) -> int:
        """删掉已经不在 K8s 里的任务记录，避免表无限增长。

        ``keep_names`` 是本轮扫描仍在 namespace 里看到的所有任务名。
        K8s 对象已回收（ttlSecondsAfterFinished）之后，同名任务不可能
        再被判定终态，所以这些记录留着没有意义。返回删除行数。
        """
        with self._connect() as connection:
            rows = connection.execute("SELECT name FROM job_notifications").fetchall()
            stale = [row[0] for row in rows if row[0] not in keep_names]
            for name in stale:
                connection.execute("DELETE FROM job_notifications WHERE name = ?", (name,))
        return len(stale)


class JobWatcher:
    """后台轮询线程：检测任务终态 → 通知 → 记账。

    设计成可以在测试里直接调 ``scan_once()``，不需要真的起线程。
    """

    def __init__(
        self,
        service: Any,
        notifier: Any,
        store: JobNotificationStore,
        *,
        interval: float = 30.0,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.service = service
        self.notifier = notifier
        self.store = store
        self.interval = float(interval)
        self._clock = clock or _utc_now
        # 启动后第一轮只建快照：进程启动前就已经是终态的任务不补发通知。
        # 记的是「首轮扫描时就已终态」的任务名，之后一直跳过它们。
        self._primed = False
        self._preexisting: set[str] = set()

    # ------------------------------------------------------------------
    def scan_once(self) -> dict[str, Any]:
        """扫一遍命名空间，发通知，返回本轮统计（便于测试与排障）。

        自己吞掉所有异常：调用方（线程循环）不需要 try/except。
        """
        stats = {"jobs": 0, "notified": 0, "skipped": 0, "failed": 0, "primed": False}
        try:
            jobs = self.service.kubernetes.list_jobs()
        except Exception as exc:  # noqa: BLE001 - K8s 暂时不可用，下一轮再来
            logger.warning("job watcher: list jobs failed: %s", exc)
            stats["failed"] += 1
            return stats

        priming = not self._primed
        names = {str(job.get("metadata", {}).get("name") or "") for job in jobs}
        for job in jobs:
            name = str(job.get("metadata", {}).get("name") or "")
            if not name:
                continue
            stats["jobs"] += 1
            try:
                status = _terminal_status(job)
            except Exception as exc:  # noqa: BLE001 - 单个坏对象不影响其它任务
                logger.warning("job watcher: inspect %s failed: %s", name, exc)
                stats["failed"] += 1
                continue
            if status is None:
                continue

            if priming:
                # 第一轮（进程刚启动）看到的终态任务，可能是平台重启前就
                # 已经跑完/失败的。补发会在每次重启时把历史任务重刷一遍群，
                # 所以只登记不发送。
                self._preexisting.add(name)
                stats["skipped"] += 1
                continue
            if name in self._preexisting:
                # 老任务：首轮就已终态，永不补发。但首轮里还在跑、之后才
                # 结束的任务名不在这里，必须照常通知。
                stats["skipped"] += 1
                continue

            try:
                already = self.store.has(name)
            except Exception as exc:  # noqa: BLE001 - 读不到记账状态就跳过本轮，不冒险重发
                logger.warning("job watcher: read notification state for %s failed: %s", name, exc)
                stats["failed"] += 1
                continue
            if already:
                stats["skipped"] += 1
                continue

            payload = _job_payload(self.service, job, name, status)
            results = self.notifier.notify_job(payload, status)
            if not results or not any(result.get("ok") for result in results):
                # 所有渠道都失败：不记账，下一轮重试。
                # 注意 notify_job 自己不会抛异常，这里是纯结果判断。
                stats["failed"] += 1
                logger.warning("job watcher: notify %s (%s) failed on every channel", name, status)
                continue
            try:
                self.store.record(name, status, self._clock())
            except Exception as exc:  # noqa: BLE001 - 记账失败最多导致下一轮重发一次
                logger.warning("job watcher: record notification for %s failed: %s", name, exc)
            stats["notified"] += 1

        self._primed = True
        stats["primed"] = priming

        # 清理已回收任务的记录。放在通知循环之后，失败也不影响本轮。
        try:
            self.store.prune(names)
        except Exception as exc:  # noqa: BLE001
            logger.warning("job watcher: prune notification records failed: %s", exc)
        return stats

    # ------------------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        """线程主循环。``stop`` 被 set 后尽快退出。"""
        # 与被备份线程一样，启动后先等一下再做第一轮，避开启动时的其它 IO。
        if stop.wait(min(self.interval, 5.0)):
            return
        while True:
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001 - 轮询线程绝不能死
                logger.warning("job watcher: unexpected error: %s", exc)
            if stop.wait(self.interval):
                return


def start_job_watcher(app: Any, service: Any, notifier: Any, settings: Any) -> JobWatcher | None:
    """按配置启动轮询线程；没配 webhook 时返回 None 且不做任何事。"""
    if not getattr(settings, "notify_enabled", False):
        return None
    try:
        store = JobNotificationStore(settings.db_path)
    except Exception as exc:  # noqa: BLE001 - 建表失败不能让整个服务起不来
        logger.warning("job watcher disabled: cannot open history db: %s", exc)
        return None

    interval = float(getattr(settings, "notify_interval_seconds", 30.0) or 30.0)
    watcher = JobWatcher(service, notifier, store, interval=interval)
    stop = threading.Event()
    app.state.notify_stop = stop

    def loop() -> None:
        watcher.run(stop)

    thread = threading.Thread(target=loop, daemon=True, name="job-watcher")
    thread.start()
    app.state.notify_thread = thread
    app.state.job_watcher = watcher
    return watcher


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------
def _terminal_status(job: dict[str, Any]) -> str | None:
    """从 PyTorchJob 对象判断是否到了终态。

    只看 PyTorchJob 自己的 conditions，不看 Pod：Pod 可能是重试中的
    上一个实例，把 Pod 的 Failed 当成任务失败会造成误报。
    Kubeflow 的 operator 在任务真正结束后才会写 Succeeded/Failed 条件。
    """
    conditions = job.get("status", {}).get("conditions") or []
    hits: list[str] = []
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        flag = condition.get("status")
        is_true = flag is True or str(flag).lower() == "true"
        if not is_true:
            continue
        kind = str(condition.get("type") or "").upper()
        if kind in TERMINAL_STATUSES:
            hits.append(kind)
    if not hits:
        return None
    # 同时存在时失败优先：宁可报失败也不要漏报。
    return "FAILED" if "FAILED" in hits else hits[0]


def _job_payload(service: Any, job: dict[str, Any], name: str, status: str) -> dict[str, Any]:
    """把 K8s 对象整理成通知需要的字段。

    复用 PlatformService 的解析逻辑（spec 还原、时间、消息），
    保证通知里的内容和页面上看到的一致。解析失败时退回最小字段集，
    绝不能因为一个字段拿不到就把整条通知丢掉。
    """
    try:
        payload = service.describe_job(job)
    except Exception as exc:  # noqa: BLE001
        logger.warning("job watcher: describe %s failed, sending minimal notice: %s", name, exc)
        payload = {"name": name, "status": status}
    payload["name"] = payload.get("name") or name
    payload["status"] = status
    return payload


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "JobNotificationStore",
    "JobWatcher",
    "TERMINAL_STATUSES",
    "start_job_watcher",
]