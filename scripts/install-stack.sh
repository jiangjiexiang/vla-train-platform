#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
PROFILE="${PROFILE:-4090}"
KUEUE_VERSION="${KUEUE_VERSION:-v0.20.5}"
TRAINING_OPERATOR_VERSION="${TRAINING_OPERATOR_VERSION:-v1.9.0}"
NVIDIA_DEVICE_PLUGIN_VERSION="${NVIDIA_DEVICE_PLUGIN_VERSION:-v0.17.1}"
KUEUE_MANIFEST="${KUEUE_MANIFEST:-https://github.com/kubernetes-sigs/kueue/releases/download/$KUEUE_VERSION/manifests.yaml}"
TRAINING_OPERATOR_MANIFEST="${TRAINING_OPERATOR_MANIFEST:-https://github.com/kubeflow/training-operator/manifests/overlays/standalone?ref=$TRAINING_OPERATOR_VERSION}"
NVIDIA_DEVICE_PLUGIN_MANIFEST="${NVIDIA_DEVICE_PLUGIN_MANIFEST:-https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/$NVIDIA_DEVICE_PLUGIN_VERSION/deployments/static/nvidia-device-plugin.yml}"
IMAGE_TAR_DIR="${IMAGE_TAR_DIR:-}"
KUBECTL_BIN="${KUBECTL_BIN:-}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-300s}"

usage() {
  cat <<EOF
Usage: sudo PROFILE=4090|a100 scripts/install-stack.sh

Installs the pinned NVIDIA Device Plugin, Kubeflow Training Operator and Kueue
into an existing K3s, then applies the platform namespace/RBAC/storage and the
selected Kueue quota. Existing component resources are updated idempotently with
kubectl apply; no existing Docker/Kubernetes distribution is removed.

Pinned defaults:
  KUEUE_VERSION=$KUEUE_VERSION
  TRAINING_OPERATOR_VERSION=$TRAINING_OPERATOR_VERSION
  NVIDIA_DEVICE_PLUGIN_VERSION=$NVIDIA_DEVICE_PLUGIN_VERSION

Offline installation:
  IMAGE_TAR_DIR=/path/to/image-tars
  NVIDIA_DEVICE_PLUGIN_MANIFEST=/path/to/nvidia-device-plugin.yml
  TRAINING_OPERATOR_MANIFEST=/path/to/training-operator-kustomize-dir
  KUEUE_MANIFEST=/path/to/kueue-manifests.yaml

Every *.tar, *.tar.gz and *.tgz in IMAGE_TAR_DIR is imported with k3s ctr before
manifests are applied. Archives must contain the exact image names/tags referenced
by those manifests. A local Training Operator target may instead be one YAML file.
No external registry is needed when all images and manifests are supplied locally.

Set KUBECTL_BIN=/path/to/kubectl to use a preconfigured K3s kubeconfig instead of
'sudo k3s kubectl'. WAIT_TIMEOUT defaults to 300s.
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
[[ -f "$ROOT_DIR/deploy/kueue/$PROFILE/kustomization.yaml" ]] || die "Missing Kueue profile: $PROFILE"

if (( EUID == 0 )); then
  SUDO=()
elif command -v sudo >/dev/null 2>&1; then
  SUDO=(sudo)
else
  die "Run as root or install sudo"
fi
run_root() { "${SUDO[@]}" "$@"; }
if [[ -n "$KUBECTL_BIN" ]]; then
  [[ -x "$KUBECTL_BIN" ]] || die "KUBECTL_BIN is not executable: $KUBECTL_BIN"
  kube() { "$KUBECTL_BIN" "$@"; }
else
  command -v k3s >/dev/null 2>&1 || run_root test -x /usr/local/bin/k3s || die "K3s is not installed; run install-k3s-gpu.sh first"
  kube() { run_root k3s kubectl "$@"; }
fi

kube get nodes --request-timeout=10s >/dev/null || die "Cannot reach K3s; inspect k3s.service and kubeconfig"
if ! kube wait nodes --all --for=condition=Ready --timeout="$WAIT_TIMEOUT"; then
  die "Kubernetes nodes are not Ready"
fi

if [[ -n "$IMAGE_TAR_DIR" ]]; then
  [[ -d "$IMAGE_TAR_DIR" ]] || die "IMAGE_TAR_DIR is not a directory: $IMAGE_TAR_DIR"
  [[ -z "$KUBECTL_BIN" ]] || die "IMAGE_TAR_DIR import requires local K3s; unset KUBECTL_BIN"
  shopt -s nullglob
  IMAGE_ARCHIVES=("$IMAGE_TAR_DIR"/*.tar "$IMAGE_TAR_DIR"/*.tar.gz "$IMAGE_TAR_DIR"/*.tgz)
  shopt -u nullglob
  (( ${#IMAGE_ARCHIVES[@]} > 0 )) || die "No image archives found in $IMAGE_TAR_DIR"
  log "Importing local component images into K3s containerd"
  for archive in "${IMAGE_ARCHIVES[@]}"; do
    printf 'Importing %s\n' "$archive"
    run_root k3s ctr images import "$archive"
  done
fi

apply_target() {
  local target=$1
  local name=$2
  if [[ -d "$target" ]]; then
    [[ -f "$target/kustomization.yaml" || -f "$target/kustomization.yml" || -f "$target/Kustomization" ]] || die "$name local directory has no kustomization file: $target"
    kube apply --server-side --force-conflicts -k "$target"
  elif [[ -f "$target" ]]; then
    kube apply --server-side --force-conflicts -f "$target"
  elif [[ "$target" =~ ^https?:// ]]; then
    if [[ "$target" =~ \.ya?ml($|\?) ]]; then
      kube apply --server-side --force-conflicts -f "$target"
    else
      kube apply --server-side --force-conflicts -k "$target"
    fi
  else
    die "$name manifest does not exist: $target"
  fi
}

if kube -n kube-system get daemonset/nvidia-device-plugin-daemonset >/dev/null 2>&1; then
  log "Reusing existing NVIDIA Device Plugin in kube-system"
  kube -n kube-system rollout status daemonset/nvidia-device-plugin-daemonset --timeout="$WAIT_TIMEOUT" || die "NVIDIA Device Plugin DaemonSet did not become ready"
elif kube -n nvidia-device-plugin get daemonset/nvidia-device-plugin >/dev/null 2>&1; then
  log "Reusing existing NVIDIA Device Plugin in nvidia-device-plugin namespace"
  printf 'Existing plugin will become ready after its node-discovery dependencies label the GPU node.\n'
else
  log "Installing NVIDIA Device Plugin $NVIDIA_DEVICE_PLUGIN_VERSION"
  apply_target "$NVIDIA_DEVICE_PLUGIN_MANIFEST" "NVIDIA Device Plugin"
  kube -n kube-system rollout status daemonset/nvidia-device-plugin-daemonset --timeout="$WAIT_TIMEOUT" || die "NVIDIA Device Plugin DaemonSet did not become ready"
fi

log "Installing Kubeflow Training Operator $TRAINING_OPERATOR_VERSION"
apply_target "$TRAINING_OPERATOR_MANIFEST" "Training Operator"
kube -n kubeflow rollout status deployment/training-operator --timeout="$WAIT_TIMEOUT" || die "Training Operator did not become available"

if kube -n kueue-system get deployment/kueue-controller-manager >/dev/null 2>&1; then
  log "Reusing existing Kueue controller; not replacing its Deployment or selector"
  kube -n kueue-system rollout status deployment/kueue-controller-manager --timeout="$WAIT_TIMEOUT" || die "Kueue controller did not become available"
else
  log "Installing Kueue $KUEUE_VERSION"
  apply_target "$KUEUE_MANIFEST" "Kueue"
  kube -n kueue-system rollout status deployment/kueue-controller-manager --timeout="$WAIT_TIMEOUT" || die "Kueue controller did not become available"
fi

# Kueue v0.20.5 ships with the kubefloworg PyTorchJob integration commented out in
# both its helm values and its release manifests, so PyTorchJobs fall back to the
# Pod integration: each replica pod is queued and admitted on its own instead of
# the job as a whole. Enable it before any training job runs.
log "Ensuring Kueue manages PyTorchJob as a whole (kubeflow.org/pytorchjob integration)"
KUEUE_CONFIG_FILE="$ROOT_DIR/deploy/kueue/base/controller_manager_config.yaml"
[[ -f "$KUEUE_CONFIG_FILE" ]] || die "Missing $KUEUE_CONFIG_FILE"
if kube -n kueue-system get configmap/kueue-manager-config -o yaml 2>/dev/null \
  | grep -qE '^[[:space:]]*-[[:space:]]*"?kubeflow\.org/pytorchjob"?[[:space:]]*$'; then
  log "Kueue already integrates kubeflow.org/pytorchjob; leaving the controller untouched"
else
  log "Patching kueue-manager-config so PyTorchJob is scheduled as one unit"
  kube -n kueue-system create configmap kueue-manager-config \
    --from-file=controller_manager_config.yaml="$KUEUE_CONFIG_FILE" \
    --dry-run=client -o yaml | kube apply -f - >/dev/null
  kube -n kueue-system rollout restart deployment/kueue-controller-manager
  kube -n kueue-system rollout status deployment/kueue-controller-manager --timeout="$WAIT_TIMEOUT" \
    || die "Kueue controller did not come back with the PyTorchJob integration"
  sleep 5
  kube -n kueue-system get configmap/kueue-manager-config -o yaml \
    | grep -qE '^[[:space:]]*-[[:space:]]*"?kubeflow\.org/pytorchjob"?[[:space:]]*$' \
    || die "kueue-manager-config still lacks the kubeflow.org/pytorchjob framework"
fi

log "Applying platform namespaces, minimal RBAC, and persistent history storage"
kube apply -k "$ROOT_DIR/deploy/base"
for path in /data/vla-platform/datasets /data/vla-platform/checkpoints /data/vla-platform/history; do
  if ! run_root test -d "$path"; then
    run_root install -d -m 0755 "$path"
  fi
done

log "Applying $PROFILE Kueue queues and low/normal/high priorities"
kube apply -k "$ROOT_DIR/deploy/kueue/$PROFILE"
kube wait clusterqueue/vla-cluster-queue --for=condition=Active --timeout="$WAIT_TIMEOUT" || die "ClusterQueue vla-cluster-queue is not Active"

log "Waiting for nvidia.com/gpu to appear in node allocatable resources"
EXPECTED_GPUS=1
[[ "$PROFILE" == "a100" ]] && EXPECTED_GPUS=8
GPU_COUNT=""
for _ in $(seq 1 30); do
  GPU_COUNT=$(kube get nodes -o 'jsonpath={.items[*].status.allocatable.nvidia\.com/gpu}' 2>/dev/null || true)
  if [[ "$GPU_COUNT" =~ ^[0-9]+$ ]] && (( GPU_COUNT >= EXPECTED_GPUS )); then
    break
  fi
  sleep 5
done
[[ "$GPU_COUNT" =~ ^[0-9]+$ ]] || die "No numeric nvidia.com/gpu allocatable capacity found; inspect the device plugin and K3s runtime"
(( GPU_COUNT >= EXPECTED_GPUS )) || die "$PROFILE profile requires at least $EXPECTED_GPUS allocatable GPUs; found $GPU_COUNT"

kube get clusterqueue/vla-cluster-queue localqueue/default -n vla-training
printf '\nStack installation complete: %s GPU(s) allocatable, ClusterQueue quota profile=%s.\n' "$GPU_COUNT" "$PROFILE"
printf 'Next: sudo PROFILE=%s PLATFORM_API_TOKEN=<secret> scripts/deploy-web.sh\n' "$PROFILE"
printf 'For fully local Web deployment, use WEB_IMAGE_TAR or allow the script to build with local Docker.\n'
