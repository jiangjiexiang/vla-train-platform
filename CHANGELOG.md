# Changelog

本项目遵循语义化版本。每次大版本更新提交一次 commit 并打 tag，新条目追加到文件顶部。

## [0.9.1] - 2026-11-09

### 新增

- 训练参数模板：镜像可登记 JSON 声明自己的参数（name/flag/type/default/enum/min-max），
  提交页据此动态渲染表单，自动拼装命令行；未登记时回退内置通用模板。
- 三个接口：GET/PUT/DELETE /api/v1/images/{image}/params。
- 新增 examples/params/ 两个真实模板（lerobot-smolvla、openpi-pi05）。

### 修复

- package-lock.json 192 条依赖地址从故障的 npmmirror 改为 npmjs，新增 frontend/.npmrc；
  此前该源返回 500 会导致镜像构建失败。
- 提交页镜像输入改为可选下拉（AutoComplete），来源为默认镜像 + 历史任务用过的镜像。

### 测试

- 后端 286 -> 341，前端 89 -> 112。
- 新增前后端命令行拼装一致性测试（16 个组合逐字节比对）。

## [0.9.0] - 2026-11-09

生产化版本：多用户鉴权、按用户配额、多机训练接口与 Web 可用性边界。接口契约冻结在
`CONTRACTS.md`。既有字段语义不变，v0.8 的历史库与老任务无需迁移。

### 新增

- **鉴权模式** `PLATFORM_AUTH_MODE`（`off` / `token` / `ldap`，默认 `token`，行为与 v0.8 一致）。
  `PLATFORM_AUTH_REQUIRED=false` 等价于 `off`，同时设置时 `off` 优先，旧配置无需改动。
- **LDAP/AD 登录**：`POST /auth/login` 用公司账号换平台会话令牌，`POST /auth/logout` 立即失效，
  `GET /auth/me` 返回当前身份与生效配额。令牌有效期 `PLATFORM_SESSION_TTL_HOURS`（默认 12 小时），
  服务端只存 SHA-256 哈希，LDAP 密码不下发、不落库；会话持久化在历史库中，Web 重启后仍有效。
  LDAP 连接失败返回 503，不吞成 401。
- **角色与任务归属**：`admin` 可查看所有用户任务并管理用户与配额；`user` 只能提交、查看与取消
  自己的任务。任务对象新增 `owner` 字段，老任务回填为空串。
- **按用户配额**：`GET /users`、`PUT /users/{username}/quota`；全局默认由
  `PLATFORM_DEFAULT_MAX_GPUS` / `PLATFORM_DEFAULT_MAX_ACTIVE_JOBS` 控制，单用户覆盖存 SQLite
  `user_quotas` 表。超限提交返回 422（`GPU quota exceeded: …` / `Active job quota exceeded: …`），
  取消任务后额度立即释放；admin 同样受限。
- **多机训练接口预留**：`POST /jobs` 新增 `nodes`、`gpus_per_node`、`labels`；任务对象新增
  `labels`、`gpu_type`、`nodes`；`GET /config` 新增 `max_nodes`。`nodes > 1` 时 `gpus` 由
  `nodes × gpus_per_node` 计算。首版默认 `PLATFORM_MAX_NODES=1`，此时 `nodes > 1` 返回 422
  `multi-node training is not enabled`。
- **label**：可选自由标签，写入 PyTorchJob 的 `vla-platform/user-label-*` 注解，不参与调度。
- **Kueue 多机 profile**（`deploy/kueue/multi-node/`）：用 topology + nodeLabels 表达「每节点 1 卡、
  一个任务跨 N 节点」；配套 `examples/submit-multi-node.sh` 与 `scripts/preflight.sh` 的多机检查项。
- **smoke 多节点模式**：`images/smoke-train/train.py` 从 `MASTER_ADDR` / `MASTER_PORT` / `RANK` /
  `WORLD_SIZE` 读 rendezvous 走 `env://`，并打印每个 rank 的 `HOSTNAME`、GPU 与 NCCL 初始化结果。
- **Web 高可用边界**：`deploy/web/overlays/ha/`；`deploy/web/base/` 增加
  `topologySpreadConstraints` 与 PodDisruptionBudget（不破坏单机部署）。
- **网页**：登录页（`token` 模式填访问令牌，`ldap` 模式填账号密码）、`/users` 用户与配额管理页
  （仅 admin）、提交页新增 `owner` 只读展示、`labels` 键值编辑器、多机选项与 GPU 剩余额度提示、
  侧边栏按角色显示菜单与登出。

### 改进

- 401 的 JSON 形状保持不变（`Bearer token required` / `Invalid bearer token` + `WWW-Authenticate`），
  既有路由从静态令牌平滑迁移到统一用户解析。
- `submissions` 表以幂等 `ALTER TABLE` 增加 `owner` 列，旧库首次启动自动补齐；老任务与历史记录
  缺字段时回填默认值，不抛 `ValidationError`。
- 提交时按用户配额校验，计数口径为「K3s 实时任务 ∪ 历史中未取消的记录」，避免取消后额度残留。

### 修复

- LDAP 登录的失败语义：连接层错误返回 503，凭据错误才返回 401 `Invalid username or password`
  （不区分用户不存在与密码错，避免枚举账号），两者可在客户端明确区分。

### 文档

- 新增 `docs/auth.md`：三种鉴权模式、LDAP 配置示例（OpenLDAP / AD）、配额分配、401 与 503 的
  故障排查。
- 新增 `docs/multi-node.md`：多机前置条件清单、Kueue topology 配置思路、rendezvous 来源、
  NCCL 排查、性能预期与最小验证流程。
- 新增 `docs/high-availability.md`：Web 可用性边界（SQLite 单写者）、备份恢复窗口与 RTO/RPO、
  滚动升级与回滚步骤。
- `docs/api.md`：补齐 `/auth/login`、`/auth/logout`、`/auth/me`、`/users`、
  `PUT /users/{username}/quota` 的字段表与错误码；任务对象新增 `owner` / `labels` / `gpu_type` /
  `nodes`；`POST /jobs` 新增字段与配额、多机错误码；`GET /config` 增加 `max_nodes`；新增 §10.2.1
  说明 v0.9 部分的来源与待确认项。
- `README.md`：新增「鉴权与用户」「多机训练」「高可用」三节，并在快速开始说明 `token` 与 `off`
  模式的区别；故障速查表补充登录 401/503 与配额 422。
- `PLAN.md`：阶段 F 标为「进行中」，把「暂不包含」中已纳入范围的 SSO/LDAP、按用户配额、多机
  标注为 v0.9.0；API 初版契约补充新端点。
- `docs/operations.md`：新增第 6 节「容器运行时选型：buildah + K3s，而不是 Docker」，给出
  Docker 命令与本机等价命令对照表（buildah / `k3s crictl` / `k3s ctr` / `kubectl`）、不安装 Docker 的
  三条理由、手动 `docker run` 绕过 Kueue 的风险，以及需要 Docker 的两种场景与「不推荐生产机执行」的
  安装与回归验证清单；原「安全提示」顺延为第 7 节。
- `docs/troubleshooting.md`：新增第 9 节「`docker: command not found` 不是故障」，指向上述章节与
  等价命令，避免把缺少 Docker 误判为环境残缺。
- `README.md`：「前置条件」与「构建并导入镜像」各补一条说明——本机没有也不需要 Docker，不要为了
  省事安装；只有无 K3s 的开发机用 `docker-compose.yml` 时才需要。

## [0.8.0] - 2026-11-09

### 更名

- 产品对外名称统一为「高性能分布系统」（网页标题、登录页、侧边栏、FastAPI 标题、README、接口文档）。
  命名空间 `vla-platform` / `vla-training`、镜像 `vla-platform:local`、标签与 `PLATFORM_*` 环境变量
  属于既有技术标识，改名会破坏运行中的任务与历史记录，因此保持不变，README 中已注明。

### 新增

- 根目录 `Dockerfile` 改为**多阶段自包含构建**：`node:20-slim` 阶段编译前端，
  `python:3.12-slim` 阶段安装后端依赖，最终镜像只保留运行时。宿主机不再需要安装 Node，
  也不再需要预先生成 `frontend/dist`；`backend/Dockerfile` 已移除。
- `docker-compose.yml`：`docker compose up -d web` 运行服务，`docker compose run --rm test` 跑测试。
- `docker/test.Dockerfile`：预装 Python 3.12 与 Node 20 及两侧依赖的测试环境镜像。
  已在容器内实跑通过：后端 153 passed、前端 45 passed，宿主机无需安装 Python/Node。
- Playwright 端到端测试 7 例（总览、任务列表、提交校验、提交+取消、详情事件、导航），
  复用系统 Chrome，不下载浏览器；`make e2e` 为入口。
- Makefile 增加 `e2e` 目标，`build` 目标简化为纯镜像构建。

### 修复

- 并发取消的幂等性：两个请求同时取消同一任务时，后到的请求不再返回 404，而是返回已有取消记录。
- GPU 指标服务调用加短 TTL 缓存与互斥锁（失败结果同样缓存）：`/api/v1/cluster/summary`
  顺序请求从约 170-280ms 降到 12-15ms，8 并发下 p95 从 543-816ms 降到 159ms，尖峰消失。
- 修复镜像切换到非 root 用户后 SQLite 报 `readonly database`、导致提交与取消返回 500 的问题；
  当前仍以 root 运行，并在 Dockerfile 注明改为非 root 需要同步处理 `/data/history` 属主。

### 文档

- README 增加「名称说明」与镜像内构建说明。

## [0.7.1] - 2026-11-09

### 文档

- `docs/acceptance-4090.md` 记录两项补充验证：Web 重启后任务与历史保留、
  SQLite 在线备份与恢复（`VACUUM INTO` + `integrity_check` + `HistoryStore` 读取）。
- 说明 `cancellations` 表的真实语义：既存用户取消，也存「删除已完成任务」的记录，
  后者用 `status` 字段保留 `SUCCEEDED` / `FAILED`，因此表内行数会多于
  `/api/v1/jobs?status=CANCELLED` 的统计值。

## [0.7.0] - 2026-11-09

### 新增

- `scripts/cleanup-jobs.py`：按状态与结束时间清理历史任务，默认只预览。删除走 REST API，
  因此会进入平台历史并保留真实终态（成功任务删除后仍是 `SUCCEEDED`）；运行中与排队中的任务不会被清理。
  `make cleanup` 为等价入口。
- `make bundle`：把完整仓库导出为 git bundle，用于没有 remote 时的离线备份；已验证可
  `git clone` 出完整历史与全部标签。
- CI 配置：`.github/workflows/ci.yml`（GitHub Actions）与 `.gitlab-ci.yml`，均运行前后端测试与前端生产构建，
  不含任何密钥；集群侧验收 `verify-4090.sh` 不放入 CI（需要真实 GPU）。

### 改进

- 接口层测试改用 FastAPI `TestClient`（新增固定版本 `httpx==0.28.1`），新建 `backend/tests/test_api.py`
  共 59 例，覆盖全部端点的参数校验、错误码与响应结构；移除此前手写的极简 ASGI 驱动。
- 后端测试 100 → 149 例，前端 45 例，合计 194 例。

### 文档

- README 新增「维护」章节（历史任务清理、仓库离线备份）与「持续集成」章节。

## [0.6.0] - 2026-11-09

### 新增

- `GET /api/v1/jobs` 支持 `limit`(1-200) / `offset` / `status` / `q` 四个查询参数，响应增加
  `limit`、`offset` 字段，`total` 为过滤后、分页前的总数。不带任何新参数时行为与之前完全一致。
- 网页任务列表：状态筛选、任务名搜索（400ms 防抖）、表格分页（默认每页 20 条）；
  筛选变化立即重查且不会被 4 秒轮询重置。
- `docs/operations.md`：运维手册，覆盖日常巡检、备份与恢复、升级与回滚、8 类故障处理、
  扩容前检查清单、鉴权开启步骤。
- `docs/api.md`：接口参考，按真实 OpenAPI 定义整理，含各端点参数表、响应示例、错误码与状态机。

### 改进

- `scripts/install-stack.sh` 默认组件版本对齐到本仓库验证过的版本
  （Training Operator v1.9.4、Device Plugin v0.20.1），避免重装时静默降级。

### 测试

- 后端 100 例（新增 17 例分页/过滤，含 cancelled 历史与 422 边界）。
- 前端 45 例（新增 8 例：列表筛选、分页、查询串拼接、轮询 resetKey）。

## [0.5.0] - 2026-11-09

### 新增

- 前端单元测试 37 例（vitest + Testing Library + jsdom），覆盖日期/时长格式化、API 客户端
  错误处理与 401 事件、状态标签渲染、轮询 hook 的生命周期。前端测试脚本 `npm run test`。
- `scripts/test.sh` 升级为前后端统一入口，支持 `backend` / `frontend` 子命令与参数透传。
- `Makefile`：`make test / verify / build / deploy / release / clean` 统一常用操作。
- `scripts/sync_frontend_version.py`：发版时同步 `frontend/package.json` 版本号。

### 改进

- 消除 `GET /api/v1/jobs`、`/cluster/summary`、`/queues` 的 N+1 查询：原先每个任务都要单独
  向 Kubernetes 请求一次 Pod 列表，任务数增长时调用数线性上升。现在改为一次拉取命名空间
  Pod 快照后按 `vla-platform/job-name` 分组复用，接口响应与字段完全不变。
  本机 22 个任务时 `/api/v1/jobs` 约 13-20ms。
- 发版脚本自动同步前端版本号，避免 `VERSION` 与 `package.json` 长期漂移。

### 文档

- README 测试章节重写为前后端两套测试的说明。
- `docs/troubleshooting.md` 补充取消宽限期导致「取消后仍不放行」的解释。

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
