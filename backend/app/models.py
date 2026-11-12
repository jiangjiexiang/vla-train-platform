from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import ROLE_ADMIN, ROLE_USER

JOB_NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
# The whole spec is stored in a Kubernetes annotation, and object annotations are
# capped at 256 KiB, so keep the two unbounded fields well under that.
MAX_COMMAND_BYTES = 64 * 1024
MAX_ENV_BYTES = 64 * 1024
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 契约 1.6：标签键值都要能放进 Kubernetes 注解键的尾部，因此用 DNS-1123 子域规则。
LABEL_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
MAX_LABELS_BYTES = 256
# owner 只用于展示与过滤，不写入容器；限制长度防止 UI/数据库被撑爆。
MAX_OWNER_LENGTH = 128


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    # Pod 建出来了但永远起不来（镜像拉不到等）。和 QUEUED 分开：
    # 排队值得等，这个不值得。
    STARTING_FAILED = "STARTING_FAILED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    # 集群对象已消失，且平台没有记录到可确认的终态。
    GONE = "GONE"


class SubmitSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50)
    image: str = Field(min_length=1)
    gpus: int = Field(gt=0)
    queue: str = "default"
    priority: Literal["low", "normal", "high"] = "normal"
    command: str = Field(min_length=1)
    env: dict[str, str] = Field(default_factory=dict)
    max_retries: Literal[0, 1, 2] = 0
    # How long the training container gets to exit after a cancel. The Kubernetes
    # default is 30s, which is also what makes "cancel then the queued job starts"
    # feel slow; smaller values free the GPU sooner but risk losing unsaved state.
    termination_grace_period_seconds: int = Field(default=30, ge=1, le=600)
    # --- v0.9 新增：全部带默认值，读 v0.8 老任务/老历史时不会抛 ValidationError ---
    # owner 为空串表示「v0.8 老任务，没有提交者信息」。
    owner: str = Field(default="", max_length=MAX_OWNER_LENGTH)
    labels: dict[str, str] = Field(default_factory=dict)
    nodes: int = Field(default=1, ge=1)
    gpus_per_node: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not JOB_NAME_PATTERN.fullmatch(value):
            raise ValueError("name must contain only lowercase letters, digits and internal hyphens")
        return value

    @field_validator("image", "command")
    @classmethod
    def validate_non_blank(cls, value: str | None) -> str | None:
        # SubmitRequest makes these fields optional; an explicit null from the
        # client means "use the platform default", not "invalid value".
        if value is None:
            return None
        if not value.strip() or "\x00" in value:
            raise ValueError("value must not be blank or contain NUL characters")
        return value

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: dict[str, str]) -> dict[str, str]:
        # 键值规则在模型层就拒绝（422）；总字节数只在提交时校验，见 check_labels。
        for key, item in value.items():
            check_label_name(key, "label key")
            check_label_name(item, "label value")
        return value

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not ENV_NAME_PATTERN.fullmatch(key):
                raise ValueError(f"invalid environment variable name: {key}")
            if "\x00" in item:
                raise ValueError(f"environment variable {key} contains a NUL character")
        return value


class SubmitRequest(SubmitSpec):
    image: str | None = None
    command: str | None = None
    # 单机时 gpus 必填；多机（nodes > 1）时可由服务端按 nodes * gpus_per_node 计算。
    gpus: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_topology(self) -> SubmitRequest:
        if self.nodes > 1:
            if self.gpus_per_node is None:
                raise ValueError("gpus_per_node is required when nodes > 1")
        elif self.gpus is None and self.gpus_per_node is None:
            raise ValueError("gpus is required")
        return self

    def with_defaults(self, default_image: str, default_command: str) -> SubmitSpec:
        values = self.model_dump()
        values["image"] = self.image or default_image
        values["command"] = self.command or default_command
        if values["gpus"] is None:
            # gpus_per_node 此时必然存在：单机且没给 gpus 校验就不放行。
            values["gpus"] = self.nodes * int(values["gpus_per_node"] or 0)
        return SubmitSpec.model_validate(values)


class CloneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not JOB_NAME_PATTERN.fullmatch(value):
            raise ValueError("name must contain only lowercase letters, digits and internal hyphens")
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


class QuotaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # null 表示不限。
    max_gpus: int | None = Field(default=None, ge=0)
    max_active_jobs: int | None = Field(default=None, ge=0)


def check_payload_size(spec: SubmitSpec) -> None:
    """Reject specs too large to store in a Kubernetes annotation.

    This runs on submit only, never when reading an object or history back, so a
    job created before the limit existed can still be inspected and cancelled.
    """
    if len(spec.command.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise ValueError(f"command must not exceed {MAX_COMMAND_BYTES} bytes")
    total = sum(
        len(key.encode("utf-8")) + len(value.encode("utf-8")) for key, value in spec.env.items()
    )
    if total > MAX_ENV_BYTES:
        raise ValueError(f"env entries must not exceed {MAX_ENV_BYTES} bytes in total")


def check_label_name(value: str, field: str = "label") -> str:
    if not LABEL_PATTERN.fullmatch(value) or len(value) > 63:
        raise ValueError(
            f"{field} must match {LABEL_PATTERN.pattern} and be at most 63 characters"
        )
    return value


def check_labels(labels: dict[str, str]) -> None:
    """提交时校验自由标签：键值规则一致，总长（UTF-8）不超过 256 字节。"""
    total = 0
    for key, value in labels.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("label keys and values must be strings")
        check_label_name(key, "label key")
        check_label_name(value, "label value")
        total += len(key.encode("utf-8")) + len(value.encode("utf-8"))
    if total > MAX_LABELS_BYTES:
        raise ValueError(f"labels must not exceed {MAX_LABELS_BYTES} bytes in total")


def normalize_role(value: str | None) -> str:
    return ROLE_ADMIN if value == ROLE_ADMIN else ROLE_USER


def spec_from_stored_json(payload: str) -> SubmitSpec:
    """读历史库里的老 spec：丢弃本版本不认识的键，缺字段走默认值。

    ``SubmitSpec`` 是 extra="forbid"，直接 model_validate_json 一个带未来字段
    的老记录会整体失败，进而让任务详情/取消都读不出来。读取路径必须宽容。
    """
    data: Any = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("stored spec is not an object")
    known = set(SubmitSpec.model_fields)
    return SubmitSpec.model_validate({key: value for key, value in data.items() if key in known})


class DatasetRegisterRequest(BaseModel):
    """注册一个已存在的数据集目录（仅 admin）。

    路径是否合法（在允许的根目录内、是 LeRobot 数据集）由服务层校验，
    这里只拦明显非法的输入形状。
    """

    model_config = ConfigDict(extra="forbid")

    # 宿主机路径和容器内路径都行：Web 容器与宿主机挂的是同一个 /data/vla-platform。
    path: str = Field(min_length=1, max_length=1024)
    # 不给就用目录名，与启动扫描得到的结果保持一致。
    id: str | None = Field(default=None, min_length=1, max_length=100)


class DatasetScanRequest(BaseModel):
    """触发数据集扫描。id 为空表示扫描全部。"""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, max_length=100)
    deep: bool = False


class RunSubmitRequest(BaseModel):
    """一键训练请求，由 API 与业务层共用，确保非法输入统一返回 HTTP 422。"""

    model_config = ConfigDict(extra="forbid")

    recipe_id: str = Field(min_length=1, max_length=64)
    dataset_id: str = Field(min_length=1, max_length=100)
    # 省略时自动生成 <recipe>-<dataset>-<序号>
    name: str | None = Field(default=None, max_length=50)
    # L2/L3 参数覆盖；键名是参数模板里的 key
    overrides: dict[str, Any] = Field(default_factory=dict)
    priority: str = "normal"
    # 额外的环境变量（高级用途），会覆盖配方里的 env
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_run_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not JOB_NAME_PATTERN.fullmatch(value):
            raise ValueError("任务名只能包含小写字母、数字和中划线，且不能以中划线开头或结尾")
        return value

    @field_validator("priority")
    @classmethod
    def validate_run_priority(cls, value: str) -> str:
        if value not in ("low", "normal", "high"):
            raise ValueError("优先级必须是 low / normal / high")
        return value


RunRequest = RunSubmitRequest
