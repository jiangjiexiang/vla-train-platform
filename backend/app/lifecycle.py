from __future__ import annotations

import logging

from fastapi import FastAPI

from .config import Settings
from .services.job_watcher import start_job_watcher
from .services.notifier import Notifier
from .services.platform import PlatformService

logger = logging.getLogger("vla-platform")


def register_lifecycle(app: FastAPI, service: PlatformService, settings: Settings) -> None:
    @app.on_event("startup")
    def start_auto_backup() -> None:
        """起一个后台线程定期备份数据库。

        只提供手动接口是不够的——没人会每天点一次。
        而数据库里是审计和提交历史，真丢了无法重建。

        interval 为 0 时不起线程（测试或不需要备份的部署）。
        """
        interval = settings.backup_interval_seconds
        if interval <= 0:
            return

        import threading
        from pathlib import Path

        from .services.backup import create_backup, prune_backups

        stop = threading.Event()
        app.state.backup_stop = stop

        def loop() -> None:
            # 启动后先等一会儿再备第一次，避开启动时的其它初始化 IO。
            # 首次等待不能超过正常间隔，否则短间隔配置会被拖长。
            first_delay = min(interval, 300)
            if stop.wait(first_delay):
                return
            while True:
                try:
                    result = create_backup(Path(settings.db_path), Path(settings.backup_dir))
                    if result.get("ok"):
                        prune_backups(Path(settings.backup_dir), settings.backup_keep)
                    else:
                        print(f"[backup] 备份失败：{result.get('error')}", flush=True)
                except Exception as exc:  # noqa: BLE001 - 备份失败绝不能影响服务
                    print(f"[backup] 备份异常：{exc}", flush=True)
                # 一次备份做完再等下一轮，间隔就是 interval。
                if stop.wait(interval):
                    return

        thread = threading.Thread(target=loop, daemon=True, name="db-backup")
        thread.start()
        app.state.backup_thread = thread

    @app.on_event("startup")
    def start_job_notifications() -> None:
        """任务跑到终态时推送到钉钉/企微/通用 webhook。

        没配任何 webhook 时这里什么都不做：不起线程、不建表、不碰 K8s，
        与加这个功能之前的启动流程完全一致。
        """
        notifier = Notifier(settings)
        app.state.notifier = notifier
        if not notifier.enabled:
            return
        watcher = start_job_watcher(app, service, notifier, settings)
        if watcher is not None:
            logger.info(
                "job notifications enabled (interval=%ss, channels=%s)",
                settings.notify_interval_seconds,
                ", ".join(
                    name
                    for name, url in (
                        ("dingtalk", notifier.dingtalk_webhook),
                        ("wecom", notifier.wecom_webhook),
                        ("generic", notifier.generic_webhook),
                    )
                    if url
                ),
            )

    @app.on_event("startup")
    def purge_expired_sessions() -> None:
        # 会话持久化在 SQLite 里；重启时顺手清掉过期令牌，避免表无限增长。
        removed = service.sessions.purge_expired()
        if removed:
            logger.info("purged %s expired sessions", removed)

    @app.on_event("shutdown")
    def stop_job_notifications() -> None:
        # 与备份线程一致的停止方式：set 事件，线程在下一轮等待时退出。
        stop = getattr(app.state, "notify_stop", None)
        if stop is not None:
            stop.set()
