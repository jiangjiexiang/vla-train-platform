#!/usr/bin/env bash
set -euo pipefail

# 后端单元测试入口。首次运行会自动建 .venv 并安装 requirements-dev.txt。

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"

cd "$ROOT_DIR"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  printf "==> 创建虚拟环境 %s\n" "$VENV_DIR"
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install -q --upgrade pip >/dev/null
  "$VENV_DIR/bin/pip" install -q -r backend/requirements-dev.txt
fi

printf "==> 运行后端单元测试\n"
exec "$VENV_DIR/bin/python" -m pytest backend/tests "$@"
