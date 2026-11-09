#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

docker build --tag vla-smoke:latest "${ROOT_DIR}/images/smoke-train"
docker save vla-smoke:latest | sudo k3s ctr -n k8s.io images import -
