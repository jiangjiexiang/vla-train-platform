from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .checkpoint_retention import UnsafePath, safe_job_dir
from .errors import ServiceError


class MaintenanceServiceMixin:
    def checkpoint_usage(self) -> dict[str, Any]:
        """按任务统计产物占用；查不到目录的任务不出现在结果里。"""
        root = Path(self.settings.checkpoints_mount_path)
        items: list[dict[str, Any]] = []
        total = 0

        if root.is_dir():
            for entry in root.iterdir():
                if not entry.is_dir() or entry.is_symlink():
                    continue
                try:
                    size, files = _dir_usage(entry)
                except OSError:
                    continue
                items.append({"job": entry.name, "bytes": size, "files": files})
                total += size

        items.sort(key=lambda item: item["bytes"], reverse=True)
        return {"total_bytes": total, "items": items}

    def estimate_run(self, request: Any) -> dict[str, Any]:
        """按相同 batch 的历史实测速度估算训练时长。"""
        from .estimate import estimate_duration

        recipe = self.get_recipe(request.recipe_id)
        defaults = dict(recipe.defaults or {})
        defaults.update({k: v for k, v in (request.overrides or {}).items() if v is not None})

        try:
            steps = int(defaults.get("steps") or 0)
        except (TypeError, ValueError):
            steps = 0
        if steps <= 0:
            return {"estimated_seconds": None, "estimated_text": "—", "source": "unknown", "samples": 0}

        raw_batch = defaults.get("batch_size")
        try:
            batch_size = int(raw_batch) if raw_batch is not None else None
        except (TypeError, ValueError):
            batch_size = None

        history: list[dict[str, Any]] = []
        for job in self._list_jobs(self.kubernetes.list_namespace_pods(), limit=None)["items"]:
            if job.get("status") != "SUCCEEDED":
                continue
            parsed = _parse_training_args(str(job.get("command") or ""))
            if not parsed:
                continue
            history.append(
                {
                    "steps": parsed.get("steps"),
                    "batch_size": parsed.get("batch_size"),
                    "duration_seconds": job.get("duration_seconds"),
                }
            )

        result = estimate_duration(steps=steps, batch_size=batch_size, history=history)
        result["steps"] = steps
        result["batch_size"] = batch_size
        return result

    def failure_summary(self, *, limit: int = 20) -> dict[str, Any]:
        """分析最近 limit 个失败任务，避免全量诊断拖慢接口。"""
        from .failure_stats import summarize_failures

        jobs = self._list_jobs(self.kubernetes.list_namespace_pods(), limit=None)["items"]
        failed = [job for job in jobs if job.get("status") == "FAILED"][:limit]

        diagnoses: dict[str, Any] = {}
        for job in failed:
            name = str(job.get("name") or "")
            try:
                result = self.job_diagnosis(name)
            except Exception:  # noqa: BLE001 - 单个任务诊断失败不该影响整体统计
                result = None
            diagnoses[name] = (result or {}).get("diagnosis")

        return summarize_failures(failed, diagnoses)

    def plan_cleanup(
        self,
        *,
        older_than_days: int,
        keep_latest: int = 20,
        include_checkpoints: bool = False,
    ) -> dict[str, Any]:
        """算出该清理什么，不执行。清理不可逆，先让用户看一眼。"""
        from .cleanup import plan_cleanup as build_plan

        jobs = self._list_jobs(self.kubernetes.list_namespace_pods(), limit=None)["items"]
        plan = build_plan(
            jobs,
            older_than_days=older_than_days,
            keep_latest=keep_latest,
            include_checkpoints=include_checkpoints,
        )
        return plan.as_dict()

    def cleanup_jobs(
        self,
        *,
        older_than_days: int,
        keep_latest: int = 20,
        include_checkpoints: bool = False,
    ) -> dict[str, Any]:
        """清理历史任务；单项失败不中断整批，最后统一汇总。"""
        from .cleanup import plan_cleanup as build_plan

        jobs = self._list_jobs(self.kubernetes.list_namespace_pods(), limit=None)["items"]
        plan = build_plan(
            jobs,
            older_than_days=older_than_days,
            keep_latest=keep_latest,
            include_checkpoints=include_checkpoints,
        )

        deleted: list[str] = []
        failed: list[dict[str, str]] = []

        for name in plan.job_names:
            try:
                # 对象可能已被 ttlSecondsAfterFinished 回收，404 视为已完成。
                self.kubernetes.delete_job(name)
            except ServiceError as exc:
                if exc.status_code != 404:
                    failed.append({"job": name, "stage": "k8s", "error": str(exc)})
                    continue
            try:
                self.history.forget_job(name)
            except Exception as exc:  # noqa: BLE001 - 单条失败不该中断整批
                failed.append({"job": name, "stage": "history", "error": str(exc)})
                continue
            deleted.append(name)

        removed_checkpoints: list[str] = []
        for name in plan.checkpoints:
            # 删除前必须校验路径，并使用 Web 容器实际挂载的根目录。
            try:
                path = safe_job_dir(Path(self.settings.checkpoints_mount_path), name)
            except UnsafePath as exc:
                failed.append({"job": name, "stage": "path_check", "error": str(exc)})
                continue
            try:
                if path.is_dir():
                    import shutil

                    shutil.rmtree(path)
                    removed_checkpoints.append(name)
            except OSError as exc:
                failed.append({"job": name, "stage": "checkpoint", "error": str(exc)})

        self.audit.record(
            "jobs.cleanup",
            actor=self._current_actor,
            target=f"{len(deleted)} jobs",
            detail={
                "older_than_days": older_than_days,
                "keep_latest": keep_latest,
                "checkpoints_removed": len(removed_checkpoints),
            },
        )
        return {
            "deleted_jobs": deleted,
            "removed_checkpoints": removed_checkpoints,
            "failed": failed,
            "kept_running": plan.keep,
            "summary": {
                "deleted": len(deleted),
                "checkpoints": len(removed_checkpoints),
                "failed": len(failed),
            },
        }


def _dir_usage(path: Path) -> tuple[int, int]:
    """返回 (字节数, 文件数)。单个文件统计失败就跳过。"""
    total = 0
    files = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
                files += 1
        except OSError:
            continue
    return total, files


def _parse_training_args(command: str) -> dict[str, int] | None:
    """从平台生成的命令中读取 steps 和 batch_size；找不到则跳过该样本。"""
    if not command:
        return None
    result: dict[str, int] = {}
    for key in ("steps", "batch_size"):
        match = re.search(rf"--{key}[= ](\d+)", command)
        if match:
            result[key] = int(match.group(1))
    return result or None
