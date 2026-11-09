"""训练参数模板（v0.9.1）：模型校验、初始化、渲染一致性。"""

from __future__ import annotations

import pytest
from app.params_templates import (
    BUILTIN_TEMPLATE,
    BUILTIN_TEMPLATE_MODEL,
    MAX_PARAMS,
    ParamsTemplate,
    parse_template,
    validate_image_name,
)
from app.services.params_render import (
    build_command,
    build_param_tokens,
    resolve_base_command,
    shell_quote,
)

BASE = "python /app/train.py"


def make_template(**overrides):
    payload = {
        "schema_version": 1,
        "entrypoint": BASE,
        "params": [
            {"key": "steps", "flag": "--steps", "label": "训练步数", "type": "int", "default": 1000},
            {"key": "batch_size", "flag": "--batch_size", "label": "batch size", "type": "int", "default": 8},
            {"key": "dtype", "flag": "--dtype", "label": "精度", "type": "enum", "default": "bfloat16",
             "options": ["bfloat16", "float16"]},
        ],
    }
    payload.update(overrides)
    return parse_template(payload)


# --------------------------------------------------------------------------
# 镜像名校验
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "image",
    [
        "vla-smoke:latest",
        "vla-smoke",
        "docker.io/library/vla-train:0.9.1",
        "harbor.corp/vla/pi05:v1.2.3",
        "registry.local:5000/a/b/c:tag",
        "mini@sha256:" + "a" * 64,
    ],
)
def test_validate_image_name_accepts(image):
    assert validate_image_name(image) is True


@pytest.mark.parametrize(
    "image",
    ["", "VLA:latest", "vla:", "-vla", "vla::latest", "vla name", "a" * 256],
)
def test_validate_image_name_rejects(image):
    assert validate_image_name(image) is False


# --------------------------------------------------------------------------
# 模板校验
# --------------------------------------------------------------------------
def test_builtin_template_is_valid():
    """内置模板自身必须通过校验，否则前端回退会拿到非法模板。"""
    assert BUILTIN_TEMPLATE_MODEL.schema_version == 1
    assert len(BUILTIN_TEMPLATE_MODEL.params) == len(BUILTIN_TEMPLATE["params"])


def test_parse_template_accepts_minimal():
    template = make_template()
    assert [param.key for param in template.params] == ["steps", "batch_size", "dtype"]


def test_parse_template_rejects_wrong_schema_version():
    with pytest.raises(ValueError, match="schema_version"):
        parse_template({"schema_version": 2, "params": []})


def test_parse_template_reports_duplicate_key_with_index():
    with pytest.raises(ValueError, match="重复"):
        parse_template(
            {
                "schema_version": 1,
                "params": [
                    {"key": "a", "flag": "--a", "label": "A", "type": "int"},
                    {"key": "a", "flag": "--b", "label": "A2", "type": "int"},
                ],
            }
        )


def test_parse_template_rejects_bad_flag():
    with pytest.raises(ValueError):
        parse_template(
            {"schema_version": 1, "params": [{"key": "a", "flag": "-a", "label": "A", "type": "int"}]}
        )


def test_parse_template_rejects_enum_without_options():
    with pytest.raises(ValueError, match="options"):
        parse_template(
            {"schema_version": 1, "params": [{"key": "a", "flag": "--a", "label": "A", "type": "enum"}]}
        )


def test_parse_template_rejects_default_type_mismatch():
    with pytest.raises(ValueError):
        parse_template(
            {"schema_version": 1, "params": [{"key": "a", "flag": "--a", "label": "A", "type": "int",
                                              "default": "abc"}]}
        )


def test_parse_template_rejects_default_outside_options():
    with pytest.raises(ValueError, match="options"):
        parse_template(
            {"schema_version": 1, "params": [{"key": "a", "flag": "--a", "label": "A", "type": "enum",
                                              "options": ["x"], "default": "y"}]}
        )


def test_parse_template_rejects_too_many_params():
    params = [
        {"key": f"p{i}", "flag": f"--p{i}", "label": f"P{i}", "type": "int"}
        for i in range(MAX_PARAMS + 1)
    ]
    with pytest.raises(ValueError):
        parse_template({"schema_version": 1, "params": params})


def test_parse_template_rejects_unknown_field():
    with pytest.raises(ValueError):
        parse_template({"schema_version": 1, "params": [], "surprise": 1})


def test_parse_template_rejects_min_max_on_bool():
    with pytest.raises(ValueError, match="min/max"):
        parse_template(
            {"schema_version": 1, "params": [{"key": "a", "flag": "--a", "label": "A", "type": "bool",
                                              "min": 1}]}
        )


def test_flag_allows_dotted_hydra_style():
    template = parse_template(
        {"schema_version": 1, "params": [{"key": "cfg", "flag": "--config.name", "label": "配置",
                                          "type": "str"}]}
    )
    assert template.params[0].flag == "--config.name"


# --------------------------------------------------------------------------
# 命令行渲染（必须与前端 buildCommand.ts 一致）
# --------------------------------------------------------------------------
def test_shell_quote_safe_values_unchanged():
    assert shell_quote("1e-4") == "1e-4"
    assert shell_quote("/checkpoints/exp001") == "/checkpoints/exp001"


def test_shell_quote_wraps_and_escapes():
    assert shell_quote("a b") == "'a b'"
    assert shell_quote("it's") == "'it'\\''s'"


def test_render_uses_defaults_when_values_missing():
    template = make_template()
    assert build_command(template, {}, user_command=BASE) == (
        "python /app/train.py --steps 1000 --batch_size 8 --dtype bfloat16"
    )


def test_render_user_values_override_defaults():
    template = make_template()
    command = build_command(template, {"steps": 5, "batch_size": 2}, user_command=BASE)
    assert command == "python /app/train.py --steps 5 --batch_size 2 --dtype bfloat16"


def test_render_skips_blank_values_and_missing_default():
    template = parse_template(
        {
            "schema_version": 1,
            "params": [{"key": "resume", "flag": "--resume", "label": "恢复", "type": "path"}],
        }
    )
    assert build_command(template, {"resume": ""}, user_command=BASE) == BASE
    assert build_command(template, {}, user_command=BASE) == BASE


def test_render_bool_plain_flag():
    """true_value/false_value 都不是字符串：true 只拼 flag，false 直接跳过。"""
    template = parse_template(
        {
            "schema_version": 1,
            "params": [{"key": "amp", "flag": "--amp", "label": "AMP", "type": "bool", "default": False}],
        }
    )
    assert build_command(template, {"amp": True}, user_command=BASE) == BASE + " --amp"
    assert build_command(template, {"amp": False}, user_command=BASE) == BASE
    # 不传值 → 回退 default False → 跳过
    assert build_command(template, {}, user_command=BASE) == BASE


def test_render_bool_with_explicit_values():
    """给了 true_value/false_value 时两个分支都显式拼值（有些框架要求显式 --flag true/false）。

    注意陷阱：default 是 False 且 false_value 是字符串时，**不传值也会拼出
    --flag false**，因为 default 会参与渲染。模板作者想「默认不出现」就不要设 default。
    """
    template = parse_template(
        {
            "schema_version": 1,
            "params": [
                {
                    "key": "wandb",
                    "flag": "--wandb",
                    "label": "W&B",
                    "type": "bool",
                    "true_value": "true",
                    "false_value": "false",
                }
            ],
        }
    )
    assert build_command(template, {"wandb": True}, user_command=BASE) == BASE + " --wandb true"
    assert build_command(template, {"wandb": False}, user_command=BASE) == BASE + " --wandb false"
    # 没有 default：不传值就不出现
    assert build_command(template, {}, user_command=BASE) == BASE

def test_render_extra_args_appended_verbatim():
    template = parse_template({"schema_version": 1, "params": [], "extra_args": {"enabled": True}})
    assert build_command(template, {"extra_args": "--foo 1 --bar='a b'"}, user_command=BASE) == (
        BASE + " --foo 1 --bar='a b'"
    )


def test_render_extra_args_disabled():
    template = parse_template({"schema_version": 1, "params": [], "extra_args": {"enabled": False}})
    assert build_command(template, {"extra_args": "--foo"}, user_command=BASE) == BASE


def test_render_value_with_space_is_quoted():
    template = make_template()
    command = build_command(template, {"dtype": "float 16"}, user_command=BASE)
    assert "--dtype 'float 16'" in command


def test_render_extra_param_kind_skipped_in_builtin():
    """内置模板里 flag 为空的透传参数不单独拼 token。"""
    tokens = build_param_tokens(BUILTIN_TEMPLATE_MODEL, {"extra_args": "--x 1"})
    assert all(not token.startswith("None") for token in tokens)
    assert not any("--x 1" in token for token in tokens)


def test_resolve_base_command_priority():
    assert resolve_base_command(user_command="python mine.py", entrypoint="python ep.py",
                                default_command="python cfg.py") == "python mine.py"
    assert resolve_base_command(user_command="  ", entrypoint="python ep.py",
                                default_command="python cfg.py") == "python ep.py"
    assert resolve_base_command(user_command=None, entrypoint=None,
                                default_command="python cfg.py") == "python cfg.py"
    assert resolve_base_command(user_command="", entrypoint="", default_command="") == ""


def test_render_order_matches_params_array():
    """token 顺序必须严格按 params 数组，不能被字典顺序打乱。"""
    template = make_template()
    tokens = build_param_tokens(template, {"steps": 1, "batch_size": 2, "dtype": "float16"})
    assert tokens == ["--steps 1", "--batch_size 2", "--dtype float16"]


def test_render_empty_template_returns_base():
    template = ParamsTemplate(schema_version=1)
    assert build_command(template, {}, user_command=BASE) == BASE