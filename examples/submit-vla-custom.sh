#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
: "${VLA_IMAGE:?请设置 VLA_IMAGE 为自有 VLA 训练镜像}"
: "${VLA_COMMAND:?请设置 VLA_COMMAND 为镜像内的实际训练命令}"
JOB_NAME="${JOB_NAME:-vla-custom-001}"
VLA_GPUS="${VLA_GPUS:-1}"
VLA_PRIORITY="${VLA_PRIORITY:-normal}"
VLA_MAX_RETRIES="${VLA_MAX_RETRIES:-0}"

"${ROOT_DIR}/cli/vla-train" submit \
  --name "${JOB_NAME}" \
  --gpus "${VLA_GPUS}" \
  --image "${VLA_IMAGE}" \
  --queue default \
  --priority "${VLA_PRIORITY}" \
  --max-retries "${VLA_MAX_RETRIES}" \
  --env PYTHONUNBUFFERED=1 \
  --env "JOB_NAME=${JOB_NAME}" \
  --env DATASET_DIR=/datasets \
  --env "CHECKPOINT_DIR=/checkpoints/${JOB_NAME}" \
  --command "${VLA_COMMAND}"
