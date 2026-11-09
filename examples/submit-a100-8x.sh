#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
JOB_NAME="${JOB_NAME:-smoke-a100-8x-001}"
STEPS="${STEPS:-100}"
SLEEP="${SLEEP:-0.2}"

"${ROOT_DIR}/cli/vla-train" submit \
  --name "${JOB_NAME}" \
  --gpus 8 \
  --image vla-smoke:latest \
  --queue default \
  --priority high \
  --max-retries 0 \
  --env PYTHONUNBUFFERED=1 \
  --env "JOB_NAME=${JOB_NAME}" \
  --env "CHECKPOINT_DIR=/checkpoints/${JOB_NAME}" \
  --command "torchrun --standalone --nproc_per_node=8 /app/train.py --distributed --steps ${STEPS} --sleep ${SLEEP}"
