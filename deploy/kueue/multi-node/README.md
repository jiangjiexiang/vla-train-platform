# 多机（Multi-Node）Kueue Profile

多机训练 = **一个 PyTorchJob 跨 N 个节点、每节点 1 卡**，由 Kueue 整体排队、整体准入。
本目录只新增多机专用的 Kueue 对象与一份提交模板，**与现有 4090 / a100 单机 profile 完全解耦**：

| 对象 | 单机 profile（4090/a100） | 本目录（multi-node） | 是否冲突 |
|---|---|---|---|
| ResourceFlavor | `gpu`（无 topologyName） | `gpu-multinode`（带 `topologyName` + `nodeLabels`） | 不冲突，名字不同 |
| ClusterQueue | `vla-cluster-queue` | `vla-multinode` | 不冲突 |
| LocalQueue | `vla-training/default` | `vla-training/multinode` | 不冲突 |
| Topology | 无 | `vla-multinode` | 新增 |

`install-stack.sh`、`deploy-web.sh` 的 4090/a100 路径不会引用本目录，
现有单机部署行为保持不变。

> ### ⚠️ 这些配置没有在真实多机环境验证过
> 本仓库的开发机只有 **1 台机器、1 张 RTX 4090**，所有多机内容只做了：
> `kubectl kustomize` 渲染 + `kubectl apply --dry-run=client/server`（真实集群 schema 与
> webhook 校验）+ 假 torch 驱动 `train.py` 的 rendezvous 逻辑测试 + 假 kubectl 驱动
> `preflight.sh` 的分支测试。
> **没有做过**：真实 2 节点 NCCL 通信、TAS 实际分配、跨节点 GPU 训练。
> 按下面的「如何验证」在真正的多 GPU 集群上跑一遍再上线。

---

## 1. Kueue v0.20.5 的 API 事实（实测自运行中的 CRD）

写这份配置时以**集群里的 CRD schema** 为准，而不是旧文档：

- Workload CRD 的 **v1beta2 和 v1beta1 都没有 `topology` 字段**，PodSet 的拓扑约束叫
  `topologyRequest`（`required` / `preferred` / `podIndexLabel` / `podSetSliceSize` …）。
  v1beta1 时代的 `spec.podSets[].topology` 写法在 v0.20.5 会直接校验失败。
- `Topology`（`kueue.x-k8s.io/v1beta2`）存在，`spec.levels[].nodeLabel` 的最低层必须是
  `kubernetes.io/hostname`（CRD CEL 规则强制）。
- `ResourceFlavor.spec` 支持 `topologyName` 与 `nodeLabels`（最多 8 条）。
- TopologyAwareScheduling（TAS）**默认关闭**：运行中的 `kueue-controller-manager`
  启动参数只有 `--config=... --zap-log-level=2`，没有 `--feature-gates`。

因此「N 个 Pod 落到 N 个不同节点」只有两条路：

| 手段 | 能否真正保证 | 代价 | 本仓库用法 |
|---|---|---|---|
| **TAS**：`ResourceFlavor.topologyName` + PodSet `topologyRequest.required=kubernetes.io/hostname` | 能 | 需开 feature gate + **重启 kueue-controller-manager** | **主手段** |
| Pod `requiredDuringScheduling` 反亲和 | 不完全能。**Kueue 计算 flavor 用量时不扣减 required 反亲和的资源**，可能「先 admit 后 Unschedulable」 | 无需改控制器 | **只作防御**，写在提交模板里 |

反亲和不能替代 TAS：如果 8 节点各 2 卡、队列 quota=8，而某台机器上已经跑着别的任务，
Kueue 仍会把 8 卡算作可用并准入，随后 Pod 因反亲和永远 Pending。

---

## 2. 文件说明

| 文件 | 作用 |
|---|---|
| `kustomization.yaml` | `kubectl apply -k deploy/kueue/multi-node` 的入口，只含 `queues.yaml` |
| `queues.yaml` | `Topology` + `ResourceFlavor gpu-multinode` + `ClusterQueue vla-multinode` + `LocalQueue multinode` |
| `kueue-values-multi-node.yaml` | Helm values：启用 `TopologyAwareScheduling` feature gate（**需重启 controller**） |
| `pytorchjob-template.yaml` | 2 节点 × 1 卡参考模板（人工提交/调试用，不在 kustomization 里） |
| `pytorchjob-single-node.yaml` | 单机「多机语义」调试变体，只用于 schema 校验与观察 Workload |

配额语义：`nominalQuota` 单位是**卡数**，不是节点数。每节点 1 卡时 `nominalQuota = 节点数`。
每节点 8 卡时改成 `节点数 × 8`，并把提交模板里的 `nvidia.com/gpu` 改成 8。

---

## 3. 怎么应用

### 3.1 前置条件

1. 集群已有 Kueue v0.20.5 + Training Operator（`scripts/install-stack.sh` 已装）；
2. **≥2 个带 GPU 的节点**，每节点已装 NVIDIA 驱动、container toolkit，
   `kubectl get node <n> -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'` ≥ 1；
3. 各节点 **驱动版本一致**（`scripts/preflight.sh` 会检查）；
4. smoke 镜像在**每个节点**都存在（无镜像仓库时）：
   `docker save vla-smoke:latest | sudo k3s ctr -n k8s.io images import -`

### 3.2 给节点打多机标签

```bash
# 只给真正参与多机训练的 GPU 节点打标签，避免污染单卡 4090 机器
sudo kubectl label node <node-1> <node-2> vla-platform.io/node-pool=multinode --overwrite
```

`ResourceFlavor gpu-multinode` 用 `nodeLabels` 限定只在这批节点上调度。没打标签的节点
（例如现在这台 4090）不会被多机队列选中。

### 3.3 应用多机 Kueue 资源（不重启任何组件）

```bash
kubectl kustomize deploy/kueue/multi-node            # 先看渲染结果
kubectl apply -k deploy/kueue/multi-node
kubectl wait clusterqueue/vla-multinode --for=condition=Active --timeout=120s
kubectl -n vla-training get localqueue multinode
```

或者用脚本（默认 dry-run，加 `--apply` 才写集群）：

```bash
LABEL_NODES="node-1 node-2" scripts/deploy-multi-node-ha.sh            # dry-run
sudo LABEL_NODES="node-1 node-2" scripts/deploy-multi-node-ha.sh --apply
```

### 3.4 启用 TAS feature gate（**会重启 kueue-controller-manager**）

只在确认要多机时做；重启期间所有 PyTorchJob 的准入短暂不可用。

```bash
helm upgrade --install kueue /path/to/kueue-0.20.5.tgz -n kueue-system \
  -f deploy/kueue/multi-node/kueue-values-multi-node.yaml
kubectl -n kueue-system rollout restart deployment/kueue-controller-manager
kubectl -n kueue-system rollout status deployment/kueue-controller-manager

# 确认 gate 生效：输出里必须出现 --feature-gates=TopologyAwareScheduling=true
kubectl -n kueue-system get deployment kueue-controller-manager \
  -o jsonpath='{range .spec.template.spec.containers[0].args[*]}{@}{"\n"}{end}' | grep -i feature
```

- 没有 helm 的环境，直接给 Deployment 加参数也可以，等价做法：
  `kubectl -n kueue-system patch deployment kueue-controller-manager --type=json \
   -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--feature-gates=TopologyAwareScheduling=true"}]'`
  然后 `rollout restart`。
- **gate 名称**：按 Kueue 官方文档写作 `TopologyAwareScheduling`。若你的版本里名字不同，
  以 `kubectl -n kueue-system exec deploy/kueue-controller-manager -- /manager --help`
  或该版本 release notes 为准；controller 启动失败时会在日志里列出已知 gate。

### 3.5 回滚

```bash
# 回滚多机队列（不影响单机 vla-cluster-queue 和正在运行的任务）
kubectl delete -k deploy/kueue/multi-node

# 回滚 feature gate：去掉 --feature-gates 参数后重启
kubectl -n kueue-system rollout undo deployment/kueue-controller-manager
# 或重新用单机 values 覆盖：
# helm upgrade --install kueue /path/to/kueue-0.20.5.tgz -n kueue-system \
#   -f deploy/kueue/base/kueue-values.yaml

# 去掉节点标签（多机对象已删除时可做）
kubectl label node <node-1> <node-2> vla-platform.io/node-pool-
```

回滚顺序建议：先删多机 LocalQueue/ClusterQueue（新任务不再进多机队列），
再决定是否关 gate；已准入的多机任务不受影响。

---

## 4. 提交与查看

```bash
# 只检查前置条件并打印将提交的 PyTorchJob，不创建资源
MODE=inspect examples/submit-multi-node.sh

# 提交 2 节点 × 1 卡
examples/submit-multi-node.sh

# 3 节点
NODES=3 examples/submit-multi-node.sh

kubectl -n vla-training get pytorchjob,pod -o wide
kubectl -n vla-training get workloads.kueue.x-k8s.io -o wide
```

`MASTER_ADDR` 显式写成 master 的 Pod DNS：

```
<pytorchjob>-master-0.<pytorchjob>-master.<namespace>.svc
```

Kueue + TAS 调度后 Training Operator 不一定能稳定推导跨节点地址，显式写死最可靠。
若集群 DNS（CoreDNS）无法解析，退回用 master Pod IP，并接受 Pod 重启后需要重建 job。

---

## 5. 如何验证（单机做不到，必须在 ≥2 GPU 节点的集群上做）

按顺序执行，每一步失败都先修再往下：

```bash
# 1. 静态：配置/脚本/渲染/schema 全链路（任何机器都能跑）
bash scripts/verify-multi-node.sh --apply-dry-run

# 2. 只读巡检：节点 GPU 数、驱动一致性、Topology、TAS gate
MULTINODE_CHECK=1 PROFILE=4090 bash scripts/preflight.sh
#    期望：≥2 个 GPU 节点 PASS；驱动版本一致 PASS；Topology PASS；TAS gate PASS

# 3. 确认 Kueue 真的生成了 TAS 请求（TAS 是否生效的关键观察点）
kubectl -n vla-training get workloads.kueue.x-k8s.io -o yaml | \
  grep -E -A12 'topologyRequest|topologyAssignment'
#    必须同时看到：
#      spec.podSets[].topologyRequest.required: kubernetes.io/hostname
#      status.admission.podSetAssignments[].topologyAssignment  （有值 = TAS 已在分配节点）
#    只有 topologyRequest 没有 topologyAssignment ⇒ gate 没生效或节点没有 label

# 4. 提交 2 节点 smoke
MODE=inspect NODES=2 examples/submit-multi-node.sh   # 先看一眼渲染结果
NODES=2 examples/submit-multi-node.sh

# 5. 确认两个 Pod 真的落在不同节点
kubectl -n vla-training get pods -l app.kubernetes.io/component=multi-node-smoke \
  -o wide --no-headers | awk '{print $1, $7}'
#    master-0 与 worker-0 的 NODE 列必须不同

# 6. 确认跨节点 NCCL 正常
kubectl -n vla-training logs -l app.kubernetes.io/component=multi-node-smoke --prefix | \
  grep -E 'LAUNCH_MODE|RANK_INFO|NCCL_ROUNDTRIP_OK|DDP_VERIFIED'
```

rank 0（master）日志里必须出现且仅出现一次的三个关键行：

| 行 | 期望内容 | 含义 |
|---|---|---|
| `LAUNCH_MODE` | `LAUNCH_MODE multi-node world_size=2` | train.py 走的是多节点分支，而不是 torchrun 单机 |
| `NCCL_ROUNDTRIP_OK` | `"world_size": 2, "nccl_version": "..."` | 一次跨节点 all_reduce 往返成功，含耗时 |
| `DDP_VERIFIED` | `world_size=2 ... nodes=2 mode=multi-node backend=nccl` | 两个 rank 都参与了，且来自 **2 个不同主机** |

`nodes=1` 说明两个副本落到了同一节点 —— 这正是 TAS 没生效的典型症状。
每个 rank 还会打印 `RANK_INFO`（rank / local_rank / node / node_ip / master_addr /
gpu / driver / cuda_build），多机排查先看这一行。

### 单机环境下能做到哪一步

单机（当前开发机）**无法**验证真实多机。可以做的：

```bash
bash scripts/verify-multi-node.sh --apply-dry-run     # 静态全绿
MULTINODE_CHECK=1 bash scripts/preflight.sh           # 多机项给出 WARN，不 FAIL
kubectl apply --dry-run=server -f deploy/kueue/multi-node/pytorchjob-single-node.yaml
python3 scripts/check-smoke-train-rendezvous.py       # 假 torch 验证 rendezvous 逻辑
```

`pytorchjob-single-node.yaml` 只用于观察 schema 与 Workload 结构，**不构成多机验证**。

---

## 6. 排查清单

| 现象 | 先看 | 常见原因 |
|---|---|---|
| Pod 一直 `Pending`，Workload `pending` | `kubectl -n vla-training describe workload <name>` | 队列 quota 不足；`ResourceFlavor` nodeLabels 匹配不到节点 |
| Workload 被 admit 但 Pod `Unschedulable` | `kubectl -n vla-training describe pod <pod>` | 反亲和与 flavor 用量不一致（见第 1 节）；GPU 已被别的 Pod 占满 |
| 两个 Pod 在同一节点 | Workload 有无 `topologyAssignment` | TAS gate 未生效 / 未设 `topologyName` |
| `NCCL` 超时或卡住 | `RANK_INFO`、`NCCL_DEBUG=INFO` 日志 | `NCCL_SOCKET_IFNAME` 选错网卡（默认 `eth0`）；节点间防火墙/MTU；驱动或 NCCL 版本不一致 |
| `MASTER_ADDR` 解析失败 | master Pod 日志 `socket.gaierror` | master 的 headless Service 尚未就绪；改用手写 IP |
| worker 报 `checkpoint was not created` | 见 `images/README.md` | checkpoint 是各节点本地 hostPath，worker 本来就不该看到 rank 0 的文件（当前实现已跳过该检查） |

更多 NCCL/性能说明见 `docs/multi-node.md`（docs 分区负责）。