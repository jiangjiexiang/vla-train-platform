# CLI、Smoke 镜像与提交示例

以下命令均在项目根目录执行。CLI 只调用 `/api/v1` REST API，不直接访问 Kubernetes。

## 配置与 dry-run

```bash
read -rsp 'Platform token: ' TOKEN; echo
cli/vla-train config --server http://HOST:30080 --token "${TOKEN}"
unset TOKEN
cli/vla-train config --show
```

配置默认写入 `${XDG_CONFIG_HOME:-~/.config}/vla-train/config.json`，权限为 `0600`。可用 `VLA_TRAIN_CONFIG` 指定其他配置路径，或用 `VLA_TRAIN_SERVER`、`VLA_TRAIN_TOKEN` 临时覆盖配置；令牌不会写入示例或 dry-run 输出。

不连接平台、仅检查提交请求：

```bash
cli/vla-train --server http://127.0.0.1:30080 submit \
  --name dry-run-001 --gpus 1 --image vla-smoke:latest \
  --priority normal --env PYTHONUNBUFFERED=1 \
  --command 'python /app/train.py --steps 2 --sleep 0' --dry-run
```

## 构建并导入 Smoke 镜像

构建不需要 GPU，但需要网络拉取固定版本的 PyTorch CUDA 基础镜像。请在目标 K3s 节点执行：

```bash
examples/build-import-smoke.sh
```

等价命令：

```bash
docker build -t vla-smoke:latest images/smoke-train
docker save vla-smoke:latest | sudo k3s ctr -n k8s.io images import -
sudo k3s ctr -n k8s.io images ls name=vla-smoke:latest
```

该流程不需要镜像仓库。训练 Pod 必须运行在完成导入的同一节点；首版平台也是单节点 K3s。由于镜像名为 `vla-smoke:latest`，后端渲染的容器应设置 `imagePullPolicy: IfNotPresent`，否则 Kubernetes 对 `latest` 的默认策略可能尝试访问远端仓库。不要使用 CUDA 基础镜像之外的 `latest` 标签替代固定 PyTorch 版本。

## RTX 4090 单卡

平台 `PLATFORM_ALLOWED_GPUS` 应为 `1`，Kueue 配额为 1。

```bash
examples/submit-4090.sh
cli/vla-train status smoke-4090-001
cli/vla-train logs smoke-4090-001 --tail-lines 500
```

可通过环境变量改变名称和时长，例如：

```bash
JOB_NAME=smoke-4090-long STEPS=300 SLEEP=0.2 examples/submit-4090.sh
```

checkpoint 写入容器 `/checkpoints/smoke-4090-long/smoke.pt`，对应宿主机 `/data/vla-platform/checkpoints/smoke-4090-long/smoke.pt`。

## 单机 8×A100 DDP

平台 `PLATFORM_ALLOWED_GPUS` 应包含 `8`，Kueue 配额为 8。

```bash
examples/submit-a100-8x.sh
cli/vla-train logs smoke-a100-8x-001 --tail-lines 500
```

实际命令为 `torchrun --standalone --nproc_per_node=8 /app/train.py --distributed`。有 CUDA 时使用 NCCL，无 CUDA 时使用 gloo。每个 step 都执行全 rank 归约校验，日志末尾必须出现 `DDP_VERIFIED world_size=8`；`DDP_PARTICIPANTS` 会列出 8 个 rank 的设备。只有 rank 0 写 `/checkpoints/smoke-a100-8x-001/smoke.pt`，所有 rank 在结束前验证该文件存在。

## VLA Custom Job

平台不会下载或提供真实 VLA 模型。自备包含代码和依赖的镜像，并显式给出镜像内训练命令：

```bash
VLA_IMAGE=registry.example.com/team/vla:fixed-version \
VLA_COMMAND='python /workspace/train.py --dataset /datasets --output /checkpoints' \
VLA_GPUS=1 JOB_NAME=vla-custom-001 \
  examples/submit-vla-custom.sh
```

多卡自定义任务须由 `VLA_COMMAND` 自己使用正确的 `torchrun` 参数。数据集固定挂载到 `/datasets`（只读），checkpoint 根目录固定为 `/checkpoints`（可写）；示例不提供任意宿主机路径、特权容器或额外卷参数。

## Checkpoint 与排队演示

在 4090 配额为 1 的环境连续提交两个长任务：

```bash
examples/queue-checkpoint-demo.sh
cli/vla-train list
cli/vla-train logs queue-smoke-001
cli/vla-train cancel queue-smoke-001
cli/vla-train clone queue-smoke-002 --name queue-smoke-003
```

第一个任务运行期间第二个应保持 `QUEUED`；第一个完成或被取消后第二个由 Kueue 放行。两个任务分别写 `/checkpoints/queue-smoke-001/smoke.pt` 和 `/checkpoints/queue-smoke-002/smoke.pt`，不会因第二个任务排队而共享 checkpoint。

这些脚本会真实提交或取消任务；只有上面的 `--dry-run` 命令保证不连接平台。

## 多机（2 节点 × 1 卡）smoke

⚠️ 需要 **≥2 个 GPU 节点**、节点已打 `vla-platform.io/node-pool=multinode` 标签、
`deploy/kueue/multi-node` 已应用、Kueue 已启用 `TopologyAwareScheduling`。
本仓库的单卡开发机上跑会被前置检查拦下（预期行为）。
完整前置条件与验证步骤见 `deploy/kueue/multi-node/README.md`。

```bash
# 1. 只检查前置条件并打印将提交的 PyTorchJob，不创建资源
MODE=inspect examples/submit-multi-node.sh

# 2. 提交 2 节点 × 1 卡
examples/submit-multi-node.sh

# 3. 3 节点
NODES=3 examples/submit-multi-node.sh

# 4. 观察结果
kubectl -n vla-training get pytorchjob,pod -o wide
kubectl -n vla-training get workloads.kueue.x-k8s.io -o yaml | grep -A12 topologyAssignment
kubectl -n vla-training logs -l app.kubernetes.io/component=multi-node-smoke --prefix \
  | grep -E 'RANK_INFO|NCCL_ROUNDTRIP_OK|DDP_VERIFIED'

# 5. 清理
MODE=delete examples/submit-multi-node.sh
```

脚本参数（环境变量）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `JOB_NAME` | `multi-node-smoke` | PyTorchJob 名称 |
| `NODES` | `2` | 节点数（master 1 + worker N-1） |
| `GPUS_PER_NODE` | `1` | 每节点卡数 |
| `STEPS` / `SLEEP` | `20` / `0` | smoke 步数与每步休眠 |
| `IMAGE` | `vla-smoke:latest` | 训练镜像，需已导入**每个**节点 |
| `QUEUE` | `multinode` | 多机 LocalQueue |
| `NCCL_IFNAME` | `eth0` | 跨节点 NCCL 网卡，多网卡机器必须改 |
| `MODE` | `submit` | `submit` / `inspect` / `delete` |

成功判据（rank 0 日志里必须同时出现）：

```
LAUNCH_MODE multi-node world_size=2
NCCL_ROUNDTRIP_OK {"...": "...", "world_size": 2, ...}
DDP_VERIFIED world_size=2 ranks=[0, 1] nodes=2 mode=multi-node backend=nccl
```

`nodes=1` 表示两个副本落到了同一节点，也就是 TAS 没生效。

### 与 CLI 的区别

`examples/submit-multi-node.sh` **不走平台 API**，直接 `kubectl apply` 一个 PyTorchJob。
原因：后端 `SubmitRequest` 的 `nodes` / `gpus_per_node` 字段（契约 1.7）目前只做接受与校验，
多机 PodSet 的渲染属于 deploy 分区的落地范围；等后端渲染支持后，这个脚本可以换成
`cli/vla-train submit --nodes 2 --gpus-per-node 1`。

checkpoint 写在 `/checkpoints/<job>/smoke.pt`，每个节点看到的是自己机器上的 hostPath 目录，
只有 master 会写；这一点与单机示例一致，**不要**期望跨节点共享。

## 相关脚本一览

| 脚本 | 运行环境 | 说明 |
|---|---|---|
| `submit-4090.sh` | 单机 1 卡 | 平台 API 提交 |
| `submit-a100-8x.sh` | 单机 8 卡 | `torchrun --standalone` |
| `submit-vla-custom.sh` | 任意 | 自有镜像 |
| `queue-checkpoint-demo.sh` | 单机 | 排队/取消演示 |
| **`submit-multi-node.sh`** | **≥2 GPU 节点** | **多机 PyTorchJob，直接 kubectl apply** |
| `build-import-smoke.sh` | 每台训练节点 | 构建并导入 smoke 镜像 |
