# 多机训练

本文说明 v0.9.0 起的多机训练口径：接口怎么用、集群侧有哪些前置条件、Kueue 怎么表达
「每节点 1 卡、一个任务跨 N 节点」、NCCL 怎么排查，以及合理的性能预期。

> 版本状态（0.9.0）：**接口与校验已就位，调度落地由部署侧并行开发**
> （`deploy/kueue/multi-node/`、`images/smoke-train` 的多节点模式、`scripts/preflight.sh`
> 的多机检查项、`examples/submit-multi-node.sh`）。本文的前置条件、Kueue 配置思路和排查
> 命令可以照着做，但**在 `deploy/kueue/multi-node/` 合并并由主 Agent 实测之前，不要把
> 本文当作「多机已验收」的依据**。验收记录只在 `docs/acceptance-*.md` 中给出。

## 1. 与单机的区别

| 维度 | 单机（当前默认） | 多机（v0.9 预留） |
|---|---|---|
| 任务对象 | Master Pod × 1 | 每个节点 1 个 Pod，任务级原子放行 |
| GPU 数 | `gpus` 字段，取值在 `PLATFORM_ALLOWED_GPUS` 内 | `gpus = nodes × gpus_per_node`，由服务端计算 |
| 调度开关 | 无需配置 | `PLATFORM_MAX_NODES`，默认 `1` |
| Kueue | 单一 ResourceFlavor `gpu`，按总卡数计数 | 需要 topology + nodeLabels，按节点维度计数 |
| rendezvous | `torchrun --standalone` | `MASTER_ADDR` / `MASTER_PORT` / `RANK` / `WORLD_SIZE`，`init_method=env://` |

## 2. 前置条件

下列每一项不满足，多机任务都会卡在排队或启动阶段，且**报错位置不一定在多机相关组件上**。
按顺序检查：

| # | 前置条件 | 检查命令 | 不满足的表现 |
|---|---|---|---|
| 1 | 至少 2 个节点 Ready，且各自发布 `nvidia.com/gpu` | `kubectl get nodes -o wide`<br>`kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'` | 任务一直 `QUEUED`，Workload `Pending` |
| 2 | 每台节点的 GPU 型号、驱动、CUDA 版本一致 | `nvidia-smi --query-gpu=name,driver_version --format=csv`（每节点） | `RUNNING` 后立刻 `FAILED`，NCCL 报模型不匹配 |
| 3 | 每个节点都能看到训练镜像 | `sudo k3s ctr -n k8s.io images ls \| grep vla-smoke`（每节点） | Pod `ImagePullBackOff`（见 [`troubleshooting.md`](troubleshooting.md) 第 6 节） |
| 4 | 数据集与 checkpoint 在目标节点上路径一致 | `ls -d /data/vla-platform/datasets /data/vla-platform/checkpoints`（每节点） | Pod 启动失败或读不到数据，见 [`operations.md` 第 5 节](operations.md#5-扩容到多节点前的检查清单) |
| 5 | 节点间 Pod 网络互通（hostNetwork 无关，靠 CNI） | 在两个节点的 Pod 中互 `ping`/`nc -z <对端IP> <MASTER_PORT>` | rendezvous 超时，`Connection reset by peer` |
| 6 | Kueue 已启用 `kubeflow.org/pytorchjob` 集成 | `kubectl -n kueue-system get cm kueue-manager-config -o yaml \| grep -A20 frameworks:` | Workload 变成 `pod-<pod>-<hash>`，逐 Pod 放行，多机语义失效 |
| 7 | Kueue 的 topology 能力已启用（版本所需 feature gate） | 确切 gate 名称以 `deploy/kueue/multi-node/README.md` 为准 | ClusterQueue 上的 `topologyName` 不生效或 API 报未知字段 |
| 8 | 服务端 `PLATFORM_MAX_NODES` > 1 | `kubectl -n vla-platform get deploy vla-web -o jsonpath='{...env[?(@.name=="PLATFORM_MAX_NODES")].value}'` | 提交 `nodes: 2` 返回 422 `multi-node training is not enabled` |

第 5 条是纯多机问题才暴露的：单机训练只用到本机回环，无法发现节点间防火墙或 MTU
问题。先在两个节点各起一个临时 Pod 互测 29500 端口，再提交训练。

## 3. 接口口径

完整的字段表在 [`api.md`](api.md) §5.2。要点：

```json
{
  "name": "mn-2x1",
  "image": "vla-smoke:latest",
  "nodes": 2,
  "gpus_per_node": 1,
  "command": "torchrun --nnodes=2 --nproc_per_node=1 --node_rank=$RANK /app/train.py --distributed"
}
```

| 字段 | 约束 | 说明 |
|---|---|---|
| `nodes` | `1 <= nodes <= PLATFORM_MAX_NODES`，默认 `1` | 任务占用的节点数 |
| `gpus_per_node` | 必须属于 `PLATFORM_ALLOWED_GPUS` | 每节点卡数 |
| `gpus` | `nodes > 1` 时可省略 | 省略时服务端计算 `gpus = nodes * gpus_per_node`；显式传入必须与计算结果一致 |

- `PLATFORM_MAX_NODES` 默认 `1`，此时 `nodes > 1` 返回 422 `multi-node training is not enabled`。
**打开多机能力是服务端配置动作**，不是客户端能绕过的。
- 任务响应里的 `nodes` 字段 v0.9 固定为 `1`；`nodes > 1` 的任务只有当后端多机校验
  （`PLATFORM_MAX_NODES`）放开后才会出现。
- 配额按 `gpus` 总数计：2 节点 × 1 卡占用 2 卡额度，`user` 的 `max_gpus` 必须 ≥ 2。

## 4. Kueue：怎么表达「每节点 1 卡、一个任务跨 N 节点」

### 4.1 两种配额模型

| 模型 | ResourceFlavor | ClusterQueue 配额 | 适用 |
|---|---|---|---|
| 总量模型（当前 4090/A100） | `spec: {}`，不区分节点 | `nominalQuota: <总卡数>` | 单机；多节点时可能放行「任何单节点都放不下」的大任务 |
| 拓扑模型（多机） | `topologyName` + `nodeLabels`（如 `topology.kubernetes.io/zone` 或自定义节点标签） | 每个 topology domain 内 `nominalQuota: <每节点卡数>` | 跨节点任务，Kueue 保证各部门 Pod 落在满足拓扑约束的节点上 |

总量模型在单节点上没问题；一旦加节点，8 卡任务可能被放行到一张只有 1 卡的节点上，
Pod 随即 Pending，而 Kueue 已认为它「被准入」。多机 profile 的意义就是避免这种情况。

### 4.2 落地文件与开关

- 配额与拓扑：`deploy/kueue/multi-node/`（multinode 分区维护），用
  `kubectl apply -k deploy/kueue/multi-node/` 应用。
- Kueue 控制器配置：需要与 `deploy/kueue/base/kueue-values.yaml` 一起用 Helm 更新。
  **具体 feature gate 名称与取值以 `deploy/kueue/multi-node/README.md` 为准**——不同 Kueue
  版本对拓扑调度的开关命名不同，照抄旧版本的 gate 名会让控制器直接拒绝启动或静默忽略
  配置。
- 应用后必须验证：

```bash
kubectl get clusterqueue vla-cluster-queue -o yaml | sed -n '/topologyName/,+3p'
kubectl get resourceflavor gpu -o yaml | sed -n '/topologyName/,+3p'
kubectl -n kueue-system get cm kueue-manager-config -o yaml | grep -i -A3 'features\|featureGates'
```

### 4.3 与单机 profile 的切换

切换前先取消或等待运行中的任务结束，避免旧配额下已在跑的任务与新拓扑冲突：

```bash
kubectl -n vla-training get pytorchjob,pod
kubectl apply -k deploy/kueue/multi-node/
kubectl wait clusterqueue/vla-cluster-queue --for=condition=Active --timeout=120s
```

回滚就是重新 apply 对应单机 profile（`deploy/kueue/4090/` 或 `deploy/kueue/a100/`），并确认
没有任务在依赖拓扑放行。

## 5. 训练脚本：rendezvous 怎么来

Kubeflow Training Operator 在多副本 PyTorchJob 下会为每个 Pod 注入环境变量：

| 变量 | 含义 | 谁提供 |
|---|---|---|
| `MASTER_ADDR` | rank 0 的地址 | Training Operator |
| `MASTER_PORT` | rendezvous 端口 | Training Operator |
| `RANK` / `WORLD_SIZE` | 全局 rank 与进程总数 | Training Operator |
| `LOCAL_RANK` | 节点内序号 | torchrun |

`images/smoke-train/train.py` 的 `--distributed` 模式读取 `WORLD_SIZE` / `RANK` /
`LOCAL_RANK`，并以 `init_method="env://"` 初始化。多节点模式（0.9.0 部署分区交付）额外
打印每个 rank 的 `HOSTNAME`、GPU 与 NCCL 初始化结果——**排查多机问题先看这三样**。

单机 8 卡用 `torchrun --standalone`；多机不能用 `--standalone`（它假设所有进程在同一
主机），需要显式给出 `--nnodes` / `--nproc_per_node` / `--node_rank`。两种写法：

| 方式 | 命令片段 | 说明 |
|---|---|---|
| 静态 | `torchrun --nnodes=2 --node_rank=$RANK --nproc_per_node=1` | torchrun 从 `MASTER_ADDR` / `MASTER_PORT` 推导 rendezvous，命令最简 |
| rendezvous | `torchrun --nnodes=2 --nproc_per_node=1 --rdzv_backend=c10d --rdzv_endpoint=$MASTER_ADDR:$MASTER_PORT --rdzv_id=<job-name>` | 显式指定 rendezvous 点，便于观察重连行为 |

不论哪种，`train.py --distributed` 都从 `WORLD_SIZE` / `RANK` / `LOCAL_RANK` 读取并走
`env://`。

## 6. NCCL 排查

按「症状 → 先确认什么 → 再查什么」组织。

### 6.1 所有 rank 都起来了，但卡在初始化

```bash
kubectl -n vla-training logs <pod-name> -c pytorch --tail=200 --prefix=true
# 关注每个 rank 打印的 HOSTNAME / GPU / NCCL 初始化结果
NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,NET
```

把 `NCCL_DEBUG` 通过任务 `env` 传进去，日志量会很大，只用于定位：

| 日志关键字 | 含义 | 处理 |
|---|---|---|
| `NET/Socket : <ip> 端口不可达` | 节点间 Pod 网络不通 | 查 CNI、防火墙、NetworkPolicy |
| `connect to <ip>:<port> timed out` | 同上，或 MTU 不匹配 | 用 `ping -M do -s <mtu-28>` 验证 MTU |
| `Duplicate GPU detected` | 容器内看到同一张卡两次 | 检查 Device Plugin 是否正常发布 GPU |
| `no NVIDIA driver / CUDA failure` | 容器没拿到 GPU | 检查 `runtimeClassName: nvidia` 与 `nvidia.com/gpu` 请求 |
| `NCCL error: unhandled cuda error` | 驱动/固件不一致、或显存不足 | 对齐各节点驱动版本 |
| `Watchdog caught collective operation timeout` | 某个 rank 退出或 hang | 拉齐所有 rank 日志，找最早报错的那个 |

### 6.2 一个 rank 退出后其他 rank 卡住

训练容器的 `restartPolicy` 由 `max_retries` 决定（`0` → `Never`，`>0` → `OnFailure`）。
多机场景下建议 `max_retries: 0`：一个 rank 失败就整体失败，避免「一半重试、一半卡住」的
半死状态，然后用 [`checkpoint`](operations.md#23-checkpoint-备份) 从上一个保存点重启。

### 6.3 报错集中在某个节点

各节点分别执行，逐项对比：

```bash
nvidia-smi --query-gpu=index,name,driver_version,memory.total --format=csv
kubectl get node <node> -o jsonpath='{.status.nodeInfo.containerRuntimeVersion}{"\n"}'
kubectl -n nvidia-device-plugin get pod -o wide
```

型号、驱动、CUDA、containerd 版本任一项不同，都建议先把节点修到一致再跑多机；混合
A100 80GB 与 A100 40GB 这类「型号相同、显存不同」的机器，显存要按最小者设置 batch。

## 7. 性能预期

真实 VLA 训练的扩展性取决于模型、batch、数据管线和网络，这里只给量级判断和测量方法，
**不承诺具体加速比**。

| 场景 | 预期 |
|---|---|
| 千兆网络、2 节点 × 1 卡 | 扩展比通常明显低于 2×，通信/计算比高，适合验证链路而非追加速 |
| 万兆 / InfiniBand、2 节点多卡 | 接近线性，前提是每节点 nproc 与数据加载不成为瓶颈 |
| 跨机同一节点内卡数增加 | 单机内 NCCL 走 NVLink/PCIe，比跨机快；优先把单机卡用满 |

测量建议：

1. 固定 `--steps` 与数据，分别记录 1 节点与 N 节点的吞吐（steps/s 或 samples/s）；
2. 用 `NCCL_DEBUG=INFO` 观察是否走了预期的网络设备；
3. 同时看 `nvidia-smi` 的 GPU 利用率与 `/cluster/summary` 的 `gpu_allocated`——扩展比差时先
   区分「GPU 闲（数据/通信瓶颈）」和「GPU 满（计算瓶颈）」；
4. 对比时保证 checkpoint 频率、日志频率一致，否则会把 IO 时间算进差异。

## 8. 最小验证流程

前提：已按 §2 检查、已应用 `deploy/kueue/multi-node/`、`PLATFORM_MAX_NODES` ≥ 2。

```bash
export BASE=http://127.0.0.1:30080/api/v1
export TOKEN=<令牌>

# 1. 提交 2 节点各 1 卡的 smoke（等价于 examples/submit-multi-node.sh）
curl -fsS -X POST "$BASE/jobs" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "name": "mn-smoke-1",
    "nodes": 2,
    "gpus_per_node": 1,
    "priority": "normal",
    "command": "torchrun --nnodes=2 --nproc_per_node=1 --node_rank=$RANK /app/train.py --distributed --steps 20"
  }'

# 2. 观察排队与放行：应整体准入，而不是一个 Pod 一个 Pod 地放行
kubectl -n vla-training get workload,pytorchjob,pod -o wide

# 3. 看每个 rank 打印的 HOSTNAME / GPU / NCCL 初始化结果
curl -fsS -H "Authorization: Bearer $TOKEN" "$BASE/jobs/mn-smoke-1/logs?tail_lines=200"

# 4. 取消后确认额度释放（另一个 2 节点任务应被放行）
curl -fsS -X DELETE -H "Authorization: Bearer $TOKEN" "$BASE/jobs/mn-smoke-1"
```

通过标准：两个 Pod 落在**不同节点**、日志里两个 rank 都打印 NCCL 初始化成功、`WORLD_SIZE`
为 2、任务最终 `SUCCEEDED`；取消后 GPU 全部释放且排队任务放行。

## 9. 回滚与降级

| 想回到 | 操作 |
|---|---|
| 关闭多机能力 | 删除 Web Deployment 中的 `PLATFORM_MAX_NODES`（回到默认 `1`），`rollout restart`；此后 `nodes > 1` 一律 422 |
| 退回单机配额模型 | `kubectl apply -k deploy/kueue/4090/`（或 `a100/`），确认 ClusterQueue `Active` |
| 隐藏前端多机选项 | 前端按 `/config` 返回的 `max_nodes` 决定是否显示；服务端值为 1 时表单自然隐藏 |

回滚顺序建议：先关服务端开关（阻止新提交），再回滚 Kueue 配置，最后处理运行中的任务。