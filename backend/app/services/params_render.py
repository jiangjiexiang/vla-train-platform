"""训练参数模板 → 命令行拼装。

这是契约第 3 节的**后端实现**，必须与前端 ``frontend/src/utils/buildCommand.ts``
逐字节一致：前端用它做「最终命令预览」，后端用它生成真正执行的命令，两边不一致
会让用户看到的预览和实际跑的命令不同，这是最难排查的一类问题。

两边的对齐点：
- 空值判定：``None`` / 缺失 / 空字符串都视为未填写
- 未填写时回退模板 ``default``；``default`` 也没有则跳过该参数
- ``bool``：true 时 ``true_value`` 是字符串就拼 ``flag true_value``，否则只拼 flag；
  false 时 ``false_value`` 是字符串就拼值，否则跳过
- 其余类型一律 ``flag + " " + shell_quote(str(value))``
- shell_quote：匹配 ``^[A-Za-z0-9_@%+=:,./-]+$`` 原样返回，否则单引号包裹且 ``'`` → ``'\\''``
- ``extra_args``：原样追加，不加引号、不解析

任何改动都要同步改前端并更新两边的测试。
"""

from __future__ import annotations

import re
from typing import Any

from ..params_templates import ParamsTemplate

SAFE_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")


def shell_quote(value: str) -> str:
    """按 POSIX shell 规则给单个参数加引号。"""
    if SAFE_VALUE_PATTERN.fullmatch(value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def is_blank(value: Any) -> bool:
    """契约 3 的「空值」：None / 缺失 / 空字符串。"""
    return value is None or value == ""


def _stringify(value: Any) -> str:
    """把参数值转成字符串，与前端 ``pythonStr`` 对齐。

    bool 不会走到这里（bool 参数单独处理）。浮点数用 Python 的 ``str()``，
    它给出最短往返表示，与 JS 的 ``String(number)`` 在正常范围内一致。
    """
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float) and value.is_integer():
        # JS 的 String(2.0) 是 "2"，Python 的 str(2.0) 是 "2.0"：对齐前端
        return str(int(value))
    return str(value)


def _resolve_value(param: Any, values: dict[str, Any]) -> tuple[bool, Any]:
    """返回 (是否参与拼接, 生效值)。用户填的优先，未填则回退 default。"""
    if param.key in values:
        provided = values[param.key]
        if not is_blank(provided):
            return True, provided
    if param.default is not None and not is_blank(param.default):
        return True, param.default
    return False, None


def render_bool(param: Any, value: Any) -> str | None:
    """bool 参数的 token；None 表示该参数不出现在命令行里。"""
    flag = param.flag or ""
    if bool(value):
        if isinstance(param.true_value, str):
            return f"{flag} {param.true_value}"
        return flag
    if isinstance(param.false_value, str):
        return f"{flag} {param.false_value}"
    return None


def build_param_tokens(template: ParamsTemplate, values: dict[str, Any] | None = None) -> list[str]:
    """按 params 数组顺序生成 token。"""
    values = values or {}
    tokens: list[str] = []
    for param in template.params:
        # 内置模板的透传参数（flag 为空）不单独拼，它的值走 extra_args 通道
        if not param.flag:
            continue
        present, value = _resolve_value(param, values)
        if not present:
            continue
        if param.type == "bool":
            token = render_bool(param, value)
            if token is not None:
                tokens.append(token)
            continue
        sep = "" if param.flag.endswith("=") else " "
        tokens.append(f"{param.flag}{sep}{shell_quote(_stringify(value))}")
    return tokens


def resolve_extra_args(template: ParamsTemplate, values: dict[str, Any]) -> str:
    """「其他参数」输入框的内容；原样返回，不做任何处理。"""
    spec = template.extra_args
    if spec is None or spec.enabled is False:
        return ""
    provided = values.get("extra_args")
    value = provided if not is_blank(provided) else spec.default
    if is_blank(value):
        return ""
    text = str(value)
    return "" if not text.strip() else text


def resolve_base_command(
    *,
    user_command: str | None,
    entrypoint: str | None,
    default_command: str | None,
) -> str:
    """基础命令优先级：用户手填 > 模板 entrypoint > 平台默认命令。"""
    for candidate in (user_command, entrypoint, default_command):
        text = (candidate or "").strip()
        if text:
            return text
    return ""


def build_command(
    template: ParamsTemplate,
    values: dict[str, Any] | None = None,
    *,
    user_command: str | None,
    default_command: str | None = None,
) -> str:
    """拼出最终命令行。"""
    values = values or {}
    base = resolve_base_command(
        user_command=user_command,
        entrypoint=template.entrypoint,
        default_command=default_command,
    )
    parts: list[str] = []
    if base:
        parts.append(base)
    parts.extend(build_param_tokens(template, values))
    extra = resolve_extra_args(template, values)
    if extra:
        parts.append(extra)
    return " ".join(parts)