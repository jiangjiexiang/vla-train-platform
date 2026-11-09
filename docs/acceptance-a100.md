# 单机 8×A100 验收清单

适用环境：单节点 K3s、8 张 NVIDIA A100。本文是执行记录模板，不代表这些项目已经通过。所有标注 **真实 GPU** 的项目必须在目标 A100 机器上运行训练后才能勾选。

## 1. 记录信息

- 验收日期 / 执行人：待填写
- 代码版本：待填写
- 节点名、A100 型号/显存、驱动版本：待填写
- K3s、Kueue、Training Operator、PyTorch 版本：待填写
- Web / smoke 镜像摘要：待填写
- 失败项与issue链接：待填写

## 2. 构建与部署检查（不等于 GPU 验收）

- [ ] 前端生产构建、后端检查及 `backend/Dockerfile` 单容器构建通过。
- [ ] `images/smoke-train` 构建为 `vla-smoke:latest`，Web 与 smoke 镜像均已导入 K3s/containerd。
- [ ] `deploy/kueue/a100/` 已应用，Web 的 `PLATFORM_ALLOWED_GPUS=1,2,4,8`。
- [ ] `bash -n scripts/verify-a100.sh` 通过。
- [ ] 默认验收脚本通过：

  ```bash
  bash scripts/verify-a100.sh
  ```

默认模式应确认：单节点 Ready、GPU Capacity 总数为 8、NVIDIA Device Plugin/Kueue/Training Operator/Web 工作负载就绪、ResourceFlavor/LocalQueue/ClusterQueue 正确、GPU 配额为 8、NodePort 与 `/api/v1/health` 正常。它不会启动训练，不能证明 DDP、NCCL 或 checkpoint 可用。

## 3. 集群与网页检查

- [ ] **真实 GPU 环境**：宿主机 `nvidia-smi` 显示 8 张预期 A100，驱动正常。
- [ ] 节点 Capacity 和 Allocatable 均有 `nvidia.com/gpu: 8`。
- [ ] ClusterQueue `vla-cluster-queue` 的 `nvidia.com/gpu` nominalQuota 为 8。
- [ ] **Kueue 已启用 `kubeflow.org/pytorchjob`，排队粒度是整个 PyTorchJob 而不是单个 Pod。**
  先看配置：

  ```bash
  kubectl -n kueue-system get cm kueue-manager-config -o yaml | grep -A20 frameworks
  ```

  应包含 `kubeflow.org/pytorchjob`。再确认 Workload 归属：

  ```bash
  kubectl -n vla-training get workloads -o custom-columns=NAME:.metadata.name,OWNER:.metadata.ownerReferences[0].kind
  ```

  `OWNER` 必须是 `PyTorchJob`。若是 `Pod`，说明集成没开，8 卡任务会被逐 Pod 放行而不是整体排队。
  `scripts/verify-a100.sh` 已包含这项检查，可用 `SKIP_KUEUE_INTEGRATION_CHECK=1` 跳过。
- [ ] `/login` 无令牌或错误令牌不能提交、取消任务；正确令牌可以访问。
- [ ] `/dashboard` 显示 GPU 总数 8，节点 Ready，空闲/分配数随任务变化；实时使用情况分别显示 8 张卡的核心利用率、显存和温度。
- [ ] `/submit` 只允许 1、2、4、8 卡，其他数值被 API 拒绝。
- [ ] `/queues` 显示配额 8、实际用量和待放行任务。

## 4. 8 卡 DDP smoke（真实 GPU）

- [ ] 提交 8 卡任务，镜像 `vla-smoke:latest`，命令：

  ```bash
  torchrun --standalone --nproc_per_node=8 /app/train.py --distributed --steps 100
  ```

- [ ] 任务进入 `RUNNING` 并最终为 `SUCCEEDED`。
- [ ] 日志显示 8 个训练进程、每进程可用 GPU、训练进度和完成信息。
- [ ] 无 NCCL 初始化、通信、超时或进程退出错误。
- [ ] Pod 的 `nvidia.com/gpu` requests 和 limits 均为 8。
- [ ] checkpoint 写入 `/checkpoints/<job-name>`，宿主机对应目录存在且完成后保留。
- [ ] 使用支持恢复的自有训练代码验证重启后能从 checkpoint 恢复，并记录恢复文件、日志和结果。Smoke 只写 checkpoint，没有恢复参数，不能用 smoke 勾选本项。

可选自动端到端检查（会同时提交两个 8 卡任务，真实占用 8 张 GPU）：

```bash
PLATFORM_API_TOKEN="$PLATFORM_API_TOKEN" \
  bash scripts/verify-a100.sh --e2e --8gpu-smoke
```

该模式检查一运行一排队、取消后的自动放行，并等待第二个 8 卡任务成功。仍应人工检查 8 进程日志、NCCL 和 checkpoint。普通 `--e2e` 只运行一个单卡短 smoke，不能据此宣称 8 卡验收通过。

## 5. 配额、排队与取消（真实 GPU）

- [ ] 连续提交两个各申请 8 卡、运行时间足够长的任务，一个 `RUNNING`、一个 `QUEUED`。
- [ ] 取消运行任务后，8 张卡全部释放，第二个任务自动运行。
- [ ] 第一个 8 卡任务自然完成后，后续排队任务也能自动放行。
- [ ] 在空闲配额内分别提交 1、2、4 卡任务，网页和资源统计正确；超额请求保持排队而非超分 GPU。
- [ ] clone 生成的新任务保留原提交参数，并使用不同任务名。
- [ ] 重复取消安全，取消历史可查。

## 6. 失败、历史与自有代码（真实 GPU）

- [ ] 明确失败的短任务最终为 `FAILED`，网页展示失败原因和 Pod 事件，不无限重启。
- [ ] **Pod 事件可见**：`GET /api/v1/jobs/{name}/events` 按 `last_seen` 倒序返回事件，
  失败任务能查到 `reason` / `message` / `object_kind` / `object_name`；取消并删除对象后返回 200 和空 `items`。

  ```bash
  curl -s -H "Authorization: Bearer $PLATFORM_API_TOKEN" \
    http://127.0.0.1:30080/api/v1/jobs/<job-name>/events | python3 -m json.tool | head -30
  cli/vla-train events <job-name>
  ```
- [ ] Web Deployment 重启后实时任务不丢失，历史提交仍可查看。
- [ ] 自有 VLA 代码使用小数据/适当配置完成一次小规模 8 卡训练。
- [ ] 自有代码日志确认使用 8 卡，输出写入 `/checkpoints/<job-name>`；没有依赖任意宿主机挂载或特权容器。
- [ ] 数据集在容器中只读挂载于 `/datasets`，checkpoint 目录可写且任务结束后保留。

## 7. 结论

- 静态/集群检查：待填写
- 单卡 API smoke：未实测 / 通过 / 失败
- 8 卡 DDP/NCCL：未实测 / 通过 / 失败
- 8 卡排队与取消：未实测 / 通过 / 失败
- Checkpoint 及恢复：未实测 / 通过 / 失败
- 自有 VLA 小规模 8 卡训练：未实测 / 通过 / 失败
- 最终结论与遗留风险：待填写
