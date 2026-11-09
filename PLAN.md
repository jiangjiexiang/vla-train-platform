# VLA GPU 训练排队平台实施计划

## 1. 项目目标

建设一套公司内部使用的 VLA 训练排队平台，按以下顺序验证：

1. 单台机器、单张 NVIDIA RTX 4090：验证完整平台链路。
2. 单台机器、8 张 NVIDIA A100：验证 8 卡分布式训练和资源排队。
3. 验证通过后，再评估多节点、高可用和多用户生产化。

首版用户流程：

```text
打开网页（开启鉴权时先登录）→ 提交训练 → 查看排队 → 查看日志 → 取消任务 → 查看结果
```

## 2. 技术决策

- 集群：单节点 K3s。
- GPU：NVIDIA Container Toolkit + NVIDIA Device Plugin。
- 排队：Kueue，负责配额、排队和优先级。
- 训练：Kubeflow Training Operator，使用 PyTorchJob。
- 后端：FastAPI，通过 Kubernetes API 管理任务。
- 前端：React + TypeScript + Ant Design。
- 历史记录：SQLite，只保存提交参数、取消原因和历史，不参与调度。
- 部署：前后端构建为一个容器，通过 K3s Service/NodePort 提供内网访问。
- 登录：支持静态访问令牌，也可通过 `PLATFORM_AUTH_REQUIRED` 关闭；当前 4090 配置暂时免登录，后续再接公司 SSO/LDAP。
- 实时状态：K3s 是事实来源，网页每 3～5 秒轮询刷新。

组件安装固定版本，不使用 latest。

## 3. 首版范围

### 3.1 网页功能

- 总览页：GPU 总数、已分配数、空闲数、节点状态、任务统计，以及 `nvidia-smi` 提供的每卡核心利用率、显存和温度。
- 提交页：任务名、镜像、GPU 数、优先级、命令、环境变量、重启次数。
- 任务列表：状态、资源、排队/运行时长、提交时间、取消和复制任务。
- 任务详情：Kueue 状态、PyTorchJob 状态、Pod 事件、日志和输出目录。
- 队列页：GPU 配额、已用量、运行任务和待放行任务。

提供两个任务模板：

- GPU Smoke Test：CUDA 检查与小型 PyTorch 训练。
- VLA Custom Job：填写自有镜像和训练命令。

### 3.2 管理能力

- 任务提交、列表、详情、日志、取消和按原参数重新提交。
- Kueue 排队、配额和优先级。
- 固定数据集与 checkpoint 目录。
- 任务失败状态与 Pod 事件展示。
- 提交和取消历史持久化。

### 3.3 暂不包含

- Notebook、Web SSH、网页文件上传。
- 多用户权限、SSO/LDAP、按用户配额。
- 多机训练、控制平面高可用。
- 自动调参、模型注册、完整 MLOps。
- 复杂抢占、弹性训练和自动故障恢复。

## 4. 总体架构

```text
浏览器
   │ HTTP
   ▼
┌──────────────────────────────┐
│ React + Ant Design           │
│ FastAPI REST API             │
│ SQLite：提交/取消历史         │
└──────────────┬───────────────┘
               │ Kubernetes API
               ▼
┌──────────────────────────────┐
│ K3s                          │
│ Kueue：排队、配额、优先级      │
│ Training Operator：PyTorchJob │
│ NVIDIA Device Plugin：GPU     │
└──────────────┬───────────────┘
               ▼
       4090 / 单机 8×A100
```

Web 后端只能操作 vla-training 命名空间；节点信息只读。网页不能挂载任意宿主机目录，不开放特权容器和宿主机 Docker Socket。

## 5. 项目结构

```text
vla-train-platform/
├── PLAN.md
├── README.md
├── backend/
│   ├── app/
│   │   ├── api/              # 任务、队列、日志、GPU API
│   │   ├── services/         # K8s/Kueue 状态聚合
│   │   ├── templates/        # PyTorchJob 渲染
│   │   └── history.py        # SQLite 历史记录
│   └── Dockerfile
├── frontend/
│   ├── src/pages/
│   │   ├── Dashboard/
│   │   ├── SubmitJob/
│   │   ├── JobList/
│   │   ├── JobDetail/
│   │   └── Queues/
│   └── package.json
├── cli/
│   └── vla-train             # 与 Web 共用任务生成逻辑
├── deploy/
│   ├── base/                 # Namespace、RBAC、存储
│   ├── kueue/
│   │   ├── 4090/             # 配额 1 GPU
│   │   └── a100/             # 配额 8 GPU
│   └── web/                  # Web Deployment、Service
├── images/
│   └── smoke-train/
└── scripts/
    ├── preflight.sh
    ├── install-k3s-gpu.sh
    ├── install-stack.sh
    ├── deploy-web.sh
    ├── verify-4090.sh
    └── verify-a100.sh
```

## 6. 实施阶段

### 阶段 A：接口和项目骨架

先冻结以下契约，再并行开发：

- REST API 路径、请求和响应结构。
- 任务状态：QUEUED、RUNNING、SUCCEEDED、FAILED、CANCELLED。
- PyTorchJob 标签、注解、命名和归属规则。
- 数据集/checkpoint 宿主机路径与容器挂载路径。
- Namespace、ServiceAccount、队列和 PriorityClass 名称。
- 4090 与 A100 配置覆盖方式。

### 阶段 B：并行开发

并行完成集群部署、后端、前端、训练模板/CLI、验证脚本五部分，具体分工见第 9 节。

### 阶段 C：集成验证

- 前端生产构建通过。
- 后端 API 和任务渲染验证通过。
- Shell 脚本语法检查通过。
- 前后端单容器构建通过。
- 所有 Manifest 可以做客户端校验。
- 无集群环境只能验证 dry-run 和模拟响应，不能声称 GPU 端到端通过。

### 阶段 D：4090 实测

集群验收：

1. 容器内 nvidia-smi 正常。
2. K3s 识别 nvidia.com/gpu: 1。
3. Kueue GPU 配额为 1。
4. 单卡 PyTorchJob 成功完成。

网页验收：

| 场景 | 通过标准 |
|---|---|
| 登录 | 免登录配置可直接访问；开启鉴权后无令牌不能提交或取消任务 |
| 总览 | 正确显示 1 张 GPU |
| 提交 | 不写 YAML，通过网页启动训练 |
| 排队 | 连续提交两个任务，一个运行、一个排队 |
| 放行 | 第一个完成后，第二个自动运行 |
| 日志 | 网页持续看到训练日志 |
| 取消 | 取消后释放 GPU，排队任务自动启动 |
| 失败 | 失败原因和 Pod 事件可见，不无限重启 |
| Web 重启 | 实时任务不丢失，历史提交仍可查看 |
| Checkpoint | 文件写入固定目录，任务结束后保留 |

4090 只验证小型训练链路；VLA 代码使用小数据或 LoRA 配置单独验证，不把完整模型训练作为验收条件。

### 阶段 E：单机 8×A100 实测

复用同一套项目，只切换 A100 配置：

- Kueue GPU 配额从 1 改为 8。
- 网页开放 1/2/4/8 GPU 选项。
- Smoke Job 使用 torchrun --nproc_per_node=8。

验收：

1. 网页显示 8 张 GPU。
2. 8 卡 DDP 正常，无 NCCL 错误。
3. 两个 8 卡任务同时提交，一个运行、一个排队。
4. 取消运行任务后，8 张卡全部释放。
5. 训练重启后能从 checkpoint 恢复。
6. 自有 VLA 代码完成一次小规模 8 卡训练。

## 7. API 初版契约

- POST /api/v1/jobs：提交任务。
- GET /api/v1/jobs：任务列表。
- GET /api/v1/jobs/{name}：任务详情。
- GET /api/v1/jobs/{name}/logs：日志查询。
- DELETE /api/v1/jobs/{name}：取消任务。
- POST /api/v1/jobs/{name}/clone：按原参数重新提交。
- GET /api/v1/cluster/summary：GPU 与任务统计。
- GET /api/v1/queues：Kueue 队列和工作负载。
- GET /api/v1/config：允许的 GPU 数、默认镜像、路径和优先级。

所有变更操作要求访问令牌；日志接口限制输出大小，避免拖垮网页。

## 8. 交付物

- 完整前后端源代码和单容器 Dockerfile。
- K3s、GPU、Kueue、Training Operator 部署配置与脚本。
- 4090/A100 两套配置。
- Smoke 训练镜像和示例 VLA 自定义任务模板。
- vla-train CLI。
- README：安装、使用、卸载、故障排查和验收步骤。
- 实际执行过的验证结果；未验证项明确标注。

## 9. 最快 Agent 并行方案

推荐 5 个开发 Agent 并行，主 Agent 负责契约、集成和最终验收。总人数为 5 个开发 Agent + 1 个主 Agent。

### Agent 1：集群与部署

负责：K3s/GPU 安装脚本、Training Operator、Kueue、Namespace、RBAC、4090/A100 配置。

不修改：后端、前端业务代码。

### Agent 2：FastAPI 后端

负责：REST API、Kubernetes API、状态聚合、日志、SQLite 历史和鉴权。

不修改：前端、集群安装脚本。

### Agent 3：React 前端

负责：五个页面、表单校验、轮询、日志展示、操作确认和生产构建。

不修改：后端和部署文件。

### Agent 4：训练资产与 CLI

负责：PyTorchJob 模板、Smoke 镜像、8 卡 DDP 示例、CLI 和 checkpoint 示例。

不修改：Web 页面和集群安装脚本。

### Agent 5：验证与文档

负责：验收脚本、故障排查、README、安装/卸载步骤和结果记录模板。

不修改：其他 Agent 的核心实现；发现问题提交给主 Agent 统一处理。

### 主 Agent：集成与验收

负责：

1. 先建立目录骨架和 API/命名契约。
2. 启动并协调 5 个开发 Agent。
3. 集成代码，解决跨模块问题。
4. 执行构建、静态检查和 dry-run。
5. 驱动 4090 与 A100 实测，修复端到端问题。
6. 最终确认交付物与文档一致。

### 为什么不使用更多 Agent

超过 5 个开发 Agent 后，后端 API、前端字段、K8s 标签和部署配置很容易产生冲突；集成和返工成本会高于并行收益。部署实测阶段也不适合多个 Agent 同时修改同一台测试机。

### 执行顺序

```text
主 Agent 冻结契约
        │
        ▼
5 个开发 Agent 并行
        │
        ▼
主 Agent 集成、构建、dry-run
        │
        ▼
4090 单环境串行实测
        │
        ▼
修复后切换 8×A100 验收
```

目标是尽快形成可部署版本，但实际总时间主要取决于远端驱动、镜像下载、K3s/NVIDIA 配置和真实 VLA 训练问题，不以代码生成时间作为完成标准。
