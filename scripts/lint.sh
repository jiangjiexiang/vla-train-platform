#!/usr/bin/env bash
set -euo pipefail

# 代码规范检查入口：ruff check backend。
#   scripts/lint.sh              # 检查 backend
#   scripts/lint.sh --fix        # 自动修复可修复项（其余参数透传给 ruff）
# 首次运行会创建 .venv 并安装 backend/requirements-dev.txt。

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"

cd "$ROOT_DIR"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  printf '==> 创建虚拟环境 %s\n' "$VENV_DIR"
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install -q --upgrade pip >/dev/null
  "$VENV_DIR/bin/pip" install -q -r backend/requirements-dev.txt
elif [[ ! -x "$VENV_DIR/bin/ruff" ]]; then
  # 已有 .venv 但缺少 ruff（例如在引入 lint 之前创建的），补装开发依赖。
  printf '==> 安装后端开发依赖 (requirements-dev.txt)\n'
  "$VENV_DIR/bin/pip" install -q -r backend/requirements-dev.txt
fi

printf '==> 后端代码规范检查 (ruff)\n'
"$VENV_DIR/bin/ruff" check backend "$@"
