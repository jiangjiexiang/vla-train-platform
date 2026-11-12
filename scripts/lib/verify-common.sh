# shellcheck shell=bash
# 由 verify-4090.sh 和 verify-a100.sh source；调用方负责设置 EXPECTED_GPUS、
# EXPECTED_ALLOWED_GPUS 和 VERIFY_PLATFORM，并启用 set -euo pipefail。

verify_common_init() {
  WEB_URL="${WEB_URL:-http://127.0.0.1:30080}"
  WEB_URL="${WEB_URL%/}"
  WEB_NODEPORT="${WEB_NODEPORT:-30080}"
  WEB_DEPLOYMENT="${WEB_DEPLOYMENT:-}"
  KUEUE_DEPLOYMENT="${KUEUE_DEPLOYMENT:-kueue-controller-manager}"
  TRAINING_OPERATOR_DEPLOYMENT="${TRAINING_OPERATOR_DEPLOYMENT:-training-operator}"
  # 显式设置任一变量时只检查指定位置；否则自动探测静态 manifest 与 Helm 两种布局。
  NVIDIA_DEVICE_PLUGIN_NAMESPACE="${NVIDIA_DEVICE_PLUGIN_NAMESPACE:-}"
  NVIDIA_DEVICE_PLUGIN_DAEMONSET="${NVIDIA_DEVICE_PLUGIN_DAEMONSET:-}"
  NVIDIA_DEVICE_PLUGIN_LOCATIONS=()
  if [[ -n "$NVIDIA_DEVICE_PLUGIN_NAMESPACE" || -n "$NVIDIA_DEVICE_PLUGIN_DAEMONSET" ]]; then
    NVIDIA_DEVICE_PLUGIN_LOCATIONS+=("${NVIDIA_DEVICE_PLUGIN_NAMESPACE:-kube-system}/${NVIDIA_DEVICE_PLUGIN_DAEMONSET:-nvidia-device-plugin-daemonset}")
  else
    NVIDIA_DEVICE_PLUGIN_LOCATIONS+=("kube-system/nvidia-device-plugin-daemonset")
    NVIDIA_DEVICE_PLUGIN_LOCATIONS+=("nvidia-device-plugin/nvidia-device-plugin")
  fi
  E2E_TIMEOUT="${E2E_TIMEOUT:-300}"
  E2E_STEPS="${E2E_STEPS:-1000}"
  E2E_IMAGE="${E2E_IMAGE:-vla-smoke:latest}"
  E2E=0
  CREATED=()
  KEEP_JOB=""
}

print_diagnostics() {
  local location locations
  locations=$(IFS=','; printf '%s' "${NVIDIA_DEVICE_PLUGIN_LOCATIONS[*]}")
  cat <<EOF
诊断命令（只读）：
  kubectl get nodes -o wide
  kubectl describe nodes
EOF
  printf '  # NVIDIA Device Plugin 实际按以下顺序探测：%s\n' "$locations"
  for location in "${NVIDIA_DEVICE_PLUGIN_LOCATIONS[@]}"; do
    printf '  kubectl -n %s get daemonset/%s -o wide\n' "${location%%/*}" "${location#*/}"
    printf '  kubectl -n %s logs daemonset/%s --tail=200\n' "${location%%/*}" "${location#*/}"
  done
  cat <<EOF
  kubectl -n kueue-system get deployment,pod -o wide
  kubectl -n kueue-system logs deployment/${KUEUE_DEPLOYMENT} --tail=200
  kubectl -n kueue-system get configmap kueue-manager-config -o yaml
  kubectl -n kubeflow get deployment,pod -o wide
  kubectl -n vla-training get pytorchjob,pod,workload -o wide
  kubectl -n vla-training get events --sort-by=.lastTimestamp
  kubectl describe clusterqueue vla-cluster-queue
  kubectl -n vla-training describe localqueue default
  kubectl -n vla-platform get deployment,service,pod -o wide
  curl -fsS ${WEB_URL}/api/v1/health
详见 docs/troubleshooting.md。
EOF
}

fail() {
  printf '\n[FAIL] %s\n' "$*" >&2
  print_diagnostics >&2
  exit 1
}

on_error() {
  local rc=$?
  trap - ERR
  printf '\n[FAIL] 脚本在第 %s 行异常退出（%s）。\n' "${BASH_LINENO[0]:-unknown}" "$rc" >&2
  print_diagnostics >&2
  exit "$rc"
}

cleanup() {
  local rc=$?
  local name
  trap - EXIT
  if (( E2E == 1 )) && [[ -n "${PLATFORM_API_TOKEN:-}" ]]; then
    for name in "${CREATED[@]}"; do
      [[ -n "$name" && "$name" != "$KEEP_JOB" ]] || continue
      curl -fsS --max-time 20 -X DELETE \
        -H "Authorization: Bearer ${PLATFORM_API_TOKEN}" \
        "${WEB_URL}/api/v1/jobs/${name}" -o /dev/null \
        || printf '[WARN] 清理任务 %s 失败，请通过 API/网页检查。\n' "$name" >&2
    done
  fi
  exit "$rc"
}

verify_common_validate() {
  local command_name
  [[ "$E2E_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || fail "E2E_TIMEOUT 必须是正整数。"
  [[ "$E2E_STEPS" =~ ^[1-9][0-9]*$ ]] || fail "E2E_STEPS 必须是正整数。"
  for command_name in curl python3; do
    command -v "$command_name" >/dev/null 2>&1 || fail "缺少命令：${command_name}"
  done
}

verify_common_select_kubectl() {
  KUBE_MODE=""
  if [[ -n "${KUBECTL_BIN:-}" ]]; then
    [[ -x "$KUBECTL_BIN" ]] || fail "KUBECTL_BIN 不是可执行文件：${KUBECTL_BIN}"
    KUBE_MODE=external
  elif command -v kubectl >/dev/null 2>&1 \
    && command kubectl get nodes --request-timeout=10s >/dev/null 2>&1; then
    KUBE_MODE=kubectl
  elif command -v k3s >/dev/null 2>&1; then
    if (( EUID == 0 )) && command k3s kubectl get nodes --request-timeout=10s >/dev/null 2>&1; then
      KUBE_MODE=k3s
    elif command -v sudo >/dev/null 2>&1 \
      && sudo k3s kubectl get nodes --request-timeout=10s >/dev/null 2>&1; then
      KUBE_MODE=sudo-k3s
    fi
  fi
  [[ -n "$KUBE_MODE" ]] || fail "无法通过 kubectl 或 k3s kubectl 访问集群；也可设置 KUBECTL_BIN。"
}

kubectl() {
  case "$KUBE_MODE" in
    external) "$KUBECTL_BIN" "$@" ;;
    kubectl) command kubectl "$@" ;;
    k3s) command k3s kubectl "$@" ;;
    sudo-k3s) sudo k3s kubectl "$@" ;;
  esac
}

check_single_node() {
  local nodes_json
  printf '==> 检查单节点 Ready 与 GPU 容量（期望 %s 卡）\n' "$EXPECTED_GPUS"
  nodes_json=$(kubectl get nodes -o json) || fail "无法读取 Kubernetes 节点。"
  python3 -c '
import json, sys
nodes = json.load(sys.stdin).get("items", [])
if len(nodes) != 1:
    raise SystemExit(f"期望单节点，实际 {len(nodes)} 个节点")
for node in nodes:
    conditions = node.get("status", {}).get("conditions", [])
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
    if not ready:
        raise SystemExit(f"节点 {node.get('metadata', {}).get('name')} 未 Ready")
' <<<"$nodes_json" || fail "节点数量或 Ready 状态不符合契约。"
  python3 -c '
import json, sys
expected = int(sys.argv[1])
nodes = json.load(sys.stdin).get("items", [])
def gpu_total(field):
    return sum(int(node.get("status", {}).get(field, {}).get("nvidia.com/gpu", "0")) for node in nodes)
capacity = gpu_total("capacity")
allocatable = gpu_total("allocatable")
if capacity != expected or allocatable != expected:
    raise SystemExit(f"Capacity={capacity}, Allocatable={allocatable}, 期望均为 {expected}")
print(f"GPU Capacity/Allocatable: {capacity}/{allocatable}")
' "$EXPECTED_GPUS" <<<"$nodes_json" || fail "GPU Capacity/Allocatable 不是 ${EXPECTED_GPUS}。"
}

check_deployment() {
  local namespace=$1
  local name=$2
  local payload
  payload=$(kubectl -n "$namespace" get deployment "$name" -o json 2>/dev/null) \
    || fail "Deployment ${namespace}/${name} 不存在或不可读取。"
  python3 -c '
import json, sys
d = json.load(sys.stdin)
desired = d.get("spec", {}).get("replicas", 1)
status = d.get("status", {})
ready = status.get("readyReplicas", 0)
available = status.get("availableReplicas", 0)
if desired < 1 or ready != desired or available != desired:
    raise SystemExit(f"desired={desired}, ready={ready}, available={available}")
' <<<"$payload" || fail "Deployment ${namespace}/${name} 未完全就绪。"
  printf '[OK] Deployment %s/%s 就绪\n' "$namespace" "$name"
}

check_controller_deployments() {
  local web_names
  local -a web_name_array
  check_deployment kueue-system "$KUEUE_DEPLOYMENT"
  check_deployment kubeflow "$TRAINING_OPERATOR_DEPLOYMENT"
  if [[ -z "$WEB_DEPLOYMENT" ]]; then
    web_names=$(kubectl -n vla-platform get deployments -o jsonpath='{range .items[*]}{.metadata.name}{" "}{end}') \
      || fail "无法列出 vla-platform 中的 Web Deployment。"
    read -r -a web_name_array <<<"$web_names"
    [[ "${#web_name_array[@]}" -eq 1 ]] \
      || fail "无法唯一确定 Web Deployment；请设置 WEB_DEPLOYMENT。当前：${web_names:-无}"
    WEB_DEPLOYMENT="${web_name_array[0]}"
  fi
  check_deployment vla-platform "$WEB_DEPLOYMENT"
}

check_device_plugin() {
  local location candidate_namespace candidate_daemonset plugin_json checked_locations
  printf '==> 检查 NVIDIA Device Plugin\n'
  plugin_json=""
  for location in "${NVIDIA_DEVICE_PLUGIN_LOCATIONS[@]}"; do
    candidate_namespace="${location%%/*}"
    candidate_daemonset="${location#*/}"
    printf '  探测 %s/%s ...\n' "$candidate_namespace" "$candidate_daemonset"
    if plugin_json=$(kubectl -n "$candidate_namespace" get daemonset "$candidate_daemonset" -o json 2>/dev/null); then
      NVIDIA_DEVICE_PLUGIN_NAMESPACE=$candidate_namespace
      NVIDIA_DEVICE_PLUGIN_DAEMONSET=$candidate_daemonset
      printf '[OK] 使用 Device Plugin：%s/%s\n' "$NVIDIA_DEVICE_PLUGIN_NAMESPACE" "$NVIDIA_DEVICE_PLUGIN_DAEMONSET"
      break
    fi
  done
  checked_locations=$(IFS=','; printf '%s' "${NVIDIA_DEVICE_PLUGIN_LOCATIONS[*]}")
  [[ -n "$plugin_json" ]] \
    || fail "NVIDIA Device Plugin DaemonSet 不存在；已检查：${checked_locations}。可显式设置 NVIDIA_DEVICE_PLUGIN_NAMESPACE / NVIDIA_DEVICE_PLUGIN_DAEMONSET。"
  python3 -c '
import json, sys
d = json.load(sys.stdin)
status = d.get("status", {})
desired = status.get("desiredNumberScheduled", 0)
ready = status.get("numberReady", 0)
if desired < 1 or ready != desired:
    raise SystemExit(f"desired={desired}, ready={ready}")
' <<<"$plugin_json" || fail "NVIDIA Device Plugin DaemonSet ${NVIDIA_DEVICE_PLUGIN_NAMESPACE}/${NVIDIA_DEVICE_PLUGIN_DAEMONSET} 未就绪。"
  printf '[OK] Device Plugin DaemonSet %s/%s 就绪\n' "$NVIDIA_DEVICE_PLUGIN_NAMESPACE" "$NVIDIA_DEVICE_PLUGIN_DAEMONSET"
}

check_kueue_resources() {
  local localqueue_json clusterqueue_json
  printf '==> 检查 Kueue 资源与配额\n'
  kubectl get resourceflavor gpu >/dev/null 2>&1 || fail "ResourceFlavor gpu 不存在。"
  localqueue_json=$(kubectl -n vla-training get localqueue default -o json 2>/dev/null) \
    || fail "LocalQueue vla-training/default 不存在。"
  python3 -c '
import json, sys
actual = json.load(sys.stdin).get("spec", {}).get("clusterQueue")
if actual != "vla-cluster-queue":
    raise SystemExit(f"clusterQueue={actual!r}")
' <<<"$localqueue_json" || fail "LocalQueue default 未指向 vla-cluster-queue。"
  clusterqueue_json=$(kubectl get clusterqueue vla-cluster-queue -o json 2>/dev/null) \
    || fail "ClusterQueue vla-cluster-queue 不存在。"
  python3 -c '
import json, sys
expected = int(sys.argv[1])
cq = json.load(sys.stdin)
quotas = []
for group in cq.get("spec", {}).get("resourceGroups", []):
    for flavor in group.get("flavors", []):
        for resource in flavor.get("resources", []):
            if resource.get("name") == "nvidia.com/gpu":
                quotas.append(int(str(resource.get("nominalQuota"))))
if not quotas or sum(quotas) != expected:
    raise SystemExit(f"GPU nominalQuota entries={quotas}, 期望总量 {expected}")
print(f"Kueue GPU nominalQuota: {sum(quotas)}")
' "$EXPECTED_GPUS" <<<"$clusterqueue_json" || fail "Kueue GPU 配额不是 ${EXPECTED_GPUS}。"
}

check_kueue_integration() {
  local kueue_config_json
  printf '==> 检查 Kueue PyTorchJob 集成\n'
  if [[ "${SKIP_KUEUE_INTEGRATION_CHECK:-0}" == "1" ]]; then
    printf '[SKIP] SKIP_KUEUE_INTEGRATION_CHECK=1，跳过 Kueue PyTorchJob 集成检查。\n'
  else
    kueue_config_json=$(kubectl -n kueue-system get configmap kueue-manager-config -o json 2>/dev/null) \
      || fail "ConfigMap kueue-system/kueue-manager-config 不存在或不可读取，无法确认 PyTorchJob 集成。"
    python3 -c '
import json, sys
cm = json.load(sys.stdin)
texts = [str(value) for value in cm.get("data", {}).values()]
framework = "kubeflow.org/pytorchjob"
enabled = False
try:
    import yaml
except ImportError:
    yaml = None
if yaml is not None:
    for text in texts:
        try:
            config = yaml.safe_load(text)
        except Exception:
            continue
        if not isinstance(config, dict):
            continue
        integrations = config.get("integrations") or {}
        if not isinstance(integrations, dict):
            continue
        frameworks = integrations.get("frameworks") or []
        if isinstance(frameworks, list) and framework in frameworks:
            enabled = True
            break
else:
    for text in texts:
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = line.split(" #", 1)[0].strip()
            if line.startswith("-") and line[1:].strip().strip(chr(34) + chr(39)) == framework:
                enabled = True
                break
        if enabled:
            break
if not enabled:
    raise SystemExit("integrations.frameworks 未包含 " + framework + "，排队粒度会退化为 Pod 级")
print("integrations.frameworks 已包含 " + framework)
' <<<"$kueue_config_json" || fail "Kueue 未启用 kubeflow.org/pytorchjob 集成：PyTorchJob 无法整体排队，排队粒度会退化为 Pod 级（老环境可设置 SKIP_KUEUE_INTEGRATION_CHECK=1 跳过）。"
    printf '[OK] Kueue 已启用 PyTorchJob 集成\n'
  fi
}

check_web_health() {
  local services_json health_json
  printf '==> 检查 Web Service 与 API 健康\n'
  services_json=$(kubectl -n vla-platform get services -o json) || fail "无法读取 Web Service。"
  python3 -c '
import json, sys
port = int(sys.argv[1])
services = json.load(sys.stdin).get("items", [])
matched = [s.get("metadata", {}).get("name") for s in services
           if any(p.get("nodePort") == port for p in s.get("spec", {}).get("ports", []))]
if not matched:
    raise SystemExit(f"没有 Service 暴露 nodePort {port}")
print(f"Web Service: {matched[0]}，NodePort: {port}")
' "$WEB_NODEPORT" <<<"$services_json" || fail "Web Service 未暴露 NodePort ${WEB_NODEPORT}。"
  health_json=$(curl -fsS --max-time 15 "${WEB_URL}/api/v1/health") \
    || fail "Web 健康检查失败：${WEB_URL}/api/v1/health"
  python3 -c '
import json, sys
if json.load(sys.stdin).get("status") != "ok":
    raise SystemExit("健康响应不是 status=ok")
' <<<"$health_json" || fail "Web 健康响应格式不正确。"
  printf '[OK] Web API 健康：%s\n' "$WEB_URL"
}

api_get() {
  curl -fsS --max-time 20 -H "Authorization: Bearer ${PLATFORM_API_TOKEN}" "${WEB_URL}$1"
}

check_authenticated_api() {
  local config_json summary_json
  if [[ -n "${PLATFORM_API_TOKEN:-}" ]]; then
    printf '==> 检查认证 API 配置与集群汇总\n'
    config_json=$(api_get /api/v1/config) || fail "认证 API /config 失败。"
    python3 -c '
import json, sys
expected = json.loads(sys.argv[1])
actual = json.load(sys.stdin).get("allowed_gpus")
if actual != expected:
    raise SystemExit(f"allowed_gpus={actual}, 期望 {expected}")
' "$EXPECTED_ALLOWED_GPUS" <<<"$config_json" || fail "PLATFORM_ALLOWED_GPUS 与 ${VERIFY_PLATFORM} 配置不符。"
    summary_json=$(api_get /api/v1/cluster/summary) || fail "认证 API /cluster/summary 失败。"
    python3 -c '
import json, sys
expected = int(sys.argv[1])
actual = json.load(sys.stdin).get("gpu_total")
if actual != expected:
    raise SystemExit(f"gpu_total={actual}, 期望 {expected}")
' "$EXPECTED_GPUS" <<<"$summary_json" || fail "Web 汇总 GPU 总数不是 ${EXPECTED_GPUS}。"
  else
    printf '[SKIP] 未提供 PLATFORM_API_TOKEN，跳过认证 API；默认模式不提交任务。\n'
  fi
}

verify_common_check_cluster() {
  check_single_node
  check_controller_deployments
  check_device_plugin
  check_kueue_resources
  check_kueue_integration
  check_web_health
  check_authenticated_api
}

job_status() {
  local response
  response=$(api_get "/api/v1/jobs/$1") || return 1
  python3 -c '
import json, sys
print(json.load(sys.stdin).get("status", "UNKNOWN"))
' <<<"$response"
}

post_job() {
  local name=$1
  local gpus=$2
  local command=$3
  local payload response
  payload=$(python3 -c '
import json, sys
print(json.dumps({
    "name": sys.argv[1],
    "image": sys.argv[2],
    "gpus": int(sys.argv[3]),
    "queue": "default",
    "priority": "normal",
    "command": sys.argv[4],
    "env": {"PYTHONUNBUFFERED": "1"},
    "max_retries": 0,
}, separators=(",", ":")))
' "$name" "$E2E_IMAGE" "$gpus" "$command")
  response=$(curl -fsS --max-time 30 -X POST \
    -H "Authorization: Bearer ${PLATFORM_API_TOKEN}" \
    -H 'Content-Type: application/json' \
    --data "$payload" "${WEB_URL}/api/v1/jobs") || fail "提交任务 ${name} 失败。"
  python3 -c '
import json, sys
expected = sys.argv[1]
actual = json.load(sys.stdin).get("name")
if actual != expected:
    raise SystemExit(f"响应任务名 {actual!r}，期望 {expected!r}")
' "$name" <<<"$response" || fail "提交任务 ${name} 的响应不符合契约。"
  CREATED+=("$name")
}

cancel_job() {
  curl -fsS --max-time 30 -X DELETE \
    -H "Authorization: Bearer ${PLATFORM_API_TOKEN}" \
    "${WEB_URL}/api/v1/jobs/$1" -o /dev/null || fail "取消任务 $1 失败。"
  printf '[OK] 已取消 %s\n' "$1"
}

wait_for_running_and_queued() {
  local first=$1
  local second=$2
  local first_status second_status
  local deadline
  RUNNING_JOB=""
  QUEUED_JOB=""
  deadline=$((SECONDS + E2E_TIMEOUT))
  while (( SECONDS < deadline )); do
    first_status=$(job_status "$first") || fail "无法读取 ${first} 状态。"
    second_status=$(job_status "$second") || fail "无法读取 ${second} 状态。"
    printf '  状态：%s=%s，%s=%s\n' "$first" "$first_status" "$second" "$second_status"
    if [[ "$first_status" == "RUNNING" && "$second_status" == "QUEUED" ]]; then
      RUNNING_JOB=$first
      QUEUED_JOB=$second
      break
    fi
    if [[ "$second_status" == "RUNNING" && "$first_status" == "QUEUED" ]]; then
      RUNNING_JOB=$second
      QUEUED_JOB=$first
      break
    fi
    if [[ "$first_status" =~ ^(FAILED|UNKNOWN|SUCCEEDED|CANCELLED)$ || "$second_status" =~ ^(FAILED|UNKNOWN|SUCCEEDED|CANCELLED)$ ]]; then
      fail "未观察到一运行一排队；任务提前结束或失败。请确认集群空闲，必要时增大 E2E_STEPS。"
    fi
    sleep 5
  done
  [[ -n "$RUNNING_JOB" && -n "$QUEUED_JOB" ]] || fail "等待一运行一排队超时。"
}
