#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib/verify-common.sh
source "$SCRIPT_DIR/lib/verify-common.sh"

EXPECTED_GPUS=8
EXPECTED_ALLOWED_GPUS='[1,2,4,8]'
VERIFY_PLATFORM=A100
verify_common_init
EIGHT_GPU=0

usage() {
  cat <<'EOF'
用法：bash scripts/verify-a100.sh [--e2e] [--8gpu-smoke] [--help]

默认只检查节点、GPU、相关控制器、Kueue 配额和 Web API，不提交训练。
  --e2e         显式提交一个 1 卡短 smoke，并等待完成。
  --8gpu-smoke  提交两个 8 卡 DDP smoke，验证排队、取消、放行和 8 卡完成；
                隐含 --e2e，会真实占用 8 张 GPU。

环境变量：
  PLATFORM_API_TOKEN  API 令牌；任何端到端模式必需，不提供时跳过认证 API 检查。
  WEB_URL             API 服务地址，默认 http://127.0.0.1:30080。
  KUBECTL_BIN         可选的 kubectl 可执行文件路径；默认自动选择 kubectl/k3s。
  NVIDIA_DEVICE_PLUGIN_NAMESPACE  Device Plugin 命名空间；与 NVIDIA_DEVICE_PLUGIN_DAEMONSET 任一显式设置时
                                  只检查指定位置，默认自动探测 kube-system 与 nvidia-device-plugin 两种布局。
  NVIDIA_DEVICE_PLUGIN_DAEMONSET  Device Plugin DaemonSet 名称。
  SKIP_KUEUE_INTEGRATION_CHECK    设为 1 跳过 Kueue PyTorchJob 集成检查（老环境兼容）。
  E2E_TIMEOUT         每轮任务等待秒数，默认 300。
  E2E_STEPS           smoke 步数，默认 1000。
  E2E_IMAGE           smoke 镜像，默认 vla-smoke:latest。
EOF
}

trap on_error ERR
trap cleanup EXIT

while (( $# > 0 )); do
  case "$1" in
    --e2e) E2E=1 ;;
    --8gpu-smoke) E2E=1; EIGHT_GPU=1 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; fail "未知参数：$1" ;;
  esac
  shift
done

verify_common_validate
verify_common_select_kubectl
verify_common_check_cluster

check_gpu_logs() {
  local name=$1
  local world_size=$2
  local response
  response=$(api_get "/api/v1/jobs/${name}/logs?tail_lines=10000") \
    || fail "无法读取任务 ${name} 的 smoke 日志。"
  python3 -c '
import json, sys
world_size = int(sys.argv[1])
logs = json.load(sys.stdin).get("logs", "")
required = ["CUDA available: True", "SMOKE_COMPLETE"]
if world_size == 8:
    required += ["DDP_VERIFIED world_size=8", "backend=nccl"]
missing = [marker for marker in required if marker not in logs]
if missing:
    raise SystemExit(f"日志缺少 GPU/DDP smoke 标记：{missing}；不能用 CPU fallback 通过验收")
' "$world_size" <<<"$response" || fail "任务 ${name} 未提供真实 CUDA/${world_size} 卡 smoke 证据。"
  printf '[OK] %s 日志确认 CUDA 可用、smoke 完成（world_size=%s）\n' "$name" "$world_size"
}

submit_job() {
  post_job "$1" "$2" "$3"
  printf '[OK] 已提交 %s（%s 卡）\n' "$1" "$2"
}

wait_for_success() {
  local name=$1
  local world_size=$2
  local status
  local deadline=$((SECONDS + E2E_TIMEOUT))
  while (( SECONDS < deadline )); do
    status=$(job_status "$name") || fail "无法读取任务 ${name} 状态。"
    printf '  状态：%s=%s\n' "$name" "$status"
    case "$status" in
      SUCCEEDED)
        check_gpu_logs "$name" "$world_size"
        KEEP_JOB=$name
        printf '[OK] 真实 CUDA smoke 已完成。保留成功任务 %s 供检查日志与 checkpoint。\n' "$name"
        return 0
        ;;
      QUEUED|RUNNING) ;;
      *) fail "任务 ${name} 状态为 ${status}，未完成 smoke。" ;;
    esac
    sleep 5
  done
  fail "等待任务 ${name} 完成超时。"
}

run_queue_e2e() {
  local prefix="verifya100-$(date +%s)-$$"
  local first="${prefix}-a"
  local second="${prefix}-b"
  local command="torchrun --standalone --nproc_per_node=8 /app/train.py --distributed --steps ${E2E_STEPS}"
  local running queued

  submit_job "$first" 8 "$command"
  submit_job "$second" 8 "$command"
  wait_for_running_and_queued "$first" "$second"
  running=$RUNNING_JOB
  queued=$QUEUED_JOB

  cancel_job "$running"
  printf '==> 等待排队任务自动放行并完成 8 卡 DDP smoke\n'
  wait_for_success "$queued" 8
  printf '[OK] 8 卡排队、取消释放、自动放行和 NCCL DDP smoke 完成均通过。\n'
}

if (( E2E == 1 )); then
  [[ -n "${PLATFORM_API_TOKEN:-}" ]] || fail "端到端模式需要通过环境变量提供 PLATFORM_API_TOKEN。"
  if (( EIGHT_GPU == 1 )); then
    printf '==> 开始可选真实 GPU 端到端验证（两个 8 卡 DDP smoke）\n'
    run_queue_e2e
  else
    printf '==> 开始可选真实 GPU 端到端验证（一个 1 卡 smoke）\n'
    single_name="verifya100-$(date +%s)-$$-single"
    submit_job "$single_name" 1 "python /app/train.py --steps ${E2E_STEPS}"
    wait_for_success "$single_name" 1
  fi
fi

printf '\n[PASS] A100 %s检查通过。\n' "$([[ $E2E == 1 ]] && printf '集群及可选端到端' || printf '静态/集群')"
if (( E2E == 0 )); then
  printf '未启动训练；本结果不代表 CUDA、DDP/NCCL、checkpoint 或真实 GPU 端到端已验收。\n'
elif (( EIGHT_GPU == 0 )); then
  printf '本次只完成单卡 smoke，不代表 8 卡 DDP/NCCL 或 8 卡排队已验收。\n'
fi
