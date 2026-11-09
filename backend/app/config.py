from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    api_token: str
    auth_required: bool = True
    namespace: str = "vla-training"
    queue: str = "default"
    cluster_queue: str = "vla-cluster-queue"
    allowed_gpus: tuple[int, ...] = (1,)
    default_image: str = "vla-smoke:latest"
    default_command: str = "python /app/train.py"
    dataset_host_path: str = "/data/vla-platform/datasets"
    checkpoint_host_path: str = "/data/vla-platform/checkpoints"
    db_path: str = "/data/history/platform.db"
    kubeconfig: str | None = None
    log_tail_default: int = 500
    gpu_metrics_url: str = "http://vla-gpu-metrics:8080/"
    # The metrics endpoint is served by a single-connection `nc` process, so
    # concurrent dashboard polls queue up behind it. A short cache keeps p95
    # flat without making the numbers visibly stale.
    gpu_metrics_ttl: float = 2.0
    static_dir: str = str(Path(__file__).resolve().parent.parent / "static")

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("PLATFORM_API_TOKEN", "").strip()
        auth_required = _parse_bool(os.environ.get("PLATFORM_AUTH_REQUIRED", "true"), "PLATFORM_AUTH_REQUIRED")
        if auth_required and not token:
            raise RuntimeError("PLATFORM_API_TOKEN is required when PLATFORM_AUTH_REQUIRED is true")

        allowed_gpus = _parse_gpu_list(os.environ.get("PLATFORM_ALLOWED_GPUS", "1"))
        log_tail = _parse_positive_int(os.environ.get("PLATFORM_LOG_TAIL_DEFAULT", "500"), "PLATFORM_LOG_TAIL_DEFAULT")
        if log_tail > 10000:
            raise RuntimeError("PLATFORM_LOG_TAIL_DEFAULT must not exceed 10000")

        return cls(
            api_token=token,
            auth_required=auth_required,
            namespace=os.environ.get("PLATFORM_NAMESPACE", "vla-training"),
            queue=os.environ.get("PLATFORM_QUEUE", "default"),
            allowed_gpus=allowed_gpus,
            default_image=os.environ.get("PLATFORM_DEFAULT_IMAGE", "vla-smoke:latest"),
            default_command=os.environ.get("PLATFORM_DEFAULT_COMMAND", "python /app/train.py"),
            dataset_host_path=os.environ.get("PLATFORM_DATASET_HOST_PATH", "/data/vla-platform/datasets"),
            checkpoint_host_path=os.environ.get("PLATFORM_CHECKPOINT_HOST_PATH", "/data/vla-platform/checkpoints"),
            db_path=os.environ.get("PLATFORM_DB_PATH", "/data/history/platform.db"),
            kubeconfig=os.environ.get("PLATFORM_KUBECONFIG") or None,
            log_tail_default=log_tail,
            gpu_metrics_url=os.environ.get("PLATFORM_GPU_METRICS_URL", "http://vla-gpu-metrics:8080/"),
            gpu_metrics_ttl=_parse_nonnegative_float(
                os.environ.get("PLATFORM_GPU_METRICS_TTL", "2.0"), "PLATFORM_GPU_METRICS_TTL"
            ),
            static_dir=os.environ.get("PLATFORM_STATIC_DIR", str(Path(__file__).resolve().parent.parent / "static")),
        )


def _parse_bool(raw: str, name: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _parse_gpu_list(raw: str) -> tuple[int, ...]:
    try:
        values = tuple(dict.fromkeys(int(item.strip()) for item in raw.split(",")))
    except ValueError as exc:
        raise RuntimeError("PLATFORM_ALLOWED_GPUS must be a comma-separated list of integers") from exc
    if not values or any(value <= 0 for value in values):
        raise RuntimeError("PLATFORM_ALLOWED_GPUS must contain positive integers")
    return values


def _parse_positive_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _parse_nonnegative_float(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value < 0:
        raise RuntimeError(f"{name} must not be negative")
    return value
