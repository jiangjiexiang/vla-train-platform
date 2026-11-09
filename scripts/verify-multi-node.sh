#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
MULTINODE_DIR="$ROOT_DIR/deploy/kueue/multi-node"
HA_OVERLAY="$ROOT_DIR/deploy/web/overlays/ha"
EXPECTED_GPUS=${MULTINODE_EXPECTED_GPUS:-2}
LOCAL_QUEUE="${MULTINODE_LOCAL_QUEUE:-multinode}"
CLUSTER_QUEUE="${MULTINODE_CLUSTER_QUEUE:-vla-multinode}"
TOPOLOGY_NAME="${MULTINODE_TOPOLOGY:-vla-multinode}"
FLAVOR_NAME="${MULTINODE_FLAVOR:-gpu-multinode}"
NODE_LABEL="${MULTINODE_NODE_LABEL:-vla-platform.io/node-pool=multinode}"
DRY_RUN="${DRY_RUN:-0}"
PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

usage() {
  cat <<'EOF'
用法：bash scripts/verify-multi-node.sh [--apply-dry-run] [--help]

多机（multi-node）与 Web 高可用配置的**静态校验**。默认零副作用：
不会 apply 任何资源，也不会连接集群时修改任何东西。

检查项：
  1. bash -n 所有改动的 shell 脚本；
  2. python3 -m py_compile 训练脚本与校验脚本；
  3. kubectl kustomize 渲染 deploy/kueue/multi-node 与 deploy/web/overlays/ha；
  4. 渲染结果的结构断言（topologyRequest 用到的 Topology/Flavor/Queue 是否齐全、
     HA overlay 是否 2 副本 + PDB + 反亲和 + 拓扑分布 + 探针）；
  5. 假 torch 驱动 train.py 的 rendezvous 场景（多机 / torchrun / 单机 / 报错路径）；
  6. 若集群可连：kubectl apply --dry-run=client / --dry-run=server（不写 etcd）。

参数：
  --apply-dry-run   额外执行 kubectl apply --dry-run=client 与 --dry-run=server。
                    默认也会尝试，连不上集群时自动跳过（不 FAIL）。

环境变量：
  MULTINODE_EXPECTED_GPUS  多机 ClusterQueue 期望的 quota（默认 2，= 节点数 × 每节点卡数）。
  KUBECTL_BIN              指定 kubectl；默认用 PATH 里的 kubectl。
  SKIP_RENDEZVOUS=1        跳过 train.py 的假 torch 场景测试。

本脚本**不能**证明真实多机可用。真正的多机验证见
deploy/kueue/multi-node/README.md 的「如何验证」。
EOF
}

pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf '[PASS] %s\n' "$*"; }
warn() { WARN_COUNT=$((WARN_COUNT + 1)); printf '[WARN] %s\n' "$*"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf '[FAIL] %s\n' "$*" >&2; }
info() { printf '[INFO] %s\n' "$*"; }

while (( $# > 0 )); do
  case "$1" in
    --apply-dry-run) DRY_RUN=1 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; printf '未知参数：%s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

command -v python3 >/dev/null 2>&1 || { printf '[FAIL] 缺少 python3\n' >&2; exit 1; }

KUBECTL=""
if [[ -n "${KUBECTL_BIN:-}" ]]; then
  KUBECTL="$KUBECTL_BIN"
elif command -v kubectl >/dev/null 2>&1; then
  KUBECTL=kubectl
elif command -v k3s >/dev/null 2>&1; then
  KUBECTL="k3s kubectl"
fi

# ---------------------------------------------------------------------------
# 1. Shell 语法
# ---------------------------------------------------------------------------
printf '\n== Shell 语法检查 ==\n'
SCRIPTS=(
  "$ROOT_DIR/scripts/preflight.sh"
  "$ROOT_DIR/scripts/verify-multi-node.sh"
  "$ROOT_DIR/scripts/deploy-multi-node-ha.sh"
  "$ROOT_DIR/scripts/check-preflight-multinode.sh"
  "$ROOT_DIR/examples/submit-multi-node.sh"
)
for script in "${SCRIPTS[@]}"; do
  if [[ ! -f "$script" ]]; then
    warn "脚本不存在，跳过：${script#$ROOT_DIR/}"
    continue
  fi
  if bash -n "$script" 2>/tmp/vla-verify-mn.err; then
    pass "bash -n ${script#$ROOT_DIR/}"
  else
    fail "bash -n ${script#$ROOT_DIR/}"
    sed 's/^/       /' /tmp/vla-verify-mn.err >&2 || true
  fi
done
rm -f /tmp/vla-verify-mn.err

# ---------------------------------------------------------------------------
# 2. Python 语法
# ---------------------------------------------------------------------------
printf '\n== Python 语法检查 ==\n'
for py in \
  "$ROOT_DIR/images/smoke-train/train.py" \
  "$ROOT_DIR/scripts/check-smoke-train-rendezvous.py"; do
  if [[ ! -f "$py" ]]; then
    warn "文件不存在，跳过：${py#$ROOT_DIR/}"
    continue
  fi
  if python3 -m py_compile "$py"; then
    pass "py_compile ${py#$ROOT_DIR/}"
  else
    fail "py_compile ${py#$ROOT_DIR/}"
  fi
done
# py_compile 会写 __pycache__；必须立刻清理，否则后面的 rendezvous 步骤可能
# 加载到陈旧 .pyc（源码 mtime 与 .pyc 记录相同时 import 不走源码重编译）。
find "$ROOT_DIR/images/smoke-train" "$ROOT_DIR/scripts" -maxdepth 1 -name '__pycache__' -type d \
  -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# 3. kustomize 渲染
# ---------------------------------------------------------------------------
printf '\n== kustomize 渲染 ==\n'

render() {
  local dir=$1 out=$2
  # shellcheck disable=SC2086
  $KUBECTL kustomize "$dir" > "$out" 2>"$out.err"
}

MULTINODE_RENDER=$(mktemp --suffix=.yaml)
HA_RENDER=$(mktemp --suffix=.yaml)
trap 'rm -f "$MULTINODE_RENDER" "$MULTINODE_RENDER.err" "$HA_RENDER" "$HA_RENDER.err"' EXIT

if [[ -z "$KUBECTL" ]]; then
  warn "找不到 kubectl/k3s，无法执行 kustomize 渲染；剩余的静态断言会跳过"
else
  if render "$MULTINODE_DIR" "$MULTINODE_RENDER"; then
    pass "kubectl kustomize deploy/kueue/multi-node"
  else
    fail "kubectl kustomize deploy/kueue/multi-node"
    sed 's/^/       /' "$MULTINODE_RENDER.err" >&2 || true
  fi
  if render "$HA_OVERLAY" "$HA_RENDER"; then
    pass "kubectl kustomize deploy/web/overlays/ha"
  else
    fail "kubectl kustomize deploy/web/overlays/ha"
    sed 's/^/       /' "$HA_RENDER.err" >&2 || true
  fi
fi

# ---------------------------------------------------------------------------
# 4. 渲染结果结构断言
# ---------------------------------------------------------------------------
printf '\n== 渲染结果结构断言 ==\n'
ASSERT_PY=$(mktemp --suffix=.py)
cat > "$ASSERT_PY" <<'PY'
import json
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - 环境缺 PyYAML 时由调用方降级
    print("SKIP PyYAML is unavailable")
    sys.exit(3)


def load(path):
    with open(path, encoding="utf-8") as handle:
        return [doc for doc in yaml.safe_load_all(handle) if doc]


def find(docs, kind, name=None):
    return [
        d for d in docs
        if d.get("kind") == kind and (name is None or d.get("metadata", {}).get("name") == name)
    ]


problems = []
notes = []

try:
    multi = load(sys.argv[1])
except yaml.YAMLError as exc:
    print("FAIL multi-node render is not valid YAML: %s" % exc)
    sys.exit(1)

expected_gpus = int(sys.argv[3])
topologies = find(multi, "Topology")
flavors = find(multi, "ResourceFlavor")
cluster_queues = find(multi, "ClusterQueue")
local_queues = find(multi, "LocalQueue")

if len(topologies) != 1:
    problems.append("expected exactly 1 Topology, got %d" % len(topologies))
else:
    levels = topologies[0].get("spec", {}).get("levels") or []
    labels = [level.get("nodeLabel") for level in levels if isinstance(level, dict)]
    if not labels:
        problems.append("Topology.spec.levels is empty")
    elif labels[-1] != "kubernetes.io/hostname":
        problems.append("Topology lowest level must be kubernetes.io/hostname, got %r" % (labels[-1],))
    notes.append("Topology levels=%s" % labels)

tas_flavors = [f for f in flavors if (f.get("spec") or {}).get("topologyName")]
if not tas_flavors:
    problems.append("no ResourceFlavor sets spec.topologyName (TAS would be off)")
else:
    for flavor in tas_flavors:
        spec = flavor.get("spec") or {}
        if not spec.get("nodeLabels"):
            problems.append(
                "ResourceFlavor %s has topologyName but no nodeLabels: it would match every node, "
                "including the single-GPU 4090 box" % flavor["metadata"]["name"]
            )
        else:
            notes.append(
                "ResourceFlavor %s nodeLabels=%s topologyName=%s"
                % (flavor["metadata"]["name"], spec["nodeLabels"], spec["topologyName"])
            )

if len(cluster_queues) != 1:
    problems.append("expected exactly 1 ClusterQueue, got %d" % len(cluster_queues))
else:
    spec = cluster_queues[0].get("spec") or {}
    quotas = []
    for group in spec.get("resourceGroups") or []:
        for flavor in group.get("flavors") or []:
            for resource in flavor.get("resources") or []:
                if resource.get("name") == "nvidia.com/gpu":
                    quotas.append(int(resource.get("nominalQuota", 0)))
    if quotas != [expected_gpus]:
        problems.append(
            "ClusterQueue nvidia.com/gpu quota is %s, expected [%d] (= nodes x gpus_per_node)"
            % (quotas, expected_gpus)
        )
    else:
        notes.append("ClusterQueue %s quota=%d GPU" % (cluster_queues[0]["metadata"]["name"], expected_gpus))
    if not spec.get("namespaceSelector"):
        notes.append("WARN ClusterQueue has no namespaceSelector: it applies to every namespace")

if len(local_queues) != 1:
    problems.append("expected exactly 1 LocalQueue, got %d" % len(local_queues))
else:
    notes.append(
        "LocalQueue %s -> %s"
        % (local_queues[0]["metadata"]["name"], local_queues[0].get("spec", {}).get("clusterQueue"))
    )

# 单机 profile 不能被多机配置污染：多机对象名必须都带 multinode 语义前缀。
for doc in multi:
    name = doc.get("metadata", {}).get("name", "")
    if doc.get("kind") in ("ClusterQueue", "LocalQueue", "ResourceFlavor", "Topology"):
        if doc.get("kind") in ("ClusterQueue", "ResourceFlavor", "Topology") and "multinode" not in name:
            problems.append("%s %s would collide with the single-node profile" % (doc.get("kind"), name))

# --- HA overlay ---
try:
    ha = load(sys.argv[2])
except yaml.YAMLError as exc:
    print("FAIL ha render is not valid YAML: %s" % exc)
    sys.exit(1)

deployments = find(ha, "Deployment", "vla-web")
if not deployments:
    problems.append("HA overlay does not render Deployment/vla-web")
else:
    dep = deployments[0]
    if dep.get("spec", {}).get("replicas") != 2:
        problems.append("HA replicas is %r, expected 2" % (dep.get("spec", {}).get("replicas"),))
    strategy = dep.get("spec", {}).get("strategy") or {}
    if strategy.get("type") != "RollingUpdate":
        problems.append("HA strategy is %r, expected RollingUpdate" % (strategy.get("type"),))
    pod = dep["spec"]["template"]["spec"]
    if not (pod.get("affinity") or {}).get("podAntiAffinity"):
        problems.append("HA Deployment has no podAntiAffinity")
    if not pod.get("topologySpreadConstraints"):
        problems.append("HA Deployment has no topologySpreadConstraints")
    container = pod["containers"][0]
    for probe in ("readinessProbe", "livenessProbe"):
        http_get = (container.get(probe) or {}).get("httpGet") or {}
        if http_get.get("path") != "/api/v1/health":
            problems.append("%s path is %r, expected /api/v1/health" % (probe, http_get.get("path")))
    notes.append("HA Deployment replicas=2 strategy=RollingUpdate")

pdbs = find(ha, "PodDisruptionBudget", "vla-web")
if not pdbs:
    problems.append("HA overlay does not render PodDisruptionBudget/vla-web")
else:
    spec = pdbs[0].get("spec") or {}
    if spec.get("minAvailable") != 1:
        problems.append("HA PDB minAvailable is %r, expected 1" % (spec.get("minAvailable"),))
    if "maxUnavailable" in spec:
        problems.append("HA PDB must not set maxUnavailable together with minAvailable")
    notes.append("HA PDB minAvailable=1")

services = find(ha, "Service", "vla-web")
if not services:
    problems.append("HA overlay does not render Service/vla-web")
else:
    ports = services[0].get("spec", {}).get("ports") or [{}]
    notes.append("Service type=%s nodePort=%s" % (services[0]["spec"].get("type"), ports[0].get("nodePort")))

# 历史库必须是 RWO（SQLite 单写者），HA 不能用 RWX 掩盖问题。
if deployments:
    names = {v.get("name") for v in deployments[0]["spec"]["template"]["spec"].get("volumes") or []}
    if "history" not in names:
        problems.append("HA Deployment lost the history volume")

for note in notes:
    print("NOTE %s" % note)
for problem in problems:
    print("PROBLEM %s" % problem)
sys.exit(1 if problems else 0)
PY

if [[ -s "$MULTINODE_RENDER" && -s "$HA_RENDER" ]]; then
  if ! python3 -c 'import yaml' >/dev/null 2>&1; then
    warn "本机 python3 缺少 PyYAML，跳过结构断言（pip install pyyaml 后重跑）"
  elif python3 "$ASSERT_PY" "$MULTINODE_RENDER" "$HA_RENDER" "$EXPECTED_GPUS" > /tmp/vla-assert.out 2>&1; then
    while IFS= read -r line; do
      case "$line" in
        NOTE*) info "${line#NOTE }" ;;
        SKIP*) warn "${line#SKIP }" ;;
      esac
    done < /tmp/vla-assert.out
    pass "多机 profile 与 HA overlay 结构断言全部通过"
  else
    while IFS= read -r line; do
      case "$line" in
        NOTE*) info "${line#NOTE }" ;;
        PROBLEM*) fail "${line#PROBLEM }" ;;
        FAIL*) fail "${line#FAIL }" ;;
        SKIP*) warn "${line#SKIP }" ;;
      esac
    done < /tmp/vla-assert.out
  fi
  rm -f /tmp/vla-assert.out
else
  warn "渲染产物为空，跳过结构断言"
fi
rm -f "$ASSERT_PY"

# ---------------------------------------------------------------------------
# 5. train.py rendezvous 场景（假 torch，不下载 PyTorch）
# ---------------------------------------------------------------------------
printf '\n== train.py rendezvous 场景 ==\n'
if [[ "${SKIP_RENDEZVOUS:-0}" == "1" ]]; then
  warn "SKIP_RENDEZVOUS=1，跳过"
elif [[ ! -f "$ROOT_DIR/scripts/check-smoke-train-rendezvous.py" ]]; then
  warn "缺少 scripts/check-smoke-train-rendezvous.py，跳过"
elif python3 "$ROOT_DIR/scripts/check-smoke-train-rendezvous.py" > /tmp/vla-rendezvous.out 2>&1; then
  pass "train.py rendezvous 全场景通过（多机 rank0/rank1、torchrun、单机、缺 MASTER_ADDR/PORT 报错）"
  tail -n 1 /tmp/vla-rendezvous.out | sed 's/^/       /'
else
  fail "train.py rendezvous 场景失败"
  sed 's/^/       /' /tmp/vla-rendezvous.out >&2
fi
rm -f /tmp/vla-rendezvous.out
find "$ROOT_DIR/scripts" -maxdepth 1 -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# 5b. preflight 多机分支（假 kubectl，不接触真实集群）
# ---------------------------------------------------------------------------
printf '\n== preflight 多机分支 ==\n'
if [[ "${SKIP_RENDEZVOUS:-0}" == "1" ]]; then
  info "SKIP_RENDEZVOUS=1，跳过（该测试与 rendezvous 测试共用假 kubectl 机制）"
elif [[ ! -f "$ROOT_DIR/scripts/check-preflight-multinode.sh" ]]; then
  warn "缺少 scripts/check-preflight-multinode.sh，跳过"
elif bash "$ROOT_DIR/scripts/check-preflight-multinode.sh" > /tmp/vla-preflight-mn.out 2>&1; then
  pass "preflight 多机分支：一致/缺失/不一致 三种输入行为符合预期"
  grep -E '^\[OK' /tmp/vla-preflight-mn.out | sed 's/^/       /'
else
  fail "preflight 多机分支行为不符合预期"
  sed 's/^/       /' /tmp/vla-preflight-mn.out >&2
fi
rm -f /tmp/vla-preflight-mn.out

# ---------------------------------------------------------------------------
# 6. 集群 dry-run（可选，不写 etcd）
# ---------------------------------------------------------------------------
printf '\n== 集群 dry-run（不写入任何资源）==\n'
CLUSTER_REACHABLE=0
if [[ -z "$KUBECTL" ]]; then
  warn "找不到 kubectl/k3s，跳过集群 dry-run"
  # shellcheck disable=SC2086
elif $KUBECTL get nodes -o json --request-timeout=5s >/dev/null 2>&1; then
  CLUSTER_REACHABLE=1
else
  warn "无法连接集群（或当前用户无权限），跳过集群 dry-run"
fi

if (( CLUSTER_REACHABLE == 1 )) && [[ "$DRY_RUN" == "1" ]]; then
  # shellcheck disable=SC2086
  if $KUBECTL apply --dry-run=client -k "$MULTINODE_DIR" >/dev/null 2>&1; then
    pass "kubectl apply --dry-run=client -k deploy/kueue/multi-node"
  else
    fail "kubectl apply --dry-run=client -k deploy/kueue/multi-node"
  fi
  # shellcheck disable=SC2086
  if $KUBECTL apply --dry-run=server -k "$MULTINODE_DIR" >/dev/null 2>&1; then
    pass "kubectl apply --dry-run=server -k deploy/kueue/multi-node（CRD schema + webhook 校验）"
  else
    warn "server-side dry-run 失败（集群可能不允许 list/watch 或 CRD 缺失），client 已通过"
  fi
  # shellcheck disable=SC2086
  if $KUBECTL apply --dry-run=server -f "$MULTINODE_DIR/pytorchjob-template.yaml" >/dev/null 2>&1; then
    pass "kubectl apply --dry-run=server -f deploy/kueue/multi-node/pytorchjob-template.yaml"
  else
    warn "PyTorchJob 模板 server-side dry-run 失败；先确认 Training Operator CRD 已安装"
  fi
  # shellcheck disable=SC2086
  if $KUBECTL apply --dry-run=server -k "$HA_OVERLAY" >/dev/null 2>&1; then
    pass "kubectl apply --dry-run=server -k deploy/web/overlays/ha"
  else
    warn "HA overlay server-side dry-run 失败（PDB/探针字段可能不被接受）"
  fi
else
  info "跳过 apply dry-run（未连接集群或未加 --apply-dry-run）"
fi

printf '\n== 多机验证摘要 ==\n'
printf 'PASS=%d WARN=%d FAIL=%d\n' "$PASS_COUNT" "$WARN_COUNT" "$FAIL_COUNT"
cat <<'EOF'
注意：以上只证明「配置正确、能否静态渲染」。真实多机训练（NCCL 跨节点、
两个节点上各一张卡、TAS 分配到不同节点）必须在 ≥2 个 GPU 节点的集群上验证，
步骤见 deploy/kueue/multi-node/README.md。
EOF
if (( FAIL_COUNT > 0 )); then
  printf '静态校验失败，先修复上面的 [FAIL] 项。\n' >&2
  exit 1
fi
printf '静态校验通过（%d 个警告）。\n' "$WARN_COUNT"