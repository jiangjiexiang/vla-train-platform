#!/usr/bin/env bash
set -euo pipefail
# 用假 kubectl 验证 scripts/preflight.sh 的多机分支：单机、2 节点一致、
# 部分节点缺失版本、跨节点版本不一致（必须 FAIL）。
# 不接触真实集群，也不修改任何节点。
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"

write_nodes() {
  cat > "$WORK/$1"
}

# 1) 两台 1 卡节点、CUDA 运行时版本一致
write_nodes two-ok.json <<'JSON'
{"items":[
 {"metadata":{"name":"node-a","labels":{"vla-platform.io/node-pool":"multinode","nvidia.com/cuda.runtime-version.full":"12.4"}},
  "status":{"allocatable":{"nvidia.com/gpu":"1","cpu":"24"}}},
 {"metadata":{"name":"node-b","labels":{"vla-platform.io/node-pool":"multinode","nvidia.com/cuda.runtime-version.full":"12.4"}},
  "status":{"allocatable":{"nvidia.com/gpu":"1","cpu":"24"}}}
]}
JSON
# 2) 其中一个节点没有上报版本
write_nodes two-partial.json <<'JSON'
{"items":[
 {"metadata":{"name":"node-a","labels":{"vla-platform.io/node-pool":"multinode"}},
  "status":{"allocatable":{"nvidia.com/gpu":"1"}}},
 {"metadata":{"name":"node-b","labels":{"vla-platform.io/node-pool":"multinode","nvidia.com/cuda.runtime-version.full":"12.4"}},
  "status":{"allocatable":{"nvidia.com/gpu":"1"}}}
]}
JSON
# 3) 版本不一致：必须 FAIL
write_nodes two-mismatch.json <<'JSON'
{"items":[
 {"metadata":{"name":"node-a","labels":{"vla-platform.io/node-pool":"multinode","nvidia.com/cuda.runtime-version.full":"12.1"}},
  "status":{"allocatable":{"nvidia.com/gpu":"1"}}},
 {"metadata":{"name":"node-b","labels":{"vla-platform.io/node-pool":"multinode","nvidia.com/cuda.runtime-version.full":"12.4"}},
  "status":{"allocatable":{"nvidia.com/gpu":"1"}}}
]}
JSON

cat > "$WORK/bin/kubectl" <<'FAKE'
#!/usr/bin/env bash
case " $* " in
  *" get nodes --request-timeout=5s "*) echo ok ;;
  *" get nodes -o json --request-timeout=10s "*) cat "$FAKE_NODES" ;;
  *" get crd topologies.kueue.x-k8s.io "*) echo topology ;;
  *" get topologies.kueue.x-k8s.io "*) printf 'vla-multinode\n' ;;
  *" get resourceflavors.kueue.x-k8s.io "*) echo '{"items":[{"spec":{"topologyName":"vla-multinode"}}]}' ;;
  *" get deployment kueue-controller-manager "*) echo '--config=/controller_manager_config.yaml --feature-gates=TopologyAwareScheduling=true' ;;
  *) exit 0 ;;
esac
FAKE
chmod +x "$WORK/bin/kubectl"

status=0
run_case() {
  local name=$1 nodes=$2 expect=$3
  local out
  out=$(PATH="$WORK/bin:$PATH" FAKE_NODES="$WORK/$nodes" MIN_DISK_GB=1 MULTINODE_CHECK=1 \
        bash "$ROOT_DIR/scripts/preflight.sh" 2>&1 || true)
  local section
  section=$(sed -n '/Multi-node readiness/,/Preflight summary/p' <<< "$out")
  local summary
  summary=$(sed -n '/PASS=.*WARN=.*FAIL=/p' <<< "$out" | tail -n 1)
  local actual="pass"
  grep -q '^\[FAIL\]' <<< "$section" && actual="fail"
  if [[ "$actual" != "$expect" ]]; then
    printf '[FAIL] %s: expected %s, got %s\n' "$name" "$expect" "$actual"
    printf '%s\n' "$section"
    status=1
  else
    printf '[OK  ] %-40s %s (%s)\n' "$name" "$actual" "$summary"
  fi
}

printf '== preflight 多机分支（假 kubectl，不接触真实集群）==\n'
run_case "两个 GPU 节点、版本一致" two-ok.json pass
run_case "只有部分节点上报版本" two-partial.json pass
run_case "跨节点版本不一致（应 FAIL）" two-mismatch.json fail
printf '\n'
if (( status == 0 )); then
  printf '全部多机分支行为符合预期。\n'
fi
exit "$status"
