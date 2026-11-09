from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

JOB_NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
# The whole spec is stored in a Kubernetes annotation, and object annotations are
# capped at 256 KiB, so keep the two unbounded fields well under that.
MAX_COMMAND_BYTES = 64 * 1024
MAX_ENV_BYTES = 64 * 1024
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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

    def with_defaults(self, default_image: str, default_command: str) -> SubmitSpec:
        values = self.model_dump()
        values["image"] = self.image or default_image
        values["command"] = self.command or default_command
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
