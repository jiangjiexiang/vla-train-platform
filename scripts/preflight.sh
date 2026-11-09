#!/usr/bin/env bash
set -euo pipefail

PROFILE="${PROFILE:-4090}"
MIN_DISK_GB="${MIN_DISK_GB:-20}"
WEB_NODE_PORT="${WEB_NODE_PORT:-30080}"
PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

usage() {
  cat <<'EOF'
Usage: PROFILE=4090|a100 [MIN_DISK_GB=20] scripts/preflight.sh

Read-only Ubuntu single-node GPU/K3s preflight. No packages, services, or
configuration files are changed. The script exits non-zero if a required check
fails. Set MIN_DISK_GB to override the minimum free space on the root filesystem.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ $# -gt 0 ]]; then
  usage >&2
  exit 2
fi

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  printf '[PASS] %s\n' "$*"
}

warn() {
  WARN_COUNT=$((WARN_COUNT + 1))
  printf '[WARN] %s\n' "$*"
}

fail() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  printf '[FAIL] %s\n' "$*" >&2
}

info() {
  printf '[INFO] %s\n' "$*"
}

case "$PROFILE" in
  4090|a100) ;;
  *) fail "PROFILE must be 4090 or a100 (got: $PROFILE)" ;;
esac
if ! [[ "$MIN_DISK_GB" =~ ^[0-9]+$ ]] || (( MIN_DISK_GB < 1 )); then
  fail "MIN_DISK_GB must be a positive integer (got: $MIN_DISK_GB)"
fi

printf '\n== Operating system and privileges ==\n'
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ "${ID:-}" == "ubuntu" ]]; then
    pass "Ubuntu detected: ${PRETTY_NAME:-unknown version}"
    case "${VERSION_ID:-}" in
      20.04|22.04|24.04) pass "Tested Ubuntu release family: $VERSION_ID" ;;
      *) warn "Ubuntu $VERSION_ID is outside the explicitly tested 20.04/22.04/24.04 families" ;;
    esac
  else
    fail "Unsupported OS: ${PRETTY_NAME:-unknown}; the installers target Ubuntu"
  fi
else
  fail "/etc/os-release is unavailable; cannot identify the OS"
fi
info "Architecture: $(uname -m); kernel: $(uname -r)"

if (( EUID == 0 )); then
  pass "Running as root"
elif command -v sudo >/dev/null 2>&1; then
  if sudo -n true >/dev/null 2>&1; then
    pass "Passwordless sudo is available"
  else
    pass "sudo is installed; installation may prompt for the user's password"
  fi
else
  fail "Run as root or install sudo; installation requires elevated privileges"
fi

printf '\n== NVIDIA driver and GPUs ==\n'
GPU_COUNT=0
if ! command -v nvidia-smi >/dev/null 2>&1; then
  fail "nvidia-smi is missing; install and validate an NVIDIA driver before K3s"
elif ! GPU_INFO=$(nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>&1); then
  fail "nvidia-smi failed: $GPU_INFO"
else
  GPU_COUNT=$(printf '%s\n' "$GPU_INFO" | awk 'NF { count++ } END { print count + 0 }')
  pass "nvidia-smi works; detected $GPU_COUNT GPU(s)"
  while IFS= read -r line; do
    [[ -n "$line" ]] && info "GPU: $line"
  done <<< "$GPU_INFO"
  case "$PROFILE" in
    4090)
      (( GPU_COUNT >= 1 )) && pass "4090 profile requires at least one visible GPU" || fail "No GPU is visible"
      if ! grep -qi '4090' <<< "$GPU_INFO"; then
        warn "GPU model does not contain '4090'; quota will still be fixed at 1 GPU"
      fi
      ;;
    a100)
      (( GPU_COUNT >= 8 )) && pass "A100 profile has at least 8 visible GPUs" || fail "A100 profile requires 8 visible GPUs; found $GPU_COUNT"
      if ! grep -qi 'A100' <<< "$GPU_INFO"; then
        warn "GPU model does not contain 'A100'; quota will still be fixed at 8 GPUs"
      fi
      ;;
  esac
fi

if command -v nvidia-ctk >/dev/null 2>&1; then
  pass "NVIDIA Container Toolkit CLI: $(nvidia-ctk --version 2>&1 | head -n 1)"
else
  warn "nvidia-ctk is missing; install-k3s-gpu.sh can install the pinned toolkit"
fi

printf '\n== Storage and required tools ==\n'
if [[ "$MIN_DISK_GB" =~ ^[0-9]+$ ]]; then
  ROOT_FREE_KB=$(df -Pk / | awk 'NR == 2 { print $4 }')
  if [[ "$ROOT_FREE_KB" =~ ^[0-9]+$ ]]; then
    ROOT_FREE_GB=$((ROOT_FREE_KB / 1024 / 1024))
    if (( ROOT_FREE_GB >= MIN_DISK_GB )); then
      pass "Root filesystem has ${ROOT_FREE_GB} GiB free (minimum ${MIN_DISK_GB} GiB)"
    else
      fail "Root filesystem has only ${ROOT_FREE_GB} GiB free; ${MIN_DISK_GB} GiB is required"
    fi
  else
    warn "Could not determine free disk space"
  fi
fi
for tool in awk tar uname df; do
  if command -v "$tool" >/dev/null 2>&1; then
    pass "Required tool is available: $tool"
  else
    fail "Required tool is missing: $tool"
  fi
done
if command -v curl >/dev/null 2>&1; then
  pass "curl is available for online installation"
else
  warn "curl is missing; online installation is unavailable, but local offline artifacts can still be used"
fi

printf '\n== Existing container and Kubernetes installations ==\n'
if command -v docker >/dev/null 2>&1; then
  warn "Docker is already installed ($(docker --version 2>&1)); installers will not remove or reconfigure it"
else
  info "Docker CLI not detected"
fi
if command -v k3s >/dev/null 2>&1; then
  warn "Existing K3s binary: $(k3s --version 2>&1 | head -n 1); it will not be reinstalled"
else
  info "K3s binary not detected"
fi
if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-active --quiet k3s 2>/dev/null; then
    warn "The k3s systemd service is active; existing cluster configuration must be preserved"
  fi
  if systemctl is-active --quiet kubelet 2>/dev/null; then
    warn "A kubelet service is active; do not install another Kubernetes distribution over it"
  fi
fi
if command -v kubectl >/dev/null 2>&1; then
  if kubectl get nodes --request-timeout=5s >/dev/null 2>&1; then
    warn "kubectl can reach an existing cluster; inspect its context before installation"
  else
    info "kubectl exists but no cluster is reachable with the current user's configuration"
  fi
else
  info "Standalone kubectl not detected; K3s provides 'k3s kubectl'"
fi

printf '\n== Listening TCP ports ==\n'
if command -v ss >/dev/null 2>&1; then
  for port in 6443 10250 "$WEB_NODE_PORT"; do
    if ! [[ "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
      fail "Invalid port value: $port"
      continue
    fi
    LISTENERS=$(ss -H -ltn "sport = :$port" 2>/dev/null || true)
    if [[ -z "$LISTENERS" ]]; then
      pass "TCP port $port is not currently listening"
    elif [[ "$port" == "$WEB_NODE_PORT" && "${ALLOW_OCCUPIED_WEB_PORT:-0}" == "1" ]]; then
      warn "TCP port $port is already in use; allowed because an existing K3s installation is being reused"
    elif [[ "$port" == "$WEB_NODE_PORT" ]]; then
      fail "TCP port $port is already in use; Web NodePort $WEB_NODE_PORT would conflict"
    else
      warn "TCP port $port is already in use; expected for an existing cluster, otherwise investigate before installation"
    fi
  done
else
  warn "'ss' is unavailable; cannot check ports 6443, 10250, and $WEB_NODE_PORT"
fi

printf '\n== Preflight summary ==\n'
printf 'PASS=%d WARN=%d FAIL=%d\n' "$PASS_COUNT" "$WARN_COUNT" "$FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then
  printf 'Preflight failed. Resolve every [FAIL] item before installation.\n' >&2
  exit 1
fi
printf 'Preflight passed with %d warning(s). Review warnings before changing the host.\n' "$WARN_COUNT"
