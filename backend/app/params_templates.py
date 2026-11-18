"""训练参数模板（v0.9.1）。

每个训练镜像可以登记一份模板，声明它支持哪些参数、命令行 flag 叫什么、默认值多少。
提交页据此动态渲染表单，并把取值拼进命令行。没有登记的镜像回退到 ``BUILTIN_TEMPLATE``，
也就是 v0.9.0 里写死的那套通用参数，保证老用户习惯不被打断。

设计契约见 ``.agents/PARAMS-TEMPLATE-CONTRACT.md``（仓库内副本 ``docs`` 相关章节）。
本模块只做「描述与校验」，不做命令行拼装——拼装在 ``services/params_render.py``。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_PARAMS = 50
MAX_TEMPLATE_BYTES = 64 * 1024
MAX_OPTIONS = 50

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
# flag 允许 --a-b、--a_b、--config.name 这类形式（openpi 用点号分段）
FLAG_PATTERN = re.compile(r"^(?:--[A-Za-z0-9][-A-Za-z0-9_.]*|[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*=)$")

# 镜像名：name[:tag][@sha256:digest]，允许 registry/namespace 层
# 镜像名：registry[:port]/path/name[:tag][@sha256:digest]
# registry 端口只出现在第一个路径段（harbor.corp:5000/a/b），因此把「主机段」单独拆出来匹配。
IMAGE_PATTERN = re.compile(
    r"^(?:[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?/)?"  # 可选 registry[:port]/
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*"                        # 镜像名（最后一段）
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"                  # 中间可选的多级 namespace
    r"(?::[A-Za-z0-9._-]{1,128})?"                         # 可选 tag
    r"(?:@sha256:[a-f0-9]{64})?$"                          # 可选 digest
)
MAX_IMAGE_LENGTH = 255

ParamType = Literal["int", "float", "str", "bool", "enum", "path"]


class ParamSpec(BaseModel):
    """单个训练参数的声明。"""

    model_config = ConfigDict(extra="forbid")

    key: str
    # flag 可以为空：内置模板用 null flag + kind="extra" 表示「透传参数」
    flag: str | None = None
    label: str = Field(min_length=1, max_length=40)
    type: ParamType
    default: Any = None
    min: float | None = None
    max: float | None = None
    required: bool = False
    help: str | None = Field(default=None, max_length=200)
    placeholder: str | None = Field(default=None, max_length=100)
    options: list[str] | None = None
    true_value: str | None = None
    false_value: str | None = None
    # 仅内置模板的兼容字段：flag 为 null 时，值走 extra_args 透传通道
    kind: Literal["extra"] | None = None

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not KEY_PATTERN.fullmatch(value):
            raise ValueError(f"参数 key 须匹配 ^[a-z][a-z0-9_]*$，收到 {value!r}")
        return value

    @field_validator("flag")
    @classmethod
    def validate_flag(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not FLAG_PATTERN.fullmatch(value):
            raise ValueError(f"参数 flag 须匹配 ^--[A-Za-z0-9][-A-Za-z0-9_.]*$，收到 {value!r}")
        return value

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("参数 label 不能为空白")
        return value

    @model_validator(mode="after")
    def validate_consistency(self) -> ParamSpec:  # noqa: PYI034 - pydantic 需要实例返回
        """类型与默认值、选项、min/max 的一致性。"""
        # 内置透传参数允许 flag 为 None；其余参数必须有 flag
        if self.flag is None and self.kind != "extra":
            raise ValueError(f"参数 {self.key} 缺少 flag（只有 kind='extra' 的内置参数可以没有）")

        if self.type == "enum":
            if not self.options:
                raise ValueError(f"参数 {self.key} 的类型是 enum，必须提供 options")
            if len(self.options) > MAX_OPTIONS:
                raise ValueError(f"参数 {self.key} 的 options 不能超过 {MAX_OPTIONS} 项")
            if self.default is not None and self.default not in self.options:
                raise ValueError(f"参数 {self.key} 的 default 不在 options 里")
        elif self.options is not None:
            raise ValueError(f"参数 {self.key} 只有 type='enum' 才能设置 options")

        if self.min is not None or self.max is not None:
            if self.type not in ("int", "float"):
                raise ValueError(f"参数 {self.key} 只有 int/float 才能设置 min/max")
            if self.min is not None and self.max is not None and self.min > self.max:
                raise ValueError(f"参数 {self.key} 的 min 不能大于 max")

        if self.default is not None:
            self._check_default_type()

        if self.type == "bool":
            # true/false_value 只对 bool 有意义
            pass
        elif self.true_value is not None or self.false_value is not None:
            raise ValueError(f"参数 {self.key} 只有 type='bool' 才能设置 true_value/false_value")
        return self

    def _check_default_type(self) -> None:
        value = self.default
        key = self.key
        if self.type == "int":
            # bool 是 int 的子类，要显式排除；浮点整数也拒绝，避免默认值语义含糊
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"参数 {key} 的类型是 int，default 必须是整数，收到 {value!r}")
            if self.min is not None and value < self.min:
                raise ValueError(f"参数 {key} 的 default 小于 min")
            if self.max is not None and value > self.max:
                raise ValueError(f"参数 {key} 的 default 大于 max")
        elif self.type == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"参数 {key} 的类型是 float，default 必须是数字，收到 {value!r}")
            if self.min is not None and value < self.min:
                raise ValueError(f"参数 {key} 的 default 小于 min")
            if self.max is not None and value > self.max:
                raise ValueError(f"参数 {key} 的 default 大于 max")
        elif self.type in ("str", "path"):
            if not isinstance(value, str):
                raise ValueError(f"参数 {key} 的类型是 {self.type}，default 必须是字符串，收到 {value!r}")
        elif self.type == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"参数 {key} 的类型是 bool，default 必须是 true/false，收到 {value!r}")
        elif self.type == "enum":
            if not isinstance(value, str):
                raise ValueError(f"参数 {key} 的类型是 enum，default 必须是字符串，收到 {value!r}")


class ExtraArgsSpec(BaseModel):
    """「其他参数」输入框的配置：用户填的内容原样追加到命令行末尾。"""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="其他参数", max_length=40)
    help: str | None = Field(default=None, max_length=200)
    enabled: bool = True
    default: str | None = None


class ParamsTemplate(BaseModel):
    """镜像的完整参数模板。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    entrypoint: str | None = None
    params: list[ParamSpec] = Field(default_factory=list, max_length=MAX_PARAMS)
    extra_args: ExtraArgsSpec | None = None

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"只支持 schema_version=1，收到 {value}")
        return value

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("entrypoint 不能为空白")
        if "\x00" in value:
            raise ValueError("entrypoint 不能包含 NUL 字符")
        return value

    @model_validator(mode="after")
    def validate_unique_keys(self) -> ParamsTemplate:  # noqa: PYI034
        seen: set[str] = set()
        for param in self.params:
            if param.key in seen:
                raise ValueError(f"参数 key 重复：{param.key}")
            seen.add(param.key)
        return self


def validate_image_name(image: str) -> bool:
    """镜像名是否合法（含可选的 tag / digest）。"""
    if not image or len(image) > MAX_IMAGE_LENGTH:
        return False
    return bool(IMAGE_PATTERN.fullmatch(image))


def parse_template(payload: Any) -> ParamsTemplate:
    """校验并返回模板；错误信息里带上参数下标，便于定位。"""
    if not isinstance(payload, dict):
        raise ValueError("模板必须是 JSON 对象")
    params = payload.get("params")
    if isinstance(params, list):
        keys: set[str] = set()
        for index, item in enumerate(params):
            if not isinstance(item, dict):
                raise ValueError(f"第 {index + 1} 个参数必须是 JSON 对象")
            key = item.get("key")
            if isinstance(key, str):
                if not KEY_PATTERN.fullmatch(key):
                    raise ValueError(f"第 {index + 1} 个参数的 key 须匹配 ^[a-z][a-z0-9_]*$，收到 {key!r}")
                if key in keys:
                    raise ValueError(f"第 {index + 1} 个参数的 key 重复：{key}")
                keys.add(key)
    try:
        return ParamsTemplate.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
        raise ValueError(_readable_pydantic_error(exc)) from exc


def _readable_pydantic_error(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        parts = []
        for item in errors():
            loc = ".".join(str(part) for part in item.get("loc", ()))
            parts.append(f"{loc}: {item.get('msg')}" if loc else str(item.get("msg")))
        if parts:
            return "；".join(parts[:5])
    return str(exc)


# --------------------------------------------------------------------------
# 内置通用模板：与前端 utils/buildCommand.ts 的 BUILTIN_PARAMS_TEMPLATE 必须一致
# --------------------------------------------------------------------------
BUILTIN_TEMPLATE: dict[str, Any] = {
    "schema_version": 1,
    "entrypoint": None,
    "params": [
        {"key": "train_steps", "flag": "--steps", "label": "训练步数", "type": "int", "placeholder": "如 1000"},
        {"key": "batch_size", "flag": "--batch-size", "label": "batch size", "type": "int", "placeholder": "如 8"},
        {"key": "grad_accum", "flag": "--grad-accum", "label": "梯度累积", "type": "int", "placeholder": "如 4"},
        {"key": "learning_rate", "flag": "--lr", "label": "学习率", "type": "str", "placeholder": "如 1e-4"},
        {
            "key": "save_every",
            "flag": "--save-freq",
            "label": "保存 checkpoint 间隔（步）",
            "type": "int",
            "placeholder": "如 500",
        },
        {
            "key": "log_every",
            "flag": "--log-freq",
            "label": "打印日志间隔（步）",
            "type": "int",
            "placeholder": "如 10",
        },
        {
            "key": "num_workers",
            "flag": "--num-workers",
            "label": "数据加载进程数",
            "type": "int",
            "placeholder": "如 4",
        },
        {
            "key": "dtype",
            "flag": "--dtype",
            "label": "精度",
            "type": "enum",
            "options": ["bfloat16", "float16", "float32"],
        },
        {"key": "extra_args", "flag": None, "label": "其他参数", "type": "str", "kind": "extra"},
    ],
    "extra_args": {"label": "其他参数", "enabled": True},
}

# 内置模板自身必须合法：模块导入时校验一次，改错了会立刻在测试里暴露
BUILTIN_TEMPLATE_MODEL = ParamsTemplate.model_validate(BUILTIN_TEMPLATE)