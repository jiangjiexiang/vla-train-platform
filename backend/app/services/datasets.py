from __future__ import annotations

from pathlib import Path
from typing import Any

from ..datasets.scanner import DatasetMeta
from ..datasets.store import DatasetStore
from .dataset_register import UnsafeDatasetPath, check_lerobot_format, resolve_dataset_dir
from .errors import ServiceError


class DatasetServiceMixin:
    def dataset_store(self) -> DatasetStore:
        """惰性创建，避免每次启动都建表（表本身是幂等的）。

        API 层判断「这个 id 是否已注册」也用它，所以是公开方法。
        """
        store = getattr(self, "_datasets", None)
        if store is None:
            store = DatasetStore(self.settings.db_path)
            self._datasets = store
        return store

    def _datasets_root(self) -> Path:
        return Path(self.settings.datasets_mount_path)

    def list_datasets(self, refresh: bool = False) -> dict[str, Any]:
        """列出数据集。缓存为准，refresh 时先重扫有变化的目录。"""
        store = self.dataset_store()
        root = self._datasets_root()
        if refresh:
            pending = store.needs_rescan(root)
            if pending:
                store.scan_many(root, pending)
        # 列表页只需要展示用字段；详情接口再返回完整内容。
        items = [_slim_dataset(_with_path_check(meta.as_dict())) for meta in store.list_all()]
        summary = {
            "total": len(items),
            "ready": sum(1 for item in items if item["status"] == "ready"),
            "warning": sum(1 for item in items if item["status"] == "warning"),
            # missing 和 unusable 一样不能拿来训练，必须计入「不可用」。
            "unusable": sum(
                1 for item in items if item["status"] in ("unusable", "error", "missing")
            ),
            "missing": sum(1 for item in items if item["status"] == "missing"),
            "total_frames": sum(int(item["frames"]) for item in items),
        }
        return {"items": items, "summary": summary, "root": str(root)}

    def get_dataset(self, dataset_id: str, deep: bool = False) -> dict[str, Any]:
        store = self.dataset_store()
        meta = store.get(dataset_id)
        # 缓存没有、或要求深扫描而现有记录还是浅扫描时，补扫一次
        if meta is None or (deep and meta.scan_depth != "deep"):
            try:
                meta = self._rescan(store, dataset_id, deep=deep)
            except FileNotFoundError as exc:
                raise ServiceError(404, f"dataset {dataset_id} not found") from exc
        return meta.as_dict()

    def scan_datasets(self, dataset_id: str | None = None, deep: bool = False) -> dict[str, Any]:
        store = self.dataset_store()
        root = self._datasets_root()
        if dataset_id:
            if store.get(dataset_id) is None and not (root / dataset_id).is_dir():
                raise ServiceError(404, f"dataset {dataset_id} not found")
            try:
                results = [self._rescan(store, dataset_id, deep=deep)]
            except FileNotFoundError as exc:
                # 目录被删了：扫描器会把它从库里清掉，但要告诉调用方扫不到。
                store.delete(dataset_id)
                raise ServiceError(404, f"dataset {dataset_id} not found") from exc
        else:
            results = store.scan_many(root, None, deep=deep)
        return {
            "scanned": len(results),
            "items": [meta.as_dict() for meta in results],
        }

    def scan_dataset(self, dataset_id: str, deep: bool = False) -> DatasetMeta:
        """重扫单个已注册数据集并返回元数据。未注册返回 404，目录不在返回 404。"""
        store = self.dataset_store()
        if store.get(dataset_id) is None:
            raise ServiceError(404, f"dataset {dataset_id} not found")
        try:
            return self._rescan(store, dataset_id, deep=deep)
        except FileNotFoundError as exc:
            store.delete(dataset_id)
            raise ServiceError(404, f"dataset {dataset_id} not found") from exc

    def _rescan(self, store: DatasetStore, dataset_id: str, deep: bool = False) -> DatasetMeta:
        """按数据集的实际路径重扫，兼容注册到其它允许根目录的数据集。"""
        meta = store.get(dataset_id)
        if meta is not None and meta.path and (Path(meta.path) / "meta" / "info.json").is_file():
            return store.scan_path(Path(meta.path), dataset_id, deep=deep)
        return store.scan_one(self._datasets_root(), dataset_id, deep=deep)

    def register_dataset(self, path: str, dataset_id: str | None = None) -> DatasetMeta:
        """注册一个已存在的数据集目录（仅 admin 可达，鉴权在 API 层）。

        不做任何文件系统写入：验证通过后按实际路径浅扫描一次并落库即可。
        重复 id 返回 409，避免覆盖已有记录。
        """
        try:
            target = resolve_dataset_dir(self.settings, path, dataset_id)
            check_lerobot_format(target.path)
        except UnsafeDatasetPath as exc:
            raise ServiceError(400, str(exc)) from exc

        store = self.dataset_store()
        if store.get(target.dataset_id) is not None:
            raise ServiceError(409, f"dataset {target.dataset_id} already registered")

        # 扫描器若仍判 unusable，照样落库，让列表展示具体 issue。
        meta = store.scan_path(target.path, target.dataset_id)
        self.audit.record(
            "dataset.register",
            actor=self._current_actor,
            target=target.dataset_id,
            detail={"path": str(target.path), "format": meta.format, "status": meta.status},
        )
        return meta


_DETAIL_ONLY_FIELDS = ("action_names", "state_names", "stats")


def _with_path_check(payload: dict[str, Any]) -> dict[str, Any]:
    """目录不在了就把状态改成 missing，并给一句说明。

    列表读 SQLite 缓存，目录被删或挂载盘掉线后缓存可能仍显示 ready。这里只检查
    目录本身，不重新扫描，也不改库；目录回来后下一次扫描会恢复正常状态。
    """
    path = str(payload.get("path") or "")
    status = payload.get("status")
    # 扫描失败的 issue 带有更具体的失败原因，优先保留。
    if not path or status == "error":
        return payload
    if Path(path).is_dir():
        return payload
    checked = dict(payload)
    checked["status"] = "missing"
    issues = list(payload.get("issues") or [])
    if not any(issue.get("code") == "path_missing" for issue in issues):
        issues.insert(
            0,
            {
                "level": "error",
                "code": "path_missing",
                "message": f"数据集目录已不存在：{path}",
            },
        )
    checked["issues"] = issues
    return checked


def _slim_dataset(payload: dict[str, Any]) -> dict[str, Any]:
    """保留列表判断与展示所需字段，去掉详情页使用的长数组。"""
    slim = {key: value for key, value in payload.items() if key not in _DETAIL_ONLY_FIELDS}
    slim["action_dim"] = len(payload.get("action_names") or []) or payload.get("action_dim") or 0
    slim["state_dim"] = len(payload.get("state_names") or []) or payload.get("state_dim") or 0
    slim["has_stats"] = payload.get("stats") is not None
    return slim
