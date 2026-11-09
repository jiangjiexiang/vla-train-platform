# 故障排查

先保存现象、发生时间、任务名和完整错误，再执行只读诊断。不要因为状态异常就直接删除 Namespace、CRD 或宿主机数据。以下命令默认在平台节点执行；将 `<...>` 替换为实际值。

## 1. NVIDIA runtime：容器不能识别 GPU

典型现象：宿主机 `nvidia-smi` 正常，但训练日志显示 CUDA 不可用、容器内 `nvidia-smi` 失败，或 Pod 报 NVIDIA runtime 错误。

```bash
nvidia-smi
kubectl get nodes -o wide
kubectl describe node <node-name>
sudo k3s crictl info
sudo k3s ctr images ls
journalctl -u k3s-agent -n 200 --no-pager
journalctl -u k3s -n 200 --no-pager
```

检查顺序：

1. 驱动与 GPU 是否在宿主机正常；驱动异常时先修复驱动，不要反复重启训练。
2. NVIDIA Container Toolkit 是否已安装，K3s containerd 是否使用 NVIDIA runtime。安装/变更入口是 `scripts/install-k3s-gpu.sh`，核对其实际输出，不要手写覆盖已有 containerd 配置。
3. 修改 runtime 后是否按安装脚本要求重启 K3s，节点是否重新 Ready。
4. 节点是否已通过 Device Plugin 发布 `nvidia.com/gpu`。只有 runtime 和 Device Plugin 都正常，容器 CUDA 才可用。

真实 GPU 判定：必须运行 smoke 并在日志中确认 CUDA/GPU；`nvidia-smi` 在宿主机成功或脚本默认检查通过都不够。

## 2. Device Plugin：节点 GPU Capacity 为 0 或不正确

```bash
kubectl -n kube-system get daemonset nvidia-device-plugin-daemonset -o wide
kubectl -n kube-system get pods -o wide
kubectl -n kube-system logs daemonset/nvidia-device-plugin-daemonset --tail=200
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\tCapacity="}{.status.capacity.nvidia\\.com/gpu}{"\tAllocatable="}{.status.allocatable.nvidia\\.com/gpu}{"\n"}{end}'
kubectl describe node <node-name>
```

- DaemonSet 不存在：核对 `scripts/install-stack.sh` 或 `scripts/install-k3s-gpu.sh` 的实际安装步骤和固定版本清单。
- Pod 未就绪：查看事件、驱动兼容性、挂载和权限；不要通过特权训练容器绕过插件。
- Capacity 为 0：先修 Device Plugin，再排查 Kueue；Kueue 不能发现插件尚未发布的 GPU。
- 4090 应为 1，A100 单机应为 8。容量正确仍不代表 CUDA 训练成功，需要真实 smoke。

## 3. PyTorchJob / Pod 一直 Pending

```bash
kubectl -n vla-training get pytorchjob,pod,workload -o wide
kubectl -n vla-training describe pytorchjob <job-name>
kubectl -n vla-training describe pod <pod-name>
kubectl -n vla-training get events --sort-by=.lastTimestamp
kubectl describe clusterqueue vla-cluster-queue
kubectl -n vla-training describe localqueue default
```

重点区分：

- **Kueue 未放行**：还没有训练 Pod 是预期行为。检查 ClusterQueue 剩余配额、其他已放行任务和 Workload 状态。
- **已放行但 Pod Pending**：检查节点 Ready、GPU Allocatable、污点/容忍、镜像卷和其他调度事件。
- **任务请求超出配置**：4090 单任务只能为 1 卡；A100 只能为 1/2/4/8 卡，且两个 8 卡任务不能同时放行。
- **取消后仍不放行**：确认原 PyTorchJob/Pod 已删除，再检查 Workload 和 ClusterQueue 用量是否更新。
  注意 Pod 有优雅退出期：默认 `terminationGracePeriodSeconds=30`，这 30 秒内 GPU 仍被旧 Pod 占用，
  排队任务不会启动。若希望更快释放，提交时传 `termination_grace_period_seconds`（1-600），
  例如 5 秒可把「取消到下一个任务开始运行」从约 32 秒降到约 8 秒。
  Pod 仍显示 Terminating 属正常，超过宽限期+30 秒仍未删除才需要排查。

Pod 的 requests 和 limits 都必须是同一个整数 `nvidia.com/gpu`，否则 Kueue 统计和实际资源可能不一致。

## 4. Kueue 未接管任务

```bash
kubectl -n kueue-system get deployment kueue-controller-manager
kubectl -n kueue-system get pods -o wide
kubectl -n kueue-system logs deployment/kueue-controller-manager --tail=200
kubectl get resourceflavor gpu
kubectl describe clusterqueue vla-cluster-queue
kubectl -n vla-training get localqueue default -o yaml
kubectl -n vla-training get pytorchjob <job-name> -o yaml
kubectl -n vla-training get workload -o wide
```

核对：

- PyTorchJob 有 `kueue.x-k8s.io/queue-name=default`，优先级标签只能是 `low`、`normal` 或 `high`。
- LocalQueue `default` 指向 ClusterQueue `vla-cluster-queue`，ClusterQueue 使用 ResourceFlavor `gpu`。
- 配额清单来自与硬件一致的 `deploy/kueue/4090/` 或 `deploy/kueue/a100/`。
- Kueue controller 就绪且支持当前 Training Operator/PyTorchJob 集成版本。
- 不要通过删除 Kueue 标签让任务绕过排队；这会破坏平台配额语义。

## 5. NCCL / 8 卡 DDP 异常

仅在单机 8×A100 任务中使用契约命令：

```bash
torchrun --standalone --nproc_per_node=8 /app/train.py --distributed
```

诊断：

```bash
kubectl -n vla-training get pod -o wide
kubectl -n vla-training logs <pod-name> -c pytorch --tail=1000
kubectl -n vla-training describe pod <pod-name>
nvidia-smi
```

检查：

1. Pod 是否实际请求并限制 8 张 GPU，日志是否显示 8 个进程；只有 1 个进程不能算 8 卡验收。
2. 命令是否包含 `--distributed`，`torchrun` 的 `--nproc_per_node` 是否等于 8。
3. 首个 NCCL 错误之前的 CUDA、进程启动和网络信息；不要只保留最后一行异常。
4. 可在网页提交表单的环境变量中临时增加 `NCCL_DEBUG=INFO` 获取详细日志。不要把排障开关写成所有任务的永久默认值。
5. 镜像内 PyTorch/CUDA 与宿主机驱动是否兼容，是否使用了同一 smoke 镜像。

默认验收脚本和单卡 smoke 都不能证明 NCCL 正常。必须让 8 卡任务真实完成，并检查完整日志。

## 6. 镜像拉取失败

典型事件：`ImagePullBackOff`、`ErrImagePull`、镜像不存在或仓库认证失败。

```bash
# 有 Docker 时
command -v docker >/dev/null 2>&1 && docker images

# 只有 Buildah 时（本机无 Docker）
command -v buildah >/dev/null 2>&1 && buildah images

sudo k3s ctr images ls
kubectl -n vla-training describe pod <pod-name>
kubectl -n vla-platform get deployment,pod
kubectl -n vla-platform describe pod <web-pod-name>
```

本地无 registry 流程（Docker 与 Buildah 二选一）：

```bash
cd /path/to/vla-train-platform

# 路径 A：Docker 可用
docker save vla-smoke:latest -o /tmp/vla-smoke.tar

# 路径 B：只有 Buildah 1.23+
# buildah bud 的产物默认带 localhost/ 前缀，必须先重 tag 成 K3s 能识别的名字。
# 若镜像本来就带完整仓库名（例如 docker.io/library/vla-smoke:latest），可跳过 tag。
buildah tag localhost/vla-smoke:latest docker.io/library/vla-smoke:latest
buildah push docker.io/library/vla-smoke:latest docker-archive:/tmp/vla-smoke.tar

sudo k3s ctr images import /tmp/vla-smoke.tar
sudo k3s ctr images ls
rm -f /tmp/vla-smoke.tar
```

Web 镜像以 `Dockerfile` 构建，标签必须与 `deploy/web/` 中 Deployment 完全一致，并同样导入 K3s/containerd。没有 Docker 时，等价构建流程为：

```bash
cd /path/to/vla-train-platform
buildah bud --isolation=chroot -t vla-platform:local -f Dockerfile .
buildah tag localhost/vla-platform:local docker.io/library/vla-platform:local
buildah push docker.io/library/vla-platform:local docker-archive:/tmp/vla-platform.tar
sudo k3s ctr images import /tmp/vla-platform.tar
rm -f /tmp/vla-platform.tar
```

训练镜像默认必须为 `vla-smoke:latest`。若使用本地导入镜像，将拉取策略设为 `IfNotPresent`；`latest` 搭配 `Always` 会尝试外网仓库。不要仅看 Docker 或 Buildah 本机存在镜像就认定 containerd 可用。

## 7. 日志为空、中断或与状态不一致

```bash
cli/vla-train status
cli/vla-train logs <job-name>
kubectl -n vla-training get pytorchjob,pod -o wide
kubectl -n vla-training logs <pod-name> -c pytorch --tail=500
kubectl -n vla-training describe pod <pod-name>
```

- `QUEUED` 且没有 Pod：API 按契约返回 200、空日志和 `pod_name: null`，不是日志故障。
- Pod 尚未启动或刚重启：等待状态变化并查看 Pod 事件。
- 容器运行但没有应用日志：确认训练命令向 stdout/stderr 输出，可设置字符串环境变量 `PYTHONUNBUFFERED=1`。
- 日志不完整：API 默认/查询有尾部行数限制；按需调整日志查询的 `tail_lines`，不要无限制拉取拖垮网页。
- 已取消任务：实时 Pod 已删除，应查看取消历史和此前保存的日志；平台不承诺永久保存已删除 Pod 的 Kubernetes 日志。

## 8. Web API、令牌与 RBAC

```bash
curl -fsS http://127.0.0.1:30080/api/v1/health
kubectl -n vla-platform get deployment,service,pod,serviceaccount
kubectl -n vla-platform get pods -o wide
kubectl -n vla-platform logs <web-pod-name> --tail=200
kubectl -n vla-training get pytorchjob
kubectl auth can-i list pytorchjobs.kubeflow.org --namespace=vla-training \
  --as=system:serviceaccount:vla-platform:<web-service-account>
kubectl auth can-i get nodes --as=system:serviceaccount:vla-platform:<web-service-account>
```

按响应区分：

- 健康检查失败：检查 Web Pod、Service、NodePort `30080`、防火墙以及 `WEB_URL`，健康接口本身不需要令牌。响应中的 `auth_required` 表示当前是否开启鉴权。
- 401/403 且后端日志显示鉴权失败：确认浏览器/CLI 使用最新 `PLATFORM_API_TOKEN`，请求头为 `Authorization: Bearer <token>`。开启鉴权时，除健康检查外的 API 都需要令牌；不要把令牌写进脚本或命令行历史。当前 4090 overlay 可通过 `PLATFORM_AUTH_REQUIRED=false` 免登录，该模式只允许用于可信内网。
- Kubernetes API 403：通常是 Web ServiceAccount RBAC 不足。后端应能管理 `vla-training` 中的任务并只读节点信息，但不应获得无关命名空间权限。核对 `deploy/base/` 中实际 RBAC 与 ServiceAccount 绑定。
- API 正常但网页失败：检查浏览器实际错误响应和前端构建的 `VITE_API_BASE`；默认应为 `/api/v1`，不要让浏览器访问容器内部地址。
- GPU 实时指标不可用：检查 `vla-platform/vla-gpu-metrics` DaemonSet、Service 和 Pod 日志。该服务通过 `nvidia-smi` 提供核心利用率、显存和温度，不申请训练 GPU 配额；Kubernetes 中的 GPU 分配数与实时核心利用率是不同指标。
- Web 重启后历史丢失：检查 `/data/history/platform.db` 是否挂载持久卷；实时任务状态仍以 K3s 为准，不应从 SQLite 重建调度。

可用 API 做最小只读验证（开启鉴权时，令牌只从环境变量读取；免登录时可省略 Authorization 头）：

```bash
curl -fsS -H "Authorization: Bearer $PLATFORM_API_TOKEN" \
  http://127.0.0.1:30080/api/v1/config
curl -fsS -H "Authorization: Bearer $PLATFORM_API_TOKEN" \
  http://127.0.0.1:30080/api/v1/cluster/summary
```

## 9. `docker: command not found` 不是故障

**现象**：执行 `docker ps`、`docker build` 或沿用旧习惯的命令时提示 `docker: command not found`，
于是被判断为「环境残缺、需要装 Docker」。

**这不是故障，是设计。** 本机没有 Docker，容器运行时是 K3s 内置的 containerd，构建用 buildah，
`docker.service` 与 Docker/containerd 软件包本来就没有安装。不要为此安装 Docker，原因与替代命令见
`docs/operations.md` 第 6 节「容器运行时选型：buildah + K3s，而不是 Docker」。

| 原来想执行 | 改用它 |
|---|---|
| `docker ps` | `sudo k3s crictl ps`（endpoint 配置见 `docs/operations.md` 6.5） |
| `docker logs <x>` | `kubectl -n <ns> logs <pod> -c <container> --tail=500` |
| `docker exec -it <x> sh` | `sudo k3s crictl exec -it <container-id> sh` |
| `docker build -t <tag> .` | `buildah bud -t <tag> .` |
| `docker images` | `sudo k3s ctr -n k8s.io images ls` |
| `docker run --gpus all ...` | 走平台提交 PyTorchJob，不要手动占卡 |

需要排查的「真故障」是下游环节，不是 `docker` 缺失：镜像没导入看 `sudo k3s ctr -n k8s.io images ls`
（见第 6 节），Pod 起不来看 `kubectl describe pod`（见第 3 节），NodePort 不通见
`docs/operations.md` 4.8。

## DataLoader worker killed by signal: Bus error

### 现象

训练刚开始（通常是第 0 步）就失败：

```
RuntimeError: DataLoader worker (pid 192) is killed by signal: Bus error.
It is possible that dataloader's workers are out of shared memory.
Please try to raise your shared memory limit.
```

GPU 完全没用上（util 0%），看起来像数据问题，实际是**容器共享内存不够**。

### 原因

容器默认的 `/dev/shm` 只有 64MB。PyTorch DataLoader 通过共享内存把 batch
从 worker 进程传给主进程，`num_workers` 越多、`batch_size` 越大，需要的
共享内存越大。64MB 在 `batch_size=8` + `num_workers=4` 时必然被打爆。

### 平台的处理

训练 Pod 模板默认挂一块 4Gi 内存盘到 `/dev/shm`，不需要用户做任何事。
大小可以用 `PLATFORM_SHM_SIZE` 调整（比如换到内存更大的机器时可以调大）。

### 实测对比

同一份代码、同一个镜像、同样的 `batch_size=8 / num_workers=4`：

| 任务 | Pod 的卷 | 结果 |
|---|---|---|
| `smolvla-pick1028-001` | datasets, checkpoints | 第 0 步 Bus error |
| `smolvla-pick1028-002` | **dshm(4Gi)**, datasets, checkpoints | 500 步跑完，193 秒 |

### 如果还遇到

训练启动时的「环境自检」会打印 `shm_gb` 和 `num_workers`。先看这两个值：

- `shm_gb` 明显小于 1 → 说明挂载没生效，检查 Pod 定义里有没有 `dshm` 卷
- `shm_gb` 够但还是崩 → 把 `PLATFORM_SHM_SIZE` 调大，或降低 `num_workers`

降低 `num_workers` 是退而求其次：它会拖慢数据加载，不解决根本问题。

### 一个容易忽略的点

小参数训练（比如 `batch_size=2` + `num_workers=2`）在 64MB 下**能跑通**。
所以不能凭一次小规模训练就认为环境没问题——真实配置下的验证才算数。
这个坑我们是在跑 500 步验证时才暴露的。

