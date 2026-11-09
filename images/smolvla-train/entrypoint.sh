#!/usr/bin/env bash
# 容器入口。
#
# 只做三件事：确认环境变量、把参数转发给 train_vla.py、把失败原因翻译成人话。
# 真正的参数解析在 Python 侧，避免 shell 与 python 两处维护同一套规则。
set -euo pipefail

# 平台会注入这两个变量；缺失时给出明确提示而不是让它空着继续跑
if [[ -z "${DATASET_ROOT:-}" ]] && [[ "${1:-}" != "--help" && "${1:-}" != "-h" && "${1:-}" != "--smoke" ]]; then
  printf 'DATASET_ROOT 未设置。平台提交时会自动注入；手动运行时请设置：\n' >&2
  printf '  DATASET_ROOT=/datasets/<数据集名> docker run ...\n' >&2
  exit 2
fi

exec python /app/train_vla.py "$@"
