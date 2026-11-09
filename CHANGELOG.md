# Changelog

本项目遵循语义化版本。每次大版本更新提交一次 commit 并打 tag，新条目追加到文件顶部。

## [0.4.0] - 2026-11-09

### 新增

- 提交接口新增 `termination_grace_period_seconds`（1-600，默认 30），决定取消任务时
  训练进程有多少秒退出。网页提交表单、任务详情页与 `vla-train submit
  --termination-grace-period` 均已支持。

### 改进

- 取消运行中任务到排队任务真正开始运行的延迟，从默认的约 **32 秒**降到设置 5 秒宽限时的
  约 **8 秒**（本机实测 8.2 秒）。原因此前是 Pod 走默认 30 秒优雅退出期，期间 GPU 仍被占用。

### 测试

- 单元测试增至 79 例，新增 PyTorchJob 模板渲染（宽限期、标签、checkpoint 注入、只读数据集挂载、
  GPU request/limit 一致、重启策略）与宽限期的端到端字段传递。

## [0.3.0] - 2026-11-09

### 新增

- `vla-train events`：按时间倒序列出 PyTorchJob 与训练 Pod 的事件，默认人类可读，
  `--json` 输出原始响应；省略任务名时逐个列出所有任务的事件。
- CLI 单元测试 16 例（`backend/tests/test_cli.py`）：服务地址规范化、任务名校验、
  环境变量解析、参数解析、事件渲染与异常处理。
- `docs/acceptance-a100.md` 补充两项检查：Kueue 是否启用 `kubeflow.org/pytorchjob`、
  Pod 事件是否可见。

### 变更

- README 与 CONTRACTS 同步 `events` 子命令的用法与契约。

## [0.2.0] - 2026-11-09

### 新增

- 后端单元测试 50 例（`backend/tests/`），用内存假实现替代 Kubernetes 客户端，覆盖：
  提交参数校验与默认值、状态推导、取消语义、事件映射与排序、历史表迁移、GPU 指标解析。
- `scripts/test.sh`：测试入口，首次运行自动创建 `.venv` 并安装 `backend/requirements-dev.txt`。
- `scripts/release.sh` 发版前自动运行单元测试，未通过即中止。

### 修复

- 客户端显式传 `"image": null` 或 `"command": null` 时返回 500：可选字段继承了非空校验器，
  对 `None` 调用 `.strip()` 抛 `AttributeError`。现在按约定回退到平台默认值；
  空字符串仍然拒绝（422）。

## [0.1.1] - 2026-11-09

### 新增

- `scripts/release.sh`：发版脚本，校验版本号、CHANGELOG 小节与工作区状态，跑验收脚本后
  更新 `VERSION`、提交并打 tag。

### 修复

- `scripts/deploy-web.sh` 在 `frontend/dist` 缺失时直接失败于 Dockerfile 的 COPY 步骤，
  报错不直观。改为构建前显式检查并提示先执行 `npm ci && npm run build`。

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
