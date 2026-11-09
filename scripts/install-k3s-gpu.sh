#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
K3S_VERSION="${K3S_VERSION:-v1.36.6+k3s1}"
NVIDIA_CONTAINER_TOOLKIT_VERSION="${NVIDIA_CONTAINER_TOOLKIT_VERSION:-1.17.8-1}"
K3S_INSTALL_SCRIPT="${K3S_INSTALL_SCRIPT:-}"
K3S_BINARY="${K3S_BINARY:-}"
K3S_AIRGAP_IMAGES="${K3S_AIRGAP_IMAGES:-}"
NVIDIA_TOOLKIT_DEB_DIR="${NVIDIA_TOOLKIT_DEB_DIR:-}"
ALLOW_EXISTING_K3S_RECONFIGURE="${ALLOW_EXISTING_K3S_RECONFIGURE:-0}"
SKIP_PREFLIGHT="${SKIP_PREFLIGHT:-0}"
PROFILE="${PROFILE:-4090}"

usage() {
  cat <<EOF
Usage: sudo PROFILE=4090|a100 scripts/install-k3s-gpu.sh

Installs the pinned NVIDIA Container Toolkit and K3s on an Ubuntu single node.
Existing Docker is never removed or reconfigured. Existing K3s is reused, not
reinstalled. An existing NVIDIA runtime plus RuntimeClass is accepted without
changing the cluster; training Pods then use runtimeClassName=nvidia. Otherwise,
the script stops unless ALLOW_EXISTING_K3S_RECONFIGURE=1 is set. That opt-in
writes /etc/rancher/k3s/config.yaml.d/10-vla-nvidia.yaml and restarts K3s.

Pinned defaults (override only with known-compatible versions):
  K3S_VERSION=$K3S_VERSION
  NVIDIA_CONTAINER_TOOLKIT_VERSION=$NVIDIA_CONTAINER_TOOLKIT_VERSION

Offline options:
  K3S_INSTALL_SCRIPT=/path/to/k3s-install.sh
  K3S_BINARY=/path/to/k3s                 # optional; installed only for new K3s
  K3S_AIRGAP_IMAGES=/path/to/k3s-images.tar
  NVIDIA_TOOLKIT_DEB_DIR=/path/to/debs     # all toolkit dependency .deb files

For a fully offline new installation, provide K3S_INSTALL_SCRIPT and either
K3S_BINARY or a preloaded /usr/local/bin/k3s. K3S_AIRGAP_IMAGES is strongly
recommended. No SSH credentials or external host access are used by this script.
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

log() { printf '\n==> %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

case "$PROFILE" in
  4090|a100) ;;
  *) die "PROFILE must be 4090 or a100" ;;
esac
[[ "$ALLOW_EXISTING_K3S_RECONFIGURE" =~ ^[01]$ ]] || die "ALLOW_EXISTING_K3S_RECONFIGURE must be 0 or 1"
[[ "$SKIP_PREFLIGHT" =~ ^[01]$ ]] || die "SKIP_PREFLIGHT must be 0 or 1"
[[ -r /etc/os-release ]] || die "Cannot identify this operating system"
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || die "This installer targets Ubuntu; detected ${PRETTY_NAME:-unknown}"
command -v nvidia-smi >/dev/null 2>&1 || die "nvidia-smi is missing; install the NVIDIA driver first"
nvidia-smi >/dev/null || die "nvidia-smi failed; repair the NVIDIA driver first"

if (( EUID == 0 )); then
  SUDO=()
elif command -v sudo >/dev/null 2>&1; then
  SUDO=(sudo)
else
  die "Run as root or install sudo"
fi
run_root() { "${SUDO[@]}" "$@"; }

EXISTING_K3S=0
if command -v k3s >/dev/null 2>&1 || run_root systemctl list-unit-files k3s.service --no-legend 2>/dev/null | grep -q 'k3s.service'; then
  EXISTING_K3S=1
fi
if run_root systemctl is-active --quiet kubelet 2>/dev/null; then
  die "An existing kubelet service is active. Refusing to install K3s over another Kubernetes setup"
fi
if (( EXISTING_K3S == 0 )) && command -v kubectl >/dev/null 2>&1 && kubectl get nodes --request-timeout=5s >/dev/null 2>&1; then
  die "kubectl reaches an existing non-K3s cluster. Refusing to install over it"
fi

if [[ "$SKIP_PREFLIGHT" == "0" ]]; then
  log "Running read-only preflight"
  ALLOW_OCCUPIED_WEB_PORT="$EXISTING_K3S" PROFILE="$PROFILE" bash "$SCRIPT_DIR/preflight.sh"
else
  warn "SKIP_PREFLIGHT=1: host validation was explicitly skipped"
fi

install_toolkit() {
  if command -v nvidia-ctk >/dev/null 2>&1 && command -v nvidia-container-runtime >/dev/null 2>&1; then
    log "NVIDIA Container Toolkit already installed; leaving it unchanged"
    nvidia-ctk --version
    return
  fi

  log "Installing NVIDIA Container Toolkit $NVIDIA_CONTAINER_TOOLKIT_VERSION"
  if [[ -n "$NVIDIA_TOOLKIT_DEB_DIR" ]]; then
    [[ -d "$NVIDIA_TOOLKIT_DEB_DIR" ]] || die "NVIDIA_TOOLKIT_DEB_DIR is not a directory: $NVIDIA_TOOLKIT_DEB_DIR"
    local debs=()
    shopt -s nullglob
    debs=("$NVIDIA_TOOLKIT_DEB_DIR"/*.deb)
    shopt -u nullglob
    (( ${#debs[@]} > 0 )) || die "No .deb files found in $NVIDIA_TOOLKIT_DEB_DIR"
    run_root apt-get install -y "${debs[@]}"
  else
    command -v curl >/dev/null 2>&1 || die "curl is required for online toolkit installation; use NVIDIA_TOOLKIT_DEB_DIR offline"
    command -v gpg >/dev/null 2>&1 || run_root apt-get install -y gnupg
    local tmp_key tmp_list
    tmp_key=$(mktemp)
    tmp_list=$(mktemp)
    if ! run_root test -f /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg; then
      curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey -o "$tmp_key"
      gpg --dearmor --batch --yes --output "$tmp_key.gpg" "$tmp_key"
      run_root install -D -m 0644 "$tmp_key.gpg" /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    fi
    if ! run_root test -f /etc/apt/sources.list.d/nvidia-container-toolkit.list; then
      curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list -o "$tmp_list"
      sed -i 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' "$tmp_list"
      run_root install -m 0644 "$tmp_list" /etc/apt/sources.list.d/nvidia-container-toolkit.list
    fi
    rm -f "$tmp_key" "$tmp_key.gpg" "$tmp_list"
    run_root apt-get update
    run_root apt-get install -y \
      "nvidia-container-toolkit=$NVIDIA_CONTAINER_TOOLKIT_VERSION" \
      "nvidia-container-toolkit-base=$NVIDIA_CONTAINER_TOOLKIT_VERSION" \
      "libnvidia-container-tools=$NVIDIA_CONTAINER_TOOLKIT_VERSION" \
      "libnvidia-container1=$NVIDIA_CONTAINER_TOOLKIT_VERSION"
  fi
  command -v nvidia-ctk >/dev/null 2>&1 || die "nvidia-ctk is still unavailable after installation"
  command -v nvidia-container-runtime >/dev/null 2>&1 || die "nvidia-container-runtime is still unavailable after installation"
}

install_toolkit

CONTAINERD_CONFIG=/var/lib/rancher/k3s/agent/etc/containerd/config.toml
nvidia_runtime_available() {
  run_root grep -Eq "containerd\.runtimes\.'nvidia'" "$CONTAINERD_CONFIG" 2>/dev/null \
    && run_root k3s kubectl get runtimeclass nvidia >/dev/null 2>&1
}

if (( EXISTING_K3S == 1 )); then
  log "Existing K3s detected; skipping K3s installation"
  if run_root grep -Eq 'default_runtime_name[[:space:]]*=[[:space:]]*"nvidia"' "$CONTAINERD_CONFIG" 2>/dev/null; then
    printf 'Existing K3s already uses NVIDIA as the default containerd runtime.\n'
  elif nvidia_runtime_available; then
    printf 'Existing K3s has NVIDIA runtime and RuntimeClass; training Pods will use runtimeClassName=nvidia.\n'
  elif [[ "$ALLOW_EXISTING_K3S_RECONFIGURE" == "1" ]]; then
    warn "RISK: writing one K3s NVIDIA drop-in and restarting the existing k3s service"
    if run_root test -e /etc/rancher/k3s/config.yaml.d/10-vla-nvidia.yaml; then
      die "Drop-in already exists but NVIDIA is not active; inspect /etc/rancher/k3s/config.yaml.d/10-vla-nvidia.yaml manually"
    fi
    run_root install -d -m 0755 /etc/rancher/k3s/config.yaml.d
    printf 'default-runtime: nvidia\n' | run_root tee /etc/rancher/k3s/config.yaml.d/10-vla-nvidia.yaml >/dev/null
    run_root systemctl restart k3s
  else
    die "Existing K3s has neither an NVIDIA default runtime nor an nvidia RuntimeClass. Re-run with ALLOW_EXISTING_K3S_RECONFIGURE=1 to configure the default runtime and restart K3s"
  fi
else
  log "Preparing new K3s $K3S_VERSION installation"
  if [[ -n "$K3S_BINARY" ]]; then
    [[ -f "$K3S_BINARY" ]] || die "K3S_BINARY does not exist: $K3S_BINARY"
    run_root install -m 0755 "$K3S_BINARY" /usr/local/bin/k3s
  fi
  if [[ -n "$K3S_AIRGAP_IMAGES" ]]; then
    [[ -f "$K3S_AIRGAP_IMAGES" ]] || die "K3S_AIRGAP_IMAGES does not exist: $K3S_AIRGAP_IMAGES"
    run_root install -d -m 0755 /var/lib/rancher/k3s/agent/images
    run_root install -m 0644 "$K3S_AIRGAP_IMAGES" /var/lib/rancher/k3s/agent/images/
  fi

  INSTALLER="$K3S_INSTALL_SCRIPT"
  TEMP_INSTALLER=""
  if [[ -z "$INSTALLER" ]]; then
    command -v curl >/dev/null 2>&1 || die "curl is unavailable; set K3S_INSTALL_SCRIPT for offline installation"
    TEMP_INSTALLER=$(mktemp)
    curl -fsSL "https://raw.githubusercontent.com/k3s-io/k3s/$K3S_VERSION/install.sh" -o "$TEMP_INSTALLER"
    INSTALLER="$TEMP_INSTALLER"
  fi
  [[ -f "$INSTALLER" ]] || die "K3s install script does not exist: $INSTALLER"

  SKIP_DOWNLOAD=false
  if command -v k3s >/dev/null 2>&1; then
    SKIP_DOWNLOAD=true
  fi
  printf 'RISK: installing K3s creates systemd services and /etc/rancher, /var/lib/rancher state.\n'
  run_root env \
    INSTALL_K3S_VERSION="$K3S_VERSION" \
    INSTALL_K3S_EXEC="server --write-kubeconfig-mode 600 --default-runtime=nvidia" \
    INSTALL_K3S_SKIP_DOWNLOAD="$SKIP_DOWNLOAD" \
    bash "$INSTALLER"
  [[ -z "$TEMP_INSTALLER" ]] || rm -f "$TEMP_INSTALLER"
fi

log "Waiting for the K3s node to become Ready"
READY=0
for _ in $(seq 1 60); do
  if run_root k3s kubectl get nodes --request-timeout=5s 2>/dev/null | grep -q ' Ready'; then
    READY=1
    break
  fi
  sleep 5
done
(( READY == 1 )) || die "K3s node did not become Ready within 5 minutes; inspect: journalctl -u k3s"
run_root k3s kubectl get nodes -o wide
if run_root grep -Eq 'default_runtime_name[[:space:]]*=[[:space:]]*"nvidia"' "$CONTAINERD_CONFIG" 2>/dev/null; then
  printf 'Verified NVIDIA as K3s containerd default runtime.\n'
elif nvidia_runtime_available; then
  printf 'Verified NVIDIA runtime and RuntimeClass; training Pods will use runtimeClassName=nvidia.\n'
else
  die "K3s is Ready but NVIDIA runtime is unavailable; inspect $CONTAINERD_CONFIG and runtimeclasses"
fi
printf '\nK3s/GPU base installation complete. Next: sudo PROFILE=%s scripts/install-stack.sh\n' "$PROFILE"
printf 'Administrative kubeconfig: /etc/rancher/k3s/k3s.yaml (mode 0600).\n'
