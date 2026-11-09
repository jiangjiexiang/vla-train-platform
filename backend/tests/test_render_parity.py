"""前后端命令行拼装一致性测试（v0.9.1 核心风险点）。

前端 ``frontend/src/utils/buildCommand.ts`` 负责「最终命令预览」，后端
``backend/app/services/params_render.py`` 负责生成真正执行的命令。两者一旦不一致，
用户看到的预览就不是实际跑的命令，这类问题极难排查。

做法：用同一组 (模板, 取值) 分别跑前端和后端，比较输出。为此本测试直接调用
前端的 TS 实现——通过 ``node`` 执行一段小脚本导入它并输出结果。
node 路径由 ``scripts/test.sh`` 使用的 ``NODE_BIN_DIR``（默认 /opt/node20/bin）决定，
找不到 node 时整个测试跳过（CI 的后端 job 没有 node，不应因此失败）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from app.params_templates import BUILTIN_TEMPLATE, parse_template
from app.services.params_render import build_command

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_COMMAND_TS = REPO_ROOT / "frontend" / "src" / "utils" / "buildCommand.ts"


def _find_node() -> str | None:
    candidate = os.environ.get("NODE_BIN_DIR", "/opt/node20/bin")
    node = Path(candidate) / "node"
    if node.is_file():
        return str(node)
    return shutil.which("node")


NODE = _find_node()

# (模板, 取值) 组合：覆盖契约第 3 节的每个分支
CASES = [
    (
        "内置通用模板",
        BUILTIN_TEMPLATE,
        {"train_steps": 1000, "batch_size": 8, "learning_rate": "1e-4", "dtype": "bfloat16"},
    ),
    ("空取值", BUILTIN_TEMPLATE, {}),
    (
        "带空格与单引号",
        {
            "schema_version": 1,
            "params": [
                {"key": "note", "flag": "--note", "label": "备注", "type": "str"},
                {"key": "resume", "flag": "--resume", "label": "恢复", "type": "path"},
            ],
        },
        {"note": "it's a test", "resume": "/ckpt/my run/last"},
    ),
    (
        "bool 各分支",
        {
            "schema_version": 1,
            "params": [
                {"key": "amp", "flag": "--amp", "label": "AMP", "type": "bool", "default": False},
                {"key": "plain", "flag": "--plain", "label": "P", "type": "bool", "true_value": ""},
                {"key": "tri", "flag": "--tri", "label": "T", "type": "bool",
                 "true_value": "true", "false_value": "false"},
            ],
        },
        {"amp": True, "plain": True, "tri": False},
    ),
    (
        "enum 与数值边界",
        {
            "schema_version": 1,
            "params": [
                {"key": "dtype", "flag": "--dtype", "label": "精度", "type": "enum",
                 "default": "bfloat16", "options": ["bfloat16", "float16"]},
                {"key": "lr", "flag": "--lr", "label": "学习率", "type": "float", "default": 1e-4},
                {"key": "steps", "flag": "--steps", "label": "步数", "type": "int", "default": 0},
            ],
        },
        {"dtype": "float16", "lr": "2.5e-5", "steps": 0},
    ),
    (
        "extra_args 原样追加",
        {
            "schema_version": 1,
            "params": [{"key": "steps", "flag": "--steps", "label": "步数", "type": "int", "default": 10}],
            "extra_args": {"enabled": True},
        },
        {"steps": 20, "extra_args": "--foo 1 --bar='a b'"},
    ),
    (
        "extra_args 关闭",
        {
            "schema_version": 1,
            "params": [],
            "extra_args": {"enabled": False},
        },
        {"extra_args": "--ignored"},
    ),
    (
        "Hydra 风格 flag",
        {
            "schema_version": 1,
            "params": [
                {"key": "cfg", "flag": "--config.name", "label": "配置", "type": "str", "default": "pi05_droid"},
                {"key": "fsdp", "flag": "--fsdp-devices", "label": "FSDP", "type": "int", "default": 1},
            ],
        },
        {"cfg": "pi05_libero"},
    ),
]

BASE_COMMANDS = ["python /app/train.py", "torchrun --nproc_per_node=1 /app/train.py"]


def _node_bin(name: str) -> Path:
    return REPO_ROOT / "frontend" / "node_modules" / ".bin" / name


def _run_frontend(cases: list[tuple[dict, dict, str]]) -> list[str]:
    """用 esbuild 转译前端 buildCommand.ts，再用 node 批量求值，返回每个用例的结果。

    用 esbuild 而不是 node 的 TS 支持：node 20 还不支持 --experimental-strip-types。
    一次转译 + 一次 node 启动求值所有用例，避免每个用例都拉起一个进程。
    """
    esbuild = _node_bin("esbuild")
    out = REPO_ROOT / "frontend" / "node_modules" / ".cache" / "buildCommand.parity.mjs"
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(  # noqa: S603 - 测试内固定命令
        [str(esbuild), str(BUILD_COMMAND_TS), "--bundle", "--format=esm", f"--outfile={out}"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT / "frontend"),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"esbuild failed: {proc.stderr[:800]}")

    script = f"""
import {{ buildCommandFromTemplate }} from {json.dumps(out.as_uri())};
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map(([t, v, b]) => buildCommandFromTemplate(t, v, b))));
"""
    proc = subprocess.run(  # noqa: S603
        [NODE, "--input-type=module", "-e", script, json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT / "frontend"),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node failed: {proc.stderr[:800]}")
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def frontend_results():
    """一次性算出所有 (用例 × base command) 的前端结果。"""
    if NODE is None:
        pytest.skip("node not available; frontend/backend parity check needs node")
    if not BUILD_COMMAND_TS.is_file():
        pytest.skip("frontend buildCommand.ts missing")
    if not _node_bin("esbuild").is_file():
        pytest.skip("esbuild not installed; run npm ci in frontend/")
    pairs = [(template, values, base) for _name, template, values in CASES for base in BASE_COMMANDS]
    return _run_frontend(pairs)


@pytest.mark.parametrize("base_index,base", list(enumerate(BASE_COMMANDS)))
@pytest.mark.parametrize("case_index,name", [(i, c[0]) for i, c in enumerate(CASES)])
def test_frontend_and_backend_render_identically(case_index, name, base_index, base, frontend_results):
    _n, template, values = CASES[case_index]
    backend_command = build_command(parse_template(template), values, user_command=base)
    index = case_index * len(BASE_COMMANDS) + base_index
    frontend_command = frontend_results[index]

    assert frontend_command == backend_command, (
        f"前后端拼装结果不一致（用例：{name}）\n"
        f"  后端: {backend_command!r}\n  前端: {frontend_command!r}"
    )