#!/usr/bin/env bash
set -euo pipefail

# 单元测试入口：后端 pytest + 前端 vitest。
#   scripts/test.sh                 # 两边都跑
#   scripts/test.sh backend [-q]    # 只跑后端，其余参数透传给 pytest
#   scripts/test.sh frontend        # 只跑前端，其余参数透传给 vitest
# 首次运行会为后端创建 .venv；前端缺少 node_modules 时会先 npm ci。

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
NODE_BIN_DIR="${NODE_BIN_DIR:-/opt/node20/bin}"
TARGET="${1:-all}"
shift || true

case "$TARGET" in
  all|backend|frontend) ;;
  *) printf '[ERROR] 用法：scripts/test.sh [all|backend|frontend] [参数...]\n' >&2; exit 2 ;;
esac

cd "$ROOT_DIR"

run_backend() {
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    printf '==> 创建虚拟环境 %s\n' "$VENV_DIR"
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install -q --upgrade pip >/dev/null
    "$VENV_DIR/bin/pip" install -q -r backend/requirements-dev.txt
  fi
  printf '==> 后端单元测试 (pytest)\n'
  "$VENV_DIR/bin/python" -m pytest backend/tests "$@"
}

ensure_node() {
  local major
  major="$(node -v 2>/dev/null | sed 's/^v\([0-9]*\).*/\1/')" || major=0
  if [[ -z "$major" || "$major" -lt 18 ]]; then
    if [[ -x "$NODE_BIN_DIR/node" ]]; then
      export PATH="$NODE_BIN_DIR:$PATH"
    else
      printf '[ERROR] 需要 Node >= 18；未找到可用的 node，也未在 %s 找到\n' "$NODE_BIN_DIR" >&2
      exit 1
    fi
  fi
  printf '==> Node %s / npm %s\n' "$(node -v)" "$(npm -v)"
}

run_frontend() {
  ensure_node
  if [[ ! -d frontend/node_modules ]]; then
    printf '==> 安装前端依赖 (npm ci)\n'
    ( cd frontend && npm ci --silent )
  fi
  printf '==> 前端单元测试 (vitest)\n'
  ( cd frontend && npm run test --silent -- "$@" )
}

case "$TARGET" in
  backend) run_backend "$@" ;;
  frontend) run_frontend "$@" ;;
  all) run_backend "$@"; run_frontend ;;
esac
