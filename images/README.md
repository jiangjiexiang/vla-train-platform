# Smoke 训练镜像（`vla-smoke:latest`）

单机与多机 smoke 都用这同一个镜像，**跨节点 NCCL 版本因此天然一致** —— 这是多机排查
里最容易踩的坑之一（两台机器装了不同的 torch/CUDA 轮子）。

```
images/smoke-train/
├── Dockerfile
└── train.py          合成训练任务（不是真实 VLA 模型）
```

## 构建与导入

```bash
examples/build-import-smoke.sh
# 等价于：
# docker build -t vla-smoke:latest images/smoke-train
# docker save vla-smoke:latest | sudo k3s ctr -n k8s.io images import -
```

无镜像仓库环境下，**每个要跑训练节点都要导入一次**，否则该节点会 `ImagePullBackOff`。

## train.py 的三种启动模式

| 模式 | 触发条件 | 后端 | 说明 |
|---|---|---|---|
| 单卡 | 默认，无 `--distributed` | 无 | 最基础的 smoke |
| 单机多卡（torchrun） | `torchrun --standalone --nproc_per_node=8 /app/train.py --distributed` | nccl（无 GPU 时 gloo） | 由 torchrun 注入 `RANK`/`WORLD_SIZE`/`MASTER_ADDR`/`MASTER_PORT` |
| **多节点** | PyTorchJob 直接跑 `python /app/train.py --distributed`，并由模板显式提供 `MASTER_ADDR`/`MASTER_PORT`/`RANK`/`WORLD_SIZE`/`LOCAL_RANK` | nccl | **本次新增** |

启动模式由 `LAUNCH_MODE` 判定并打印：`single` / `single-node-torchrun` /
`single-node-env` / **`multi-node`**（`WORLD_SIZE > LOCAL_WORLD_SIZE`）。

### 多节点模式做了什么

1. 从环境变量读 rendezvous 并做严格校验（缺 `MASTER_ADDR`/`MASTER_PORT`、`RANK` 越界、
   `WORLD_SIZE=1` 却给了 `--distributed` 等都直接报错退出，退出码 2）；
2. 固定用 `dist.init_process_group(backend="nccl", init_method="env://")`；
3. 每个 rank 打印 **一行** `RANK_INFO` JSON：`rank` / `world_size` / `local_rank` /
   `node`（主机名）/ `node_ip` / `master_addr` / `master_port` / `gpu_name` / `driver` /
   `cuda_build` / `mode`；
4. 打印 `RENDEZVOUS`、`NCCL_ENV`（`NCCL_DEBUG`、`NCCL_SOCKET_IFNAME` 等）；
5. 首次初始化后做一次 **跨节点 all_reduce 往返**，输出 `NCCL_ROUNDTRIP_OK`
   （含 `nccl_version`、`all_reduce_ms`）；
6. 结束时 `DDP_VERIFIED world_size=N ranks=[...] nodes=N mode=multi-node backend=nccl`，
   其中 `nodes` 是不同主机数 —— **`nodes` 小于 `world_size` 说明 Pod 落到了同一节点**，
   会直接报错。

### 环境变量

| 变量 | 必须 | 默认 | 说明 |
|---|---|---|---|
| `MASTER_ADDR` | 多节点必须 | — | master 的 Pod DNS 或 IP |
| `MASTER_PORT` | 多节点必须 | — | 与 PyTorchJob 的 `rdzvPort` 一致 |
| `RANK` / `WORLD_SIZE` / `LOCAL_RANK` / `LOCAL_WORLD_SIZE` | 建议 | 1 / 0 / 0 / WORLD_SIZE | 由 PyTorchJob/torchrun 提供 |
| `CHECKPOINT_DIR` | 否 | `/checkpoints/$JOB_NAME` | 只有 rank 0 写 |
| `NCCL_SOCKET_IFNAME` | 多节点建议 | — | 未设置会打印 `NCCL_HINT`，常见坑 |
| `VLA_SHARED_CHECKPOINT` | 否 | `0` | 设为 `1` 表示 checkpoint 在 RWX 共享卷上，所有 rank 都校验文件 |

### checkpoint 的语义（重要）

checkpoint 写到 `hostPath /data/vla-platform/checkpoints`（与后端渲染单机任务的写法一致），
所以**每个节点看到的是自己机器上的目录**：

- 只有 rank 0（master）写 `smoke.pt`，worker 不写；
- **worker 不做 checkpoint 存在性校验**（它本来就看不懂 rank 0 的文件）；
- 想把 checkpoint 变成真正共享的，换成 RWX PVC 并设 `VLA_SHARED_CHECKPOINT=1`。

## 校验

```bash
python3 -m py_compile images/smoke-train/train.py          # 语法
python3 scripts/check-smoke-train-rendezvous.py            # 假 torch 跑 8 个 rendezvous 场景
```

`check-smoke-train-rendezvous.py` 不下载 PyTorch：它在 `sys.modules` 里放一个
最小 torch stub，真实执行 `train.py` 的 `main()`，并断言
`init_process_group` 收到的是 `env://`、rank/world_size 正确、`nodes=2` 的多机场景
被识别为 `multi-node`。它只验证**调度逻辑**，不验证 NCCL 能否真的通信。

## 回滚

镜像与部署解耦：回滚就是把 `vla-smoke:latest` 重新导入成旧版本并让 Kueue 重跑任务。
K3s 用 `imagePullPolicy: IfNotPresent`，所以导入后需要重启对应 Pod 才会用上新镜像。

## 多机真实验证

见 [`../deploy/kueue/multi-node/README.md`](../deploy/kueue/multi-node/README.md) 第 5 节。