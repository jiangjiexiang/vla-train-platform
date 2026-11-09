#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
JOB_NAME="${JOB_NAME:-smoke-4090-001}"
STEPS="${STEPS:-100}"
SLEEP="${SLEEP:-0.2}"

"${ROOT_DIR}/cli/vla-train" submit \
  --name "${JOB_NAME}" \
  --gpus 1 \
  --image vla-smoke:latest \
  --queue default \
  --priority normal \
  --max-retries 0 \
  --env PYTHONUNBUFFERED=1 \
  --env "JOB_NAME=${JOB_NAME}" \
  --env "CHECKPOINT_DIR=/checkpoints/${JOB_NAME}" \
  --command "python /app/train.py --steps ${STEPS} --sleep ${SLEEP}"
