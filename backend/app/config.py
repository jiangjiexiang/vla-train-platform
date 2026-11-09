from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

AUTH_MODES = ("off", "token", "ldap")
ROLE_ADMIN = "admin"
ROLE_USER = "user"


@dataclass(frozen=True)
class Settings:
    api_token: str
    # 兼容 v0.8：PLATFORM_AUTH_REQUIRED=false 等价于 auth_mode="off"。
    # 保留该字段是因为既有调用方（测试、部署清单）仍在读它。
    auth_required: bool = True
    # off / token / ldap，默认 token（v0.8 行为）。
    auth_mode: str = "token"
    session_ttl_hours: int = 12
    ldap_url: str = ""
    ldap_base_dn: str = ""
    ldap_user_template: str = "uid={username},{base_dn}"
    ldap_group_dn_admin: str = ""
    # 可选：只读复查会话用的服务账号。不配则匿名查询（能匿名搜 memberOf 即可）。
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_user_whitelist: tuple[str, ...] = ()
    ldap_timeout_seconds: float = 5.0
    default_max_gpus: int | None = None
    default_max_active_jobs: int | None = None
    max_nodes: int = 1
    namespace: str = "vla-training"
    queue: str = "default"
    cluster_queue: str = "vla-cluster-queue"
    allowed_gpus: tuple[int, ...] = (1,)
    default_image: str = "vla-smoke:latest"
    default_command: str = "python /app/train.py"
    dataset_host_path: str = "/data/vla-platform/datasets"
    # Web 容器内看到数据集的路径。Web Pod 挂载了同一个 hostPath，
    # 扫描器在这里读 meta/info.json（见 deploy/web 的 volumeMounts）。
    datasets_mount_path: str = "/datasets"
    checkpoint_host_path: str = "/data/vla-platform/checkpoints"
    db_path: str = "/data/history/platform.db"
    kubeconfig: str | None = None
    log_tail_default: int = 500
    gpu_metrics_url: str = "http://vla-gpu-metrics:8080/"
    # The metrics endpoint is served by a single-connection `nc` process, so
    # concurrent dashboard polls queue up behind it. A short cache keeps p95
    # flat without making the numbers visibly stale.
    gpu_metrics_ttl: float = 2.0
    # 集群总览缓存。总览页轮询与提交前预检都会读它，加短 TTL 挡掉重复调用。
    cluster_summary_ttl: float = 2.0
    # 任务结束后多久回收 K8s 对象（秒）。产物在 /checkpoints 下独立保存，
    # 删对象不影响结果。0 表示不回收（保留全部历史对象）。
    job_ttl_seconds: int = 3600
    # 多用户令牌表（token 模式）："用户名:角色:令牌,..."。
    # 为空时退回单一 PLATFORM_API_TOKEN（等价于一个 admin），
    # 保证老部署升级后行为不变。
    user_tokens: str = ""
    static_dir: str = str(Path(__file__).resolve().parent.parent / "static")

    def __post_init__(self) -> None:
        # 直接构造 Settings(auth_required=False) 且未显式给 auth_mode 时，等同于 off；
        # 否则 auth_required 与 auth_mode 会出现两种互相矛盾的表示。
        if not self.auth_required and self.auth_mode == "token":
            object.__setattr__(self, "auth_mode", "off")

    @property
    def auth_enabled(self) -> bool:
        return self.auth_mode != "off"

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("PLATFORM_API_TOKEN", "").strip()
        auth_required = _parse_bool(os.environ.get("PLATFORM_AUTH_REQUIRED", "true"), "PLATFORM_AUTH_REQUIRED")
        auth_mode = _parse_auth_mode(os.environ.get("PLATFORM_AUTH_MODE"), auth_required)
        if auth_mode == "token" and not token:
            # 启动即失败，避免上线后所有请求都 401 才发现令牌没配。
            raise RuntimeError("PLATFORM_API_TOKEN is required when PLATFORM_AUTH_REQUIRED is true")

        allowed_gpus = _parse_gpu_list(os.environ.get("PLATFORM_ALLOWED_GPUS", "1"))
        log_tail = _parse_positive_int(os.environ.get("PLATFORM_LOG_TAIL_DEFAULT", "500"), "PLATFORM_LOG_TAIL_DEFAULT")
        if log_tail > 10000:
            raise RuntimeError("PLATFORM_LOG_TAIL_DEFAULT must not exceed 10000")

        ldap_url = os.environ.get("PLATFORM_LDAP_URL", "").strip()
        ldap_base_dn = os.environ.get("PLATFORM_LDAP_BASE_DN", "").strip()
        ldap_user_template = os.environ.get("PLATFORM_LDAP_USER_TEMPLATE", "").strip()
        if ldap_user_template and "{username}" not in ldap_user_template:
            raise RuntimeError("PLATFORM_LDAP_USER_TEMPLATE must contain the {username} placeholder")
        if auth_mode == "ldap" and (not ldap_url or not ldap_base_dn):
            raise RuntimeError("PLATFORM_LDAP_URL and PLATFORM_LDAP_BASE_DN are required when PLATFORM_AUTH_MODE=ldap")

        return cls(
            api_token=token,
            auth_required=auth_required,
            auth_mode=auth_mode,
            session_ttl_hours=_parse_positive_int(
                os.environ.get("PLATFORM_SESSION_TTL_HOURS", "12"), "PLATFORM_SESSION_TTL_HOURS"
            ),
            ldap_url=ldap_url,
            ldap_base_dn=ldap_base_dn,
            ldap_user_template=ldap_user_template or "uid={username},{base_dn}",
            ldap_group_dn_admin=os.environ.get("PLATFORM_LDAP_GROUP_DN_ADMIN", "").strip(),
            ldap_bind_dn=os.environ.get("PLATFORM_LDAP_BIND_DN", "").strip(),
            ldap_bind_password=os.environ.get("PLATFORM_LDAP_BIND_PASSWORD", ""),
            ldap_user_whitelist=_parse_csv(os.environ.get("PLATFORM_LDAP_USER_WHITELIST", "")),
            ldap_timeout_seconds=_parse_positive_float(
                os.environ.get("PLATFORM_LDAP_TIMEOUT_SECONDS", "5"), "PLATFORM_LDAP_TIMEOUT_SECONDS"
            ),
            default_max_gpus=_parse_optional_nonnegative_int(
                os.environ.get("PLATFORM_DEFAULT_MAX_GPUS"), "PLATFORM_DEFAULT_MAX_GPUS"
            ),
            default_max_active_jobs=_parse_optional_nonnegative_int(
                os.environ.get("PLATFORM_DEFAULT_MAX_ACTIVE_JOBS"), "PLATFORM_DEFAULT_MAX_ACTIVE_JOBS"
            ),
            max_nodes=_parse_positive_int(os.environ.get("PLATFORM_MAX_NODES", "1"), "PLATFORM_MAX_NODES"),
            namespace=os.environ.get("PLATFORM_NAMESPACE", "vla-training"),
            queue=os.environ.get("PLATFORM_QUEUE", "default"),
            allowed_gpus=allowed_gpus,
            default_image=os.environ.get("PLATFORM_DEFAULT_IMAGE", "vla-smoke:latest"),
            default_command=os.environ.get("PLATFORM_DEFAULT_COMMAND", "python /app/train.py"),
            dataset_host_path=os.environ.get("PLATFORM_DATASET_HOST_PATH", "/data/vla-platform/datasets"),
            datasets_mount_path=os.environ.get("PLATFORM_DATASETS_MOUNT_PATH", "/datasets"),
            checkpoint_host_path=os.environ.get("PLATFORM_CHECKPOINT_HOST_PATH", "/data/vla-platform/checkpoints"),
            db_path=os.environ.get("PLATFORM_DB_PATH", "/data/history/platform.db"),
            kubeconfig=os.environ.get("PLATFORM_KUBECONFIG") or None,
            log_tail_default=log_tail,
            gpu_metrics_url=os.environ.get("PLATFORM_GPU_METRICS_URL", "http://vla-gpu-metrics:8080/"),
            user_tokens=os.environ.get("PLATFORM_USER_TOKENS", ""),
            job_ttl_seconds=_parse_nonnegative_int(
                os.environ.get("PLATFORM_JOB_TTL_SECONDS", "3600"), "PLATFORM_JOB_TTL_SECONDS"
            ),
            cluster_summary_ttl=_parse_nonnegative_float(
                os.environ.get("PLATFORM_CLUSTER_SUMMARY_TTL", "2.0"), "PLATFORM_CLUSTER_SUMMARY_TTL"
            ),
            gpu_metrics_ttl=_parse_nonnegative_float(
                os.environ.get("PLATFORM_GPU_METRICS_TTL", "2.0"), "PLATFORM_GPU_METRICS_TTL"
            ),
            static_dir=os.environ.get("PLATFORM_STATIC_DIR", str(Path(__file__).resolve().parent.parent / "static")),
        )


def _parse_auth_mode(raw: str | None, auth_required: bool) -> str:
    """PLATFORM_AUTH_MODE 优先；未设置时从 v0.8 的 PLATFORM_AUTH_REQUIRED 推导。"""
    if raw is None or not raw.strip():
        return "token" if auth_required else "off"
    value = raw.strip().lower()
    if value not in AUTH_MODES:
        raise RuntimeError(f"PLATFORM_AUTH_MODE must be one of: {', '.join(AUTH_MODES)}")
    return value


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


def _parse_csv(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def _parse_optional_nonnegative_int(raw: str | None, name: str) -> int | None:
    """空字符串表示不限（None），与契约「默认空=不限」一致。"""
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 0:
        raise RuntimeError(f"{name} must not be negative")
    return value


def _parse_positive_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _parse_positive_float(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
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

def _parse_nonnegative_int(raw: str, name: str) -> int:
    """解析非负整数环境变量，非法值直接报错而不是静默用默认值。"""
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")
    return value
