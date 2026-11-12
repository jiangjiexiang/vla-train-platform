#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib/verify-common.sh
source "$SCRIPT_DIR/lib/verify-common.sh"

EXPECTED_GPUS=1
EXPECTED_ALLOWED_GPUS='[1]'
VERIFY_PLATFORM=4090
verify_common_init

usage() {
  cat <<'EOF'
用法：bash scripts/verify-4090.sh [--e2e] [--help]

默认只检查节点、GPU、相关控制器、Kueue 配额和 Web API，不提交训练。
  --e2e   显式提交两个 1 卡短 smoke，验证排队、取消、放行和完成。

环境变量：
  PLATFORM_API_TOKEN  API 令牌；--e2e 必需，不提供时跳过认证 API 检查。
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
    --help|-h) usage; exit 0 ;;
    *) usage >&2; fail "未知参数：$1" ;;
  esac
  shift
done

verify_common_validate
verify_common_select_kubectl
verify_common_check_cluster

check_gpu_logs() {
  local response
  response=$(api_get "/api/v1/jobs/$1/logs?tail_lines=10000") \
    || fail "无法读取任务 $1 的 smoke 日志。"
  python3 -c '
import json, sys
logs = json.load(sys.stdin).get("logs", "")
required = ["CUDA available: True", "SMOKE_COMPLETE"]
missing = [marker for marker in required if marker not in logs]
if missing:
    raise SystemExit(f"日志缺少 GPU smoke 标记：{missing}；不能用 CPU fallback 通过验收")
' <<<"$response" || fail "任务 $1 未提供真实 CUDA smoke 证据。"
  printf '[OK] %s 日志确认 CUDA 可用且 smoke 完成\n' "$1"
}

submit_job() {
  post_job "$1" 1 "$2"
  printf '[OK] 已提交 %s\n' "$1"
}

run_queue_e2e() {
  local prefix="verify4090-$(date +%s)-$$"
  local first="${prefix}-a"
  local second="${prefix}-b"
  local command="python /app/train.py --steps ${E2E_STEPS}"
  local running queued status
  local deadline

  submit_job "$first" "$command"
  submit_job "$second" "$command"
  wait_for_running_and_queued "$first" "$second"
  running=$RUNNING_JOB
  queued=$QUEUED_JOB

  cancel_job "$running"
  deadline=$((SECONDS + E2E_TIMEOUT))
  while (( SECONDS < deadline )); do
    status=$(job_status "$queued") || fail "无法读取放行任务 ${queued} 状态。"
    printf '  取消后状态：%s=%s\n' "$queued" "$status"
    case "$status" in
      SUCCEEDED)
        check_gpu_logs "$queued"
        KEEP_JOB=$queued
        printf '[OK] 排队、取消释放、自动放行和真实 CUDA smoke 完成均通过。保留成功任务 %s 供检查。\n' "$queued"
        return 0
        ;;
      QUEUED|RUNNING) ;;
      *) fail "放行任务 ${queued} 状态为 ${status}，未完成 smoke。" ;;
    esac
    sleep 5
  done
  fail "等待放行任务 ${queued} 完成超时。"
}

if (( E2E == 1 )); then
  [[ -n "${PLATFORM_API_TOKEN:-}" ]] || fail "--e2e 需要通过环境变量提供 PLATFORM_API_TOKEN。"
  printf '==> 开始可选真实 GPU 端到端验证（两个 1 卡 smoke）\n'
  run_queue_e2e
fi

printf '\n[PASS] 4090 %s检查通过。\n' "$([[ $E2E == 1 ]] && printf '集群及可选端到端' || printf '静态/集群')"
if (( E2E == 0 )); then
  printf '未启动训练；本结果不代表 CUDA、checkpoint 或真实 GPU 端到端已验收。\n'
fi
