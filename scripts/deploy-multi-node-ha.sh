#!/usr/bin/env bash
set -euo pipefail
# 首次部署 / 校验收尾脚本。
#
# ⚠️ 只在这台真实的多机集群上由人手动执行，**不要在本仓库的单卡开发机**上跑。
#    默认 --dry-run，未加 --apply 时不会修改集群；加 --apply 才会真正提交资源。
#
# 职责（与 install-stack.sh / deploy-web.sh 互补，不替代它们）：
#   1. 给多机 GPU 节点打 vla-platform.io/node-pool=multinode 标签（需要 sudo）；
#   2. 应用 deploy/kueue/multi-node（Topology + TAS ResourceFlavor + 多机队列）；
#   3. （可选）叠加 deploy/web/overlays/ha 的 Web 高可用；
#   4. 应用后逐项校验，并在失败时给出回滚命令。

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
MULTINODE_DIR="$ROOT_DIR/deploy/kueue/multi-node"
HA_OVERLAY="$ROOT_DIR/deploy/web/overlays/ha"
PROFILE="${PROFILE:-4090}"
MODE="dry-run"
APPLY_HA=0
LABEL_NODES=""
WAIT_TIMEOUT="${WAIT_TIMEOUT:-120s}"

usage() {
  cat <<'EOF'
用法：sudo PROFILE=4090|a100 [LABEL_NODES="node-1 node-2"] scripts/deploy-multi-node-ha.sh [--apply] [--with-ha]

默认 dry-run：只渲染并做 client/server dry-run，不写集群。
  --apply        真正 apply（需要集群写权限）
  --with-ha      同时叠加 deploy/web/overlays/ha（2 副本 + PDB + 反亲和）

环境变量：
  LABEL_NODES     需要打 vla-platform.io/node-pool=multinode 的节点名（空格分隔）；
                  不设置则跳过打标签，只应用 Kueue 资源。
  PROFILE         4090|a100，仅用于提示当前单机 profile（默认 4090）。
  WAIT_TIMEOUT    等待 ClusterQueue Active 的超时，默认 120s。

本脚本不会重启 kueue-controller-manager。启用 TAS feature gate 需要单独做：
  helm upgrade --install kueue <chart> -n kueue-system \
    -f deploy/kueue/multi-node/kueue-values-multi-node.yaml
  kubectl -n kueue-system rollout restart deployment/kueue-controller-manager
见 deploy/kueue/multi-node/README.md。
EOF
}

log() { printf '\n==> %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

while (( $# > 0 )); do
  case "$1" in
    --apply) MODE="apply" ;;
    --with-ha) APPLY_HA=1 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; die "未知参数：$1" ;;
  esac
  shift
done

case "$PROFILE" in
  4090|a100) ;;
  *) die "PROFILE 必须是 4090 或 a100" ;;
esac

if (( EUID == 0 )); then
  SUDO=()
elif command -v sudo >/dev/null 2>&1; then
  SUDO=(sudo)
else
  die "需要 root 或 sudo"
fi
run_root() { "${SUDO[@]}" "$@"; }

if command -v k3s >/dev/null 2>&1 || run_root test -x /usr/local/bin/k3s; then
  kube() { run_root k3s kubectl "$@"; }
else
  command -v kubectl >/dev/null 2>&1 || die "找不到 k3s 或 kubectl"
  kube() { kubectl "$@"; }
fi

kube get nodes --request-timeout=10s >/dev/null || die "连不上集群，检查 kubeconfig"
kube get crd topologies.kueue.x-k8s.io >/dev/null 2>&1 \
  || die "Kueue Topology CRD 不存在：先安装 Kueue（scripts/install-stack.sh）"
kube get crd pytorchjobs.kubeflow.org >/dev/null 2>&1 \
  || die "PyTorchJob CRD 不存在：先安装 Kubeflow Training Operator"

# --- 1. 节点标签 -------------------------------------------------------------
if [[ -n "$LABEL_NODES" ]]; then
  log "给节点打多机标签 vla-platform.io/node-pool=multinode"
  for node in $LABEL_NODES; do
    kube get node "$node" >/dev/null 2>&1 || die "节点不存在：$node"
    gpus=$(kube get node "$node" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null || true)
    [[ -n "$gpus" && "$gpus" != "0" ]] || die "节点 $node 没有可分配的 nvidia.com/gpu，不应加入多机池"
    if [[ "$MODE" == "apply" ]]; then
      kube label node "$node" "vla-platform.io/node-pool=multinode" --overwrite
    else
      printf '[dry-run] kubectl label node %s vla-platform.io/node-pool=multinode --overwrite（GPU=%s）\n' "$node" "$gpus"
    fi
  done
else
  warn "未设置 LABEL_NODES：跳过打标签。多机 ResourceFlavor 目前会匹配不到任何节点。"
fi

# --- 2. 多机 Kueue 资源 ------------------------------------------------------
log "校验 deploy/kueue/multi-node 渲染结果"
kube kustomize "$MULTINODE_DIR" >/dev/null || die "kustomize 渲染失败：$MULTINODE_DIR"

if [[ "$MODE" == "apply" ]]; then
  log "应用多机 Kueue 资源（Topology / ResourceFlavor / ClusterQueue / LocalQueue）"
  kube apply -k "$MULTINODE_DIR"
  if ! kube wait clusterqueue/vla-multinode --for=condition=Active --timeout="$WAIT_TIMEOUT"; then
    warn "ClusterQueue vla-multinode 未在 $WAIT_TIMEOUT 内变成 Active"
    kube -n vla-training describe clusterqueue vla-multinode || true
    kube -n vla-training get localqueue multinode || true
    die "多机队列未就绪；回滚命令：kubectl delete -k $MULTINODE_DIR"
  fi
else
  log "dry-run：不写入集群"
  kube apply --dry-run=client -k "$MULTINODE_DIR"
fi

# --- 3. 可选 Web 高可用 ------------------------------------------------------
if (( APPLY_HA == 1 )); then
  log "校验 deploy/web/overlays/ha"
  kube kustomize "$HA_OVERLAY" >/dev/null || die "kustomize 渲染失败：$HA_OVERLAY"
  if [[ "$MODE" == "apply" ]]; then
    kube apply -k "$HA_OVERLAY"
    kube -n vla-platform rollout status deployment/vla-web --timeout=300s \
      || die "Web 未在两副本下就绪；回滚：kubectl apply -k deploy/web/overlays/$PROFILE"
  else
    kube apply --dry-run=client -k "$HA_OVERLAY"
  fi
fi

# --- 4. 结果校验 -------------------------------------------------------------
log "校验结果"
kube get topology,resourceflavor,clusterqueue -A 2>/dev/null | grep -E "vla-multinode|gpu-multinode" || warn "未找到多机对象"
kube -n vla-training get localqueue multinode 2>/dev/null || warn "未找到 LocalQueue multinode"

TF_LABELS=$(kube get nodes -l vla-platform.io/node-pool=multinode --no-headers 2>/dev/null | wc -l | tr -d ' ')
printf '命中多机标签的节点数：%s\n' "$TF_LABELS"
if (( TF_LABELS < 2 )); then
  warn "多机训练需要 ≥2 个带标签的 GPU 节点；当前 $TF_LABELS 个。单机环境这是预期结果。"
fi

KUEUE_ARGS=$(kube -n kueue-system get deployment kueue-controller-manager \
  -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null || true)
if grep -q "TopologyAwareScheduling" <<< "$KUEUE_ARGS"; then
  printf 'TAS feature gate：已启用\n'
else
  warn "TAS feature gate 未启用：Topology 资源会被忽略，多机调度退化为普通 flavor 计数。"
  warn "启用方法见 deploy/kueue/multi-node/README.md（需要重启 kueue-controller-manager）。"
fi

log "完成。真实多机验证步骤见 deploy/kueue/multi-node/README.md，示例提交：examples/submit-multi-node.sh"