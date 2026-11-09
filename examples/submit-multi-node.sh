#!/usr/bin/env bash
set -euo pipefail
# 提交 2 节点各 1 卡的 smoke 训练（多机 Kueue 队列）。
#
# ⚠️ 前置条件（缺一不可，见 deploy/kueue/multi-node/README.md）：
#   1. 集群里至少 2 个 GPU 节点，且已打 vla-platform.io/node-pool=multinode 标签；
#   2. deploy/kueue/multi-node 已应用（LocalQueue multinode 存在）；
#   3. kueue-controller-manager 已启用 TopologyAwareScheduling（否则不保证跨节点）；
#   4. 每个节点都 import 了 smoke 镜像（无镜像仓库环境）：
#        docker save vla-smoke:latest | sudo k3s ctr -n k8s.io images import -
#   5. Training Operator 已安装。
#
# 本脚本只提交 PyTorchJob，不修改任何集群配置。
# 单卡开发机上运行会失败在「inspect：ClusterQueue 不存在 / 节点数 < 2 的检查」，
# 这是预期行为，不是 bug。

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
JOB_NAME="${JOB_NAME:-multi-node-smoke}"
NODES="${NODES:-2}"
GPUS_PER_NODE="${GPUS_PER_NODE:-1}"
STEPS="${STEPS:-20}"
SLEEP="${SLEEP:-0}"
IMAGE="${IMAGE:-vla-smoke:latest}"
QUEUE="${QUEUE:-multinode}"
NCCL_IFNAME="${NCCL_IFNAME:-eth0}"
NAMESPACE="${NAMESPACE:-vla-training}"
WAIT_SECONDS="${WAIT_SECONDS:-900}"
MODE="${MODE:-submit}" # submit | inspect | delete

usage() {
  cat <<'EOF'
用法：MODE=submit|inspect|delete [NODES=2] [GPUS_PER_NODE=1] examples/submit-multi-node.sh

  MODE=inspect  只检查前置条件并打印将要提交的 PyTorchJob，不创建资源（默认 submit）
  MODE=submit   创建 PyTorchJob（幂等：先渲染一遍，已存在时提示）
  MODE=delete   删除本次提交的 PyTorchJob

环境变量：
  JOB_NAME       任务名，默认 multi-node-smoke
  NODES          节点数，默认 2（= master 1 + worker N-1）
  GPUS_PER_NODE  每节点卡数，默认 1（与契约 1.7 的 gpus_per_node 对应）
  STEPS/SLEEP    smoke 步数/每步休眠秒数
  IMAGE          smoke 镜像，默认 vla-smoke:latest
  QUEUE          LocalQueue，默认 multinode
  NCCL_IFNAME    跨节点 NCCL 使用的网卡名，默认 eth0
  NAMESPACE      目标命名空间，默认 vla-training
  WAIT_SECONDS   等待结果的秒数（仅 submit 末尾提示用）
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ $# -gt 0 ]]; then
  usage >&2
  printf '未知参数：%s\n' "$1" >&2
  exit 2
fi

for number in "$NODES" "$GPUS_PER_NODE" "$STEPS" "$WAIT_SECONDS"; do
  [[ "$number" =~ ^[0-9]+$ ]] || { printf '[ERROR] 需要整数：%s\n' "$number" >&2; exit 2; }
done
(( NODES >= 2 )) || { printf '[ERROR] NODES 必须 ≥2（单机请用 examples/submit-4090.sh）\n' >&2; exit 2; }
(( GPUS_PER_NODE >= 1 )) || { printf '[ERROR] GPUS_PER_NODE 必须 ≥1\n' >&2; exit 2; }

if [[ -n "${KUBECTL_BIN:-}" ]]; then
  kube() { "$KUBECTL_BIN" "$@"; }
elif command -v kubectl >/dev/null 2>&1 && kubectl get nodes --request-timeout=5s >/dev/null 2>&1; then
  kube() { kubectl "$@"; }
elif command -v k3s >/dev/null 2>&1; then
  kube() { sudo k3s kubectl "$@"; }
else
  printf '[ERROR] 找不到可用的 kubectl/k3s\n' >&2
  exit 1
fi

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

WORKER_REPLICAS=$((NODES - 1))
TOTAL_GPUS=$((NODES * GPUS_PER_NODE))
MASTER_DNS="${JOB_NAME}-master-0.${JOB_NAME}-master.${NAMESPACE}.svc"
MASTER_PORT="${MASTER_PORT:-23456}"

# --- 前置检查（只读） --------------------------------------------------------
info "检查多机前置条件"
kube get crd pytorchjobs.kubeflow.org >/dev/null 2>&1 \
  || die "PyTorchJob CRD 不存在：先安装 Training Operator"
kube -n "$NAMESPACE" get localqueue "$QUEUE" >/dev/null 2>&1 \
  || die "LocalQueue $QUEUE 不存在于 $NAMESPACE：先应用 deploy/kueue/multi-node"
labeled=$(kube get nodes -l vla-platform.io/node-pool=multinode --no-headers 2>/dev/null | wc -l | tr -d ' ')
if (( labeled < NODES )); then
  die "带多机标签的节点只有 $labeled 个，NODES=$NODES 无法满足；先在 ≥$NODES 个 GPU 节点上打标签"
fi
info "多机标签节点：$labeled 个"

if kube -n kueue-system get deployment kueue-controller-manager >/dev/null 2>&1; then
  kueue_args=$(kube -n kueue-system get deployment kueue-controller-manager \
    -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null || true)
  if grep -q "TopologyAwareScheduling" <<< "$kueue_args"; then
    info "TAS feature gate 已启用"
  else
    warn "TAS feature gate 未启用：多机 Pod 不保证落到不同节点（反亲和只是防御）。"
  fi
fi

# --- 渲染 PyTorchJob ---------------------------------------------------------
# 用远程 heredoc 生成 YAML 会更难维护，这里直接内联渲染；注释保留在 header。
MANIFEST=$(mktemp --suffix=.yaml)
trap 'rm -f "$MANIFEST"' EXIT
render_pytorchjob() {
  local role replicas
  {
    cat <<EOF
# 由 examples/submit-multi-node.sh 生成：$NODES 节点 × 每节点 $GPUS_PER_NODE 卡
apiVersion: kubeflow.org/v1
kind: PyTorchJob
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: vla-train-platform
    app.kubernetes.io/component: multi-node-smoke
    kueue.x-k8s.io/queue-name: ${QUEUE}
spec:
  pytorchReplicaSpecs:
EOF
    local replicas_list=("Master:1" "Worker:${WORKER_REPLICAS}")
    for entry in "${replicas_list[@]}"; do
      role=${entry%%:*}
      replicas=${entry##*:}
      cat <<EOF
    ${role}:
      replicas: ${replicas}
      restartPolicy: OnFailure
      template:
        metadata:
          labels:
            app.kubernetes.io/component: multi-node-smoke
        spec:
          runtimeClassName: nvidia
          securityContext:
            runAsUser: 10001
            runAsGroup: 10001
            fsGroup: 10001
            seccompProfile:
              type: RuntimeDefault
          # 防御性反亲和：Kueue 计算 flavor 用量时不扣减 required 反亲和的资源，
          # 所以它只作为兜底，真正的跨节点保证来自 TAS（topologyName + topologyRequest）。
          affinity:
            podAntiAffinity:
              requiredDuringSchedulingIgnoredDuringExecution:
                - labelSelector:
                    matchLabels:
                      app.kubernetes.io/component: multi-node-smoke
                  topologyKey: kubernetes.io/hostname
          containers:
            - name: pytorch
              image: ${IMAGE}
              imagePullPolicy: IfNotPresent
              command:
                - python
                - /app/train.py
                - --distributed
                - --steps
                - "${STEPS}"
                - --sleep
                - "${SLEEP}"
              env:
                - name: PYTHONUNBUFFERED
                  value: "1"
                - name: JOB_NAME
                  value: ${JOB_NAME}
                - name: CHECKPOINT_DIR
                  value: /checkpoints/${JOB_NAME}
                - name: MASTER_ADDR
                  value: ${MASTER_DNS}
                - name: MASTER_PORT
                  value: "${MASTER_PORT}"
                - name: NCCL_SOCKET_IFNAME
                  value: ${NCCL_IFNAME}
                - name: NCCL_DEBUG
                  value: INFO
              resources:
                limits:
                  nvidia.com/gpu: ${GPUS_PER_NODE}
                requests:
                  nvidia.com/gpu: ${GPUS_PER_NODE}
              volumeMounts:
                - name: datasets
                  mountPath: /datasets
                  readOnly: true
                - name: checkpoints
                  mountPath: /checkpoints
          volumes:
            - name: datasets
              hostPath:
                path: /data/vla-platform/datasets
                type: Directory
            - name: checkpoints
              hostPath:
                path: /data/vla-platform/checkpoints
                type: Directory
EOF
    done
  } > "$MANIFEST"
}

render_pytorchjob
kube apply --dry-run=client -f "$MANIFEST" >/dev/null 2>&1 || die "生成的 PyTorchJob 无法通过 client dry-run"

# --- 执行 --------------------------------------------------------------------
case "$MODE" in
  inspect)
    info "NODES=$NODES GPUS_PER_NODE=$GPUS_PER_NODE 总卡数=$TOTAL_GPUS worker 副本=$WORKER_REPLICAS"
    sed 's/^/    /' "$MANIFEST"
    ;;
  delete)
    kube -n "$NAMESPACE" delete pytorchjob "$JOB_NAME" --ignore-not-found
    ;;
  submit)
    kube apply -f "$MANIFEST"
    info "已提交。观察命令："
    cat <<EOF
    kubectl -n $NAMESPACE get pytorchjob $JOB_NAME -o wide
    kubectl -n $NAMESPACE get pods -l app.kubernetes.io/component=multi-node-smoke -o wide
    kubectl -n $NAMESPACE get workloads.kueue.x-k8s.io -l 'kueue.x-k8s.io/job-name='$JOB_NAME -o yaml | grep -A8 topologyAssignment
    # rank 0 日志里必须出现：
    #   LAUNCH_MODE multi-node world_size=$TOTAL_GPUS
    #   NCCL_ROUNDTRIP_OK ...
    #   DDP_VERIFIED world_size=$TOTAL_GPUS ... nodes=$NODES mode=multi-node backend=nccl
    kubectl -n $NAMESPACE logs -l app.kubernetes.io/component=multi-node-smoke --all-containers --prefix | grep -E 'RANK_INFO|NCCL_|DDP_VERIFIED'
    # 等待完成并取退出状态
    kubectl -n $NAMESPACE wait --for=condition=Complete pytorchjob/$JOB_NAME --timeout=${WAIT_SECONDS}s
EOF
    ;;
  *)
    die "MODE 必须是 submit / inspect / delete"
    ;;
esac