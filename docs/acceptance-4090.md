# RTX 4090 验收清单

适用环境：单节点 K3s、单张 NVIDIA RTX 4090。本文是执行记录模板，不代表这些项目已经通过。所有标注 **真实 GPU** 的项目必须在目标 4090 机器上运行训练后才能勾选。

## 1. 记录信息

- 验收日期 / 执行人：待填写
- 代码版本：待填写
- 节点名、GPU 型号与驱动版本：待填写
- K3s、Kueue、Training Operator 版本：待填写
- Web / smoke 镜像摘要：待填写
- 失败项与issue链接：待填写

## 2. 构建与部署检查（不等于 GPU 验收）

- [ ] 前端生产构建、后端检查及 `backend/Dockerfile` 单容器构建通过。
- [ ] `images/smoke-train` 构建为 `vla-smoke:latest`，Web 与 smoke 镜像均已导入 K3s/containerd。
- [ ] `deploy/kueue/4090/` 已应用，Web 的 `PLATFORM_ALLOWED_GPUS=1`。
- [ ] `bash -n scripts/verify-4090.sh` 通过。
- [ ] 默认验收脚本通过：

  ```bash
  bash scripts/verify-4090.sh
  ```

默认模式应确认：单节点 Ready、GPU Capacity 总数为 1、NVIDIA Device Plugin/Kueue/Training Operator/Web 工作负载就绪、ResourceFlavor/LocalQueue/ClusterQueue 正确、GPU 配额为 1、NodePort 与 `/api/v1/health` 正常。它不会启动训练，因此不能证明 CUDA 或 checkpoint 可用。

## 3. 集群与网页检查

- [ ] **真实 GPU 环境**：宿主机 `nvidia-smi` 显示预期的 4090，驱动正常。
- [ ] 节点 Capacity 和 Allocatable 均有 `nvidia.com/gpu: 1`：

  ```bash
  kubectl get nodes -o wide
  kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.capacity.nvidia\\.com/gpu}{"\n"}{end}'
  ```

- [ ] ClusterQueue `vla-cluster-queue` 的 `nvidia.com/gpu` nominalQuota 为 1。
- [ ] Kueue 已启用 `kubeflow.org/pytorchjob` 集成，排队粒度是 PyTorchJob 而不是单个 Pod：

  ```bash
  kubectl -n kueue-system get cm kueue-manager-config -o yaml | grep -A20 "frameworks:"
  kubectl -n vla-training get workload -o yaml | grep -A3 "ownerReferences:"
  ```

  `frameworks` 必须包含 `kubeflow.org/pytorchjob`，且 Workload 的 owner 应是 PyTorchJob。若只启用 `pod`，Workload 会变成 `pod-<pod>-<hash>`，需用 `deploy/kueue/base/kueue-values.yaml` 重装 Kueue 后再执行本节其余检查。默认验收脚本 `scripts/verify-4090.sh` 已包含该项检查。

- [ ] 当前免登录配置下，不携带令牌可以访问网页和 API；恢复鉴权后，`/login` 无令牌或错误令牌不能提交、取消任务，正确令牌可以访问。
- [ ] `/dashboard` 显示 GPU 总数 1，节点 Ready，空闲/分配数随任务变化；实时使用情况显示该卡核心利用率、显存和温度。
- [ ] `/queues` 显示配额 1，并能看到已放行和待放行任务。
- [ ] `/submit` 的 GPU 选项只有 1；非法任务名、非法优先级和非法 GPU 数被 API 拒绝。

## 4. 单卡 smoke（真实 GPU）

- [ ] 通过网页或 CLI 提交 `smoke001`，镜像 `vla-smoke:latest`、1 卡、命令 `python /app/train.py --steps 100`。
- [ ] 任务从 `QUEUED` 进入 `RUNNING`，最终为 `SUCCEEDED`。
- [ ] 日志清楚显示 CUDA 可用、实际 GPU、训练进度和完成信息；容器内 CUDA 检查成功。
- [ ] Pod 的 `nvidia.com/gpu` requests 和 limits 均为 1。
- [ ] checkpoint 写入容器 `/checkpoints/smoke001`，宿主机 `/data/vla-platform/checkpoints/smoke001` 中文件存在且任务结束后保留。

  ```bash
  ls -la /data/vla-platform/checkpoints/smoke001
  ```

- [ ] Web 日志视图能持续刷新；排队阶段无 Pod 时返回空日志而不是报错。

可选自动端到端检查（会真实使用 GPU）：

```bash
PLATFORM_API_TOKEN="$PLATFORM_API_TOKEN" bash scripts/verify-4090.sh --e2e
```

该模式提交两个短任务，检查一运行一排队、取消后的自动放行，并让第二个任务完成。它仍需结合人工日志和 checkpoint 检查，不能替代整份清单。

## 5. 排队、取消与 clone（真实 GPU）

- [ ] 连续提交两个各申请 1 卡、运行时间足够长的任务，一个 `RUNNING`、一个 `QUEUED`。
- [ ] 第一个完成后，第二个无需人工修改即被 Kueue 放行。
- [ ] 再次形成一运行一排队后，取消运行任务；GPU 释放，排队任务自动运行。
- [ ] 取消记录可在历史中查看；重复取消不会报错。
- [ ] 从原任务 clone 为新的合法任务名，镜像、GPU 数、命令、环境变量、优先级和重启次数与原参数一致。
- [ ] 同名活动任务、含大写/下划线或超过 50 字符的任务名不能提交。

## 6. 失败、历史与数据（真实 GPU）

- [ ] 提交一个明确失败的短命令后，任务最终为 `FAILED`，网页可见失败原因和 Pod 事件。
- [ ] **Pod 事件可见**：`GET /api/v1/jobs/<job-name>/events` 返回 `{"name": ..., "items": [...]}`，`items` 按 `last_seen` 倒序，失败任务能查到带 `reason`、`message`、`object_kind`、`object_name` 的条目；取消并删除对象后返回空 `items`：

  ```bash
  curl -fsS "http://127.0.0.1:30080/api/v1/jobs/<job-name>/events" | python3 -m json.tool
  ```

- [ ] `max_retries=0` 时不无限重启；1/2 次重试行为与提交参数一致。
- [ ] 重启 Web Deployment 后，K3s 中的实时任务不丢失，历史提交仍能查看。
- [ ] 取消后训练 Pod 被删除，checkpoint 不被自动删除。
- [ ] 自有 VLA 代码使用小数据或 LoRA 配置完成一次小规模单卡链路验证；完整模型训练不是本清单的通过条件。

## 7. 结论

- 静态/集群检查：待填写
- 单卡 CUDA smoke：未实测 / 通过 / 失败
- 排队与取消：未实测 / 通过 / 失败
- Checkpoint：未实测 / 通过 / 失败
- 自有 VLA 小规模训练：未实测 / 通过 / 失败
- 最终结论与遗留风险：待填写

## 7. 2026-11-09 实测记录（RTX 4090 单卡）

环境：K3s v1.36.6+k3s1、Kueue v0.20.5（已启用 `kubeflow.org/pytorchjob` 集成）、Training Operator v1.9.4、
NVIDIA device plugin v0.20.1、驱动 595.71.05、Web 镜像 `vla-platform:local`（含 `/jobs/{name}/events`）。

**通过（有命令输出为证）**

- 单卡真实训练 `smoke001`：API 提交后 `QUEUED → RUNNING → SUCCEEDED`，5 秒完成。
  日志显示 `CUDA available: True`、`GPU 0: NVIDIA GeForce RTX 4090 compute capability 8.9 memory 47.37 GiB`、
  loss 从 0.004977 降到 0.001585。
- checkpoint 写入 `/data/vla-platform/checkpoints/smoke001/smoke.pt`（3352 字节），任务结束后保留；
  在 `vla-smoke:latest` 容器内 `torch.load` 可还原：`step=5 world_size=1 cuda=12.4 torch=2.5.1+cu124`。
- 排队：并发提交 `qa`（60 步）与 `qb`（5 步），`qa=RUNNING`、`qb=QUEUED`（`workload=Pending`、无 Pod）；
  取消 `qa` 后 `qb` 被放行并最终 `SUCCEEDED`。
- 失败可诊断：镜像不存在时任务 `message` 为 `Admitted but pod is not ready: ...`，
  `GET /api/v1/jobs/{name}/events` 能查到 `Failed` / 镜像拉取相关事件。

**已发现但未修（用户可感知）**

- 取消正在运行的任务后，排队任务约 **32 秒**后才从 Admitted 变为 Running。原因是 PyTorchJob 模板未设置
  `terminationGracePeriodSeconds`，Pod 走默认 30 秒宽限期让训练进程退出，期间 GPU 仍被占用。
  功能正确，但“取消即释放”的体感是延迟 30 秒左右。改为更小的值可加快释放，代价是取消时留给
  checkpoint 写盘的窗口变短，需按实际训练时长权衡。

**仍未验证**

- Web Deployment 重启后实时任务与历史是否保留。
- `clone` 端到端、失败任务的 `FAILED` 终态与 `max_retries` 行为。
- 单机 8×A100 全部项目。
