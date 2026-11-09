# Changelog

本项目遵循语义化版本。每次大版本更新提交一次 commit 并打 tag，新条目追加到文件顶部。

## [0.1.0] - 2026-11-09

首个可运行版本：单节点 K3s + Kueue + Kubeflow Training Operator 的 VLA 训练排队平台。

### 平台能力

- Web（React + TypeScript + Ant Design）五个页面：总览、提交、任务列表、任务详情、队列。
- FastAPI 后端：任务提交/列表/详情/日志/取消/clone、队列、集群汇总、Pod 事件。
- Kueue 排队与 GPU 配额，4090（1 卡）与 A100（8 卡）两套 profile。
- PyTorchJob 模板、smoke 训练镜像、`vla-train` CLI。
- 单容器部署，NodePort 30080；SQLite 只记录提交与取消历史，调度状态以 K3s 为准。

### 本次修复

- 启用 Kueue `kubeflow.org/pytorchjob` 集成，排队粒度从 Pod 恢复为整个 PyTorchJob。
- 新增 `GET /api/v1/jobs/{name}/events`，暴露 PyTorchJob 与 Pod 事件。
- 任务已放行但 Pod 未就绪时，`message` 显示真实原因（如 ImagePullBackOff）。
- 修复 Kubernetes 字段为 null 时 `GET /jobs/{name}` 抛 TypeError 并返回 500。
- 未捕获异常统一返回 `{"detail": ...}` JSON，不再输出纯文本 500。
- 删除已完成任务时保留原始终态，不再把 SUCCEEDED 改写为 CANCELLED。
- 为 `command` / `env` 增加大小上限；Kubernetes 拒绝时返回 422 而不是 502。
- `/cluster/summary` 新增 `gpu_metrics`，提供宿主机视角的利用率与显存占用。

### 验证

- 单卡真实训练：`QUEUED → RUNNING → SUCCEEDED`，5 秒完成，checkpoint 落盘并可还原。
- 排队：并发提交两个任务，一运行一排队；取消运行任务后排队任务自动放行并完成。
- `scripts/verify-4090.sh` 全 PASS；8 个 API 端点全 200。
