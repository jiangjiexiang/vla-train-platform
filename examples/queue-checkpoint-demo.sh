#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
FIRST_JOB="${FIRST_JOB:-queue-smoke-001}"
SECOND_JOB="${SECOND_JOB:-queue-smoke-002}"
STEPS="${STEPS:-300}"
SLEEP="${SLEEP:-0.2}"

for JOB_NAME in "${FIRST_JOB}" "${SECOND_JOB}"; do
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
done

"${ROOT_DIR}/cli/vla-train" list
