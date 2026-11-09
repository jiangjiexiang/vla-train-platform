#!/usr/bin/env bash
set -euo pipefail

PROFILE="${PROFILE:-4090}"
MIN_DISK_GB="${MIN_DISK_GB:-20}"
WEB_NODE_PORT="${WEB_NODE_PORT:-30080}"
# 多机检查在 PROFILE=4090|a100 下也默认开启（只读、不阻塞），但只有当集群里
# 至少 2 个节点时才可能「通过」；单台机器上会给出 WARN，提示这是预期的。
MULTINODE_CHECK="${MULTINODE_CHECK:-1}"
# 多机 profile 期望的节点标签，与 deploy/kueue/multi-node/queues.yaml 保持一致。
MULTINODE_NODE_LABEL="${MULTINODE_NODE_LABEL:-vla-platform.io/node-pool=multinode}"
# 期望的 NCCL 版本一致性检查依据：镜像内 torch.version.cuda 与 nccl 版本。
# 这里用 nvidia-smi 的驱动/运行时 CUDA 版本作为跨节点可比的近似值。
PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

usage() {
  cat <<'EOF'
Usage: PROFILE=4090|a100 [MIN_DISK_GB=20] [MULTINODE_CHECK=0|1] scripts/preflight.sh

Read-only Ubuntu single-node GPU/K3s preflight. No packages, services, or
configuration files are changed. The script exits non-zero if a required check
fails. Set MIN_DISK_GB to override the minimum free space on the root filesystem.

多机检查（只读）默认开启，覆盖契约第 4 节要求的三项：
  1. 每个节点的 nvidia.com/gpu 数量；
  2. 各节点 CUDA 驱动版本是否一致（NCCL 跨节点要求，不一致直接 FAIL）；
  3. Kueue Topology 与多机 ResourceFlavor 是否已配置（未启用时 WARN，不阻塞单机部署）。
单节点机器上第 3 项与「≥2 个 GPU 节点」检查会给出 WARN，这是预期结果。
设置 MULTINODE_CHECK=0 可跳过整组多机检查。
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

# ---------------------------------------------------------------------------
# 多机（multi-node）检查：只读 kubectl 查询，不会创建/修改任何资源。
# 设计原则：
#   - 单机环境不制造 FAIL：没有 ≥2 个 GPU 节点、没有 Topology 都只是 WARN，
#     这样本脚本在现有 4090 单机部署上仍然「通过」，可放心作为常规巡检；
#   - 只有「多机语义明确不一致」才 FAIL：例如节点间 CUDA 驱动版本不同，
#     或节点打了 multinode 标签但没有对应 ResourceFlavor。
# ---------------------------------------------------------------------------
# 从 kubectl get nodes -o json 的标准输入里输出每节点 GPU 数量汇总。
# 用函数 + here-doc 生成脚本文本，避免函数体内出现多层引号嵌套；
# 函数名以 _py_ 开头，不属于对外接口。
_py_node_gpu_summary() {
  cat <<'PY'
import json
import sys

GLOBAL_KEY = "nvidia.com/gpu"

try:
    items = json.load(sys.stdin)["items"]
except (ValueError, KeyError):
    sys.exit(1)


def count_gpus(node):
    allocatable = node.get("status", {}).get("allocatable") or {}
    for key, raw in allocatable.items():
        if key.startswith(GLOBAL_KEY):
            try:
                return int(raw)
            except (TypeError, ValueError):
                return 0
    return 0


gpu_nodes = 0
for node in items:
    name = node.get("metadata", {}).get("name", "")
    count = count_gpus(node)
    if count > 0:
        gpu_nodes += 1
    print("NODE_GPU\t%s\t%s" % (name, count))
print("GPU_NODE_COUNT\t%s" % gpu_nodes)
PY
}

multinode_check() {
  printf '\n== Multi-node readiness (read-only) ==\n'
  if [[ "$MULTINODE_CHECK" != "1" ]]; then
    info "MULTINODE_CHECK=$MULTINODE_CHECK; skipping multi-node checks"
    return 0
  fi
  if ! command -v kubectl >/dev/null 2>&1; then
    info "kubectl is unavailable; skipping multi-node checks"
    return 0
  fi
  if ! kubectl get nodes --request-timeout=5s >/dev/null 2>&1; then
    info "No reachable cluster; skipping multi-node checks"
    return 0
  fi

  local nodes_json gpu_nodes
  if ! nodes_json=$(kubectl get nodes -o json --request-timeout=10s 2>/dev/null); then
    warn "Cannot read nodes; skipping multi-node checks"
    return 0
  fi
  # 节点 GPU 数汇总交给一次 python3 调用完成，避免在 /tmp 写临时文件。
  local gpu_report
  if ! gpu_report=$(printf '%s' "$nodes_json" | python3 -c "$(_py_node_gpu_summary)"); then
    warn "Could not parse node allocatable resources; skipping GPU count check"
    gpu_report=""
  fi

  # 1) 每节点 GPU 数量（契约第 4 节：节点 GPU 数）
  while IFS=$'\t' read -r tag name value; do
    case "$tag" in
      NODE_GPU)
        if [[ "$value" =~ ^[0-9]+$ ]] && (( value > 0 )); then
          info "Node $name allocatable nvidia.com/gpu=$value"
        else
          info "Node $name has no allocatable nvidia.com/gpu (CPU-only or device plugin not ready)"
        fi
        ;;
      GPU_NODE_COUNT)
        # 这一行只有两列（tag + 计数），read 会把计数放进 name 而不是 value。
        gpu_nodes="${name}"
        ;;
    esac
  done <<< "$gpu_report"

  if [[ -z "${gpu_nodes:-}" ]]; then
    warn "Could not determine GPU node count from allocatable resources"
  elif (( gpu_nodes >= 2 )); then
    pass "至少 2 个 GPU 节点（$gpu_nodes 个），多机训练在同一集群内可行"
  else
    warn "只有 $gpu_nodes 个 GPU 节点；多机训练需要 ≥2 个 GPU 节点（单机环境这是预期结果）"
  fi

  # 2) 跨节点 CUDA 驱动版本一致性（NCCL 排查第一检查项）
  #    nvidia-device-plugin 会把驱动信息写进 node 资源 allocatable/状态；这里用
  #    kubectl 读节点标签/注解中可能的驱动版本。拿不到就 WARN，不伪造数据。
  local driver_lines
  driver_lines=$(printf '%s' "$nodes_json" | python3 -c '
import json, sys
items = json.load(sys.stdin)["items"]
for node in items:
    name = node["metadata"]["name"]
    labels = node["metadata"].get("labels", {}) or {}
    annotations = node["metadata"].get("annotations", {}) or {}
    driver = (
        labels.get("nvidia.com/cuda.driver.major")
        or annotations.get("nvidia.com/cuda.driver.version")
        or labels.get("nvidia.com/cuda.driver.version")
        or ""
    )
    runtime = labels.get("nvidia.com/cuda.runtime-version.full", "")
    if not driver and runtime:
        # nvidia-device-plugin 不一定暴露驱动版本；退而用 CUDA 运行时版本，
        # 只要比较基准在所有节点上一致，就能检测出「镜像/CUDA 不一致」。
        driver = "cuda-" + runtime
    print(f"{name}\t{driver}\t{runtime}")
' 2>/dev/null || true)
  if [[ -z "$driver_lines" ]]; then
    warn "Cannot read node GPU driver labels; verify driver/CUDA versions on every node manually (nvidia-smi)"
  else
    local driver_versions
    # 只统计「有版本信息」的节点；unknown 节点不参与比较，也不伪装成版本。
    # 注意管道顺序：先过滤空值再 cut，否则 cut 会把空行一起去掉且 grep 计数为 0。
    driver_versions=$(printf '%s\n' "$driver_lines" | cut -f2 | grep -E '^(cuda-)?[0-9]' | sort -u || true)
    local known unknown_count
    known=$(printf '%s\n' "$driver_versions" | grep -c . || true)
    unknown_count=$(printf '%s\n' "$driver_lines" | cut -f2 | grep -c '^$' || true)
    while IFS=$'\t' read -r name driver runtime; do
      info "Node $name driver=${driver:-unknown} cuda-runtime=${runtime:-unknown}"
    done <<< "$driver_lines"
    if (( known == 0 )); then
      warn "No node exposes a driver version label; compare 'nvidia-smi' output on every node instead"
    elif (( known > 1 )); then
      fail "GPU 驱动版本在节点间不一致（$(printf '%s' "$driver_versions" | tr '\n' ' ')）；NCCL 跨节点通信不可靠，先统一驱动"
    elif (( unknown_count > 0 )); then
      warn "只有部分节点上报驱动版本（缺失 $unknown_count 个）；请在缺失节点上运行 nvidia-smi 人工确认"
    else
      pass "所有 GPU 节点报告同一驱动版本 $driver_versions（NCCL 跨节点前置条件满足）"
    fi
  fi

  # 3) Kueue 多机配置：Topology + ResourceFlavor(topologyName)
  if ! kubectl get crd topologies.kueue.x-k8s.io >/dev/null 2>&1; then
    warn "Kueue Topology CRD 不存在；多机 profile (deploy/kueue/multi-node) 尚未安装"
    return 0
  fi
  local topology_names flavor_count
  topology_names=$(kubectl get topologies.kueue.x-k8s.io -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true)
  if [[ -z "$topology_names" ]]; then
    warn "没有 Kueue Topology 资源；多机 TAS 调度不可用（应用 deploy/kueue/multi-node 后再检查）"
  else
    pass "Kueue Topology 已配置：$(printf '%s' "$topology_names" | tr '\n' ' ')"
  fi
  # 打了多机标签却没有对应 flavor = 这些节点会永远无法被多机队列使用，必须 FAIL。
  local labeled_nodes
  labeled_nodes=$(printf '%s' "$nodes_json" | python3 -c '
import json, sys
key, _, value = sys.argv[1].partition("=")
items = json.load(sys.stdin)["items"]
for node in items:
    labels = node["metadata"].get("labels", {}) or {}
    if labels.get(key) == value:
        print(node["metadata"]["name"])
' "$MULTINODE_NODE_LABEL" 2>/dev/null || true)
  flavor_count=$(kubectl get resourceflavors.kueue.x-k8s.io -o json 2>/dev/null \
    | python3 -c 'import json,sys; print(sum(1 for f in json.load(sys.stdin)["items"] if (f.get("spec") or {}).get("topologyName")))' 2>/dev/null || echo 0)
  if [[ -n "$labeled_nodes" ]]; then
    if (( flavor_count > 0 )); then
      pass "多机标签节点：$(printf '%s' "$labeled_nodes" | tr '\n' ' ')(对应 TAS ResourceFlavor: $flavor_count 个)"
    else
      fail "节点已打 $MULTINODE_NODE_LABEL 标签，但没有任何 ResourceFlavor 设置 topologyName；这些 GPU 不会被多机队列调度"
    fi
  else
    warn "没有节点带 $MULTINODE_NODE_LABEL 标签；多机 ResourceFlavor 目前匹配不到任何节点（单机环境预期）"
  fi

  # 4) TAS feature gate：只读检查 controller 启动参数，不做任何重启。
  local kueue_args
  kueue_args=$(kubectl -n kueue-system get deployment kueue-controller-manager \
    -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null || true)
  if [[ -z "$kueue_args" ]]; then
    warn "Cannot read kueue-controller-manager args (Kueue not installed or RBAC denies); TAS state unknown"
  elif grep -q "TopologyAwareScheduling" <<< "$kueue_args"; then
    pass "Kueue 已启用 TopologyAwareScheduling feature gate"
  else
    warn "Kueue 未启用 TopologyAwareScheduling；多机 TAS 不会生效（见 deploy/kueue/multi-node/README.md，启用需重启 controller）"
  fi
  info "单机环境无法验证真实多机训练；完整验证步骤见 deploy/kueue/multi-node/README.md"
}

multinode_check

printf '\n== Preflight summary ==\n'
printf 'PASS=%d WARN=%d FAIL=%d\n' "$PASS_COUNT" "$WARN_COUNT" "$FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then
  printf 'Preflight failed. Resolve every [FAIL] item before installation.\n' >&2
  exit 1
fi
printf 'Preflight passed with %d warning(s). Review warnings before changing the host.\n' "$WARN_COUNT"
