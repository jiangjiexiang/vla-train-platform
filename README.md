# 高性能分布系统

高性能分布系统：面向单机 NVIDIA GPU 的内网训练排队与调度服务。用户通过网页或 CLI 提交 PyTorchJob，由 Kueue 按 GPU 配额排队，Kubeflow Training Operator 执行训练；K3s 中的实时状态是事实来源，SQLite 只保存提交和取消历史。

> **名称说明**：产品对外名称为「高性能分布系统」。代码与集群中的 `vla-platform` / `vla-training`
> 命名空间、`vla-platform:local` 镜像名、`app.kubernetes.io/name=vla-train-platform` 标签与
> `PLATFORM_*` 环境变量属于既有技术标识，改名会破坏正在运行的任务与历史记录，因此保持不变。

首版支持两种互斥的单机配置：

- RTX 4090：1 张 GPU，任务只能申请 1 卡。
- 8×A100：8 张 GPU，任务可申请 1、2、4 或 8 卡；8 卡 smoke 使用 `torchrun`。

> Smoke 镜像只验证 CUDA、PyTorch、排队、日志和 checkpoint 链路，不是真实 VLA 模型训练。真实 GPU 验收必须在目标机器上执行，不能以构建成功或 dry-run 代替。

## 1. 架构与边界

```text
浏览器 / cli/vla-train
        │ HTTP（Authorization: Bearer <token>）
        ▼
vla-platform：FastAPI + React 单容器，SQLite 历史
        │ Kubernetes API
        ▼
vla-training：Kueue → PyTorchJob → 单 Master Pod
        │
        ▼
NVIDIA Device Plugin → 单机 1×4090 或 8×A100
```

固定资源与路径：

| 项目 | 值 |
|---|---|
| Web Namespace | `vla-platform` |
| 训练 Namespace | `vla-training` |
| LocalQueue / ClusterQueue / ResourceFlavor | `default` / `vla-cluster-queue` / `gpu` |
| 优先级 | `low`、`normal`、`high` |
| Web NodePort | `30080` |
| 数据集 | 宿主机 `/data/vla-platform/datasets` → 容器 `/datasets`（只读） |
| Checkpoint | 宿主机 `/data/vla-platform/checkpoints` → 容器 `/checkpoints`（可写） |
| 单任务 checkpoint | 容器 `/checkpoints/<job-name>` |
| Web 历史库 | 容器内 `/data/history/platform.db`，由数据卷持久化 |

任务名只能包含小写字母、数字和连字符，最长 50 字符，且不能以连字符开头或结尾。首版不开放任意宿主机路径、特权容器、宿主机 Docker Socket、Notebook、Web SSH。

v0.9.0 起支持三种鉴权模式（`off` / `token` / `ldap`，默认 `token`）、按用户 GPU/任务数配额，以及多机训练的接口预留（`nodes` / `gpus_per_node`，默认关闭）。见第 3.4、3.5、3.6 节。

## 2. 前置条件

在目标 GPU 机器上准备：

- 支持的操作系统、容器镜像构建工具（Docker，或本机无 Docker 时用 buildah，见 3.1）、以及与驱动匹配的 NVIDIA GPU 驱动；宿主机 `nvidia-smi` 应正常。
- 4090 机器实际安装 1 张可用 GPU，或 A100 机器实际安装 8 张可用 GPU。
- 可访问安装所需软件包和镜像的网络；磁盘空间足以保存镜像、数据集和 checkpoint。
- 当前用户可构建镜像并导入 K3s containerd（Docker 或 buildah），且能通过 `kubectl` 管理目标 K3s。安装脚本需要提权时按脚本提示执行。
- 数据集已放入 `/data/vla-platform/datasets`，checkpoint 父目录为 `/data/vla-platform/checkpoints`。

平台安装脚本用于专用目标机器，不会替代运维人员判断现有 Docker/Kubernetes 环境是否适合改动。不要在未确认影响的生产集群上直接安装或卸载。

> **本机没有 Docker，也不需要 Docker。** 目标机器由 K3s 自带的 containerd 负责运行容器，构建用 buildah、运维用 `k3s ctr` / `kubectl` / `crictl` 即可，Docker 命令与本机等价命令的对照见 `docs/operations.md` 第 7 节。**不要为了跑测试或图省事安装 Docker**：该机器是多用户共享的 GPU 工作站，Docker 与 K3s containerd 同时管理 iptables/cgroup 可能干扰 NodePort `30080` 和 Pod 网络。只有「在没有 K3s 的开发机上用 `docker-compose.yml` 跑平台」或「独立的专用机器」才需要 Docker。

## 3. 部署顺序

在仓库根目录执行。4090 使用 `PROFILE=4090`；A100 机器从第一步起就统一改成 `PROFILE=a100`：

```bash
cd /path/to/vla-train-platform
PROFILE=4090 bash scripts/preflight.sh
PROFILE=4090 bash scripts/install-k3s-gpu.sh
PROFILE=4090 bash scripts/install-stack.sh
```

这些脚本分别负责前置检查、K3s/NVIDIA 容器运行环境以及 Training Operator/Kueue 等基础组件。`install-stack.sh` 同时应用所选 Kueue 配额；已有 Device Plugin 或 Kueue 控制器会被复用，不重复安装或替换其 Deployment。训练 Pod 显式使用 `runtimeClassName: nvidia`，无需修改集群全局默认 Runtime。平台使用 Kueue `v1beta2` API，默认组件版本为 K3s v1.36.6、Kueue v0.20.5。Kueue 必须启用 `kubeflow.org/pytorchjob` 集成，chart 默认值与官方 release `manifests.yaml` 只启用 `pod`，会让排队粒度退化为 Pod 级；启用方式见 `deploy/kueue/base/kueue-values.yaml`，判断方法见第 10 节。组件安装后，构建并导入 Web 与 smoke 镜像，再部署 Web；不要在镜像尚未导入时提交任务。

### 3.1 构建并导入镜像

Web 镜像是**自包含的多阶段构建**（根目录 `Dockerfile`）：

1. `node:20-slim` 阶段执行 `npm ci && npm run build` 生成 SPA；
2. `python:3.12-slim` 阶段安装后端依赖；
3. 最终镜像只保留运行时的 Python、应用代码与静态资源，并以非 root 用户 `vla` 运行。

因此构建只需要 Docker 或 buildah，不需要在宿主机装 Node，也不需要提前准备 `frontend/dist`。

> **本机没有 Docker，也不需要 Docker**：目标机器是 buildah + K3s containerd，直接看本节末尾「目标机器没有 Docker 时（buildah 路径）」。下面用 `docker build` 的写法适用于有 Docker 的开发机；只有「要在没有 K3s 的开发机上用 `docker-compose.yml` 跑平台」时才需要 Docker，在这台生产/共享机器上装 Docker 没有必要且有网络风险。

Web 默认标签为 `vla-platform:local`，与 `deploy/web/base/deployment.yaml` 一致。手动构建和导入：

```bash
cd /path/to/vla-train-platform
WEB_IMAGE=vla-platform:local

docker build -t "$WEB_IMAGE" .
docker build -t vla-smoke:latest images/smoke-train

docker save "$WEB_IMAGE" -o /tmp/vla-platform-web.tar
docker save vla-smoke:latest -o /tmp/vla-smoke.tar
sudo k3s ctr images import /tmp/vla-platform-web.tar
sudo k3s ctr images import /tmp/vla-smoke.tar
sudo k3s ctr images ls
rm -f /tmp/vla-platform-web.tar /tmp/vla-smoke.tar
```

也可执行 `bash examples/build-import-smoke.sh` 构建并导入 smoke。`deploy-web.sh` 在 Web 镜像不存在时默认自动构建/导入，设置 `BUILD_IMAGE=1` 可强制重建；已有镜像归档时可设置 `WEB_IMAGE_TAR`，smoke 归档可设置 `SMOKE_IMAGE_TAR`。

使用本地导入镜像时，对应 Deployment/PyTorchJob 应采用 `IfNotPresent` 拉取策略；`latest` 配合 `Always` 会尝试访问外部镜像仓库。Smoke 镜像名固定为 `vla-smoke:latest`，训练入口为 `/app/train.py`。

**目标机器没有 Docker 时（buildah 路径）**

本机实测没有 `docker`，`scripts/deploy-web.sh` 会自动回退到 buildah（1.23.1）。
镜像本身是多阶段构建：前端在 `node:20-slim` 阶段编译，后端依赖在 `python:3.12-slim` 阶段安装，
**宿主机不需要 Node，也不需要预先生成 `frontend/dist`**。

```bash
cd /path/to/vla-train-platform

buildah bud -t vla-platform:local .
buildah bud -t vla-smoke:latest images/smoke-train

buildah push vla-platform:local docker-archive:/tmp/vla-platform-web.tar:vla-platform:local
buildah push vla-smoke:latest docker-archive:/tmp/vla-smoke.tar:vla-smoke:latest

sudo k3s ctr -n k8s.io images import /tmp/vla-platform-web.tar
sudo k3s ctr -n k8s.io images import /tmp/vla-smoke.tar
sudo k3s ctr -n k8s.io images ls
rm -f /tmp/vla-platform-web.tar /tmp/vla-smoke.tar

BUILD_IMAGE=0 PROFILE=4090 bash scripts/deploy-web.sh
```

buildah 的存储与 K3s containerd 相互独立，所以 `buildah push ... docker-archive:` 加 `k3s ctr images import` 这一步不能省。buildah 拉取 `docker.io` 基础镜像走本机 `/etc/containers/registries.conf.d/` 中配置的 `docker.m.daocloud.io` 镜像加速；`python:3.12-slim` 仍需要能访问该加速源。镜像已预先导入时必须带 `BUILD_IMAGE=0`（或改用 `WEB_IMAGE_TAR=`/`SMOKE_IMAGE_TAR=` 指定归档），否则 `deploy-web.sh` 在 `BUILD_IMAGE=auto` 且镜像缺失时会尝试调用不存在的 `docker`。

### 3.2 选择或切换 4090/A100 配置

首次安装由 `PROFILE` 同时选择 Kueue 配额和 Web overlay。已有平台上切换时，先取消或等待已有任务结束，确保没有工作负载仍按旧配额运行，再执行：

**4090：**

```bash
kubectl apply -k deploy/kueue/4090/
PROFILE=4090 bash scripts/deploy-web.sh
```

**单机 8×A100：**

```bash
kubectl apply -k deploy/kueue/a100/
PROFILE=a100 bash scripts/deploy-web.sh
```

4090 overlay 设置 `PLATFORM_ALLOWED_GPUS=1`，A100 overlay 设置 `1,2,4,8`。配额和该变量必须一起切换：前者决定集群能放行多少卡，后者限制表单/API 允许的单任务卡数。已存在 `vla-web-auth` Secret 时，以上命令复用原令牌；只有显式提供新令牌才会轮换并重启 Web。

### 3.3 配置鉴权并启动 Web

当前 4090 overlay 设置 `PLATFORM_AUTH_REQUIRED=false`，等价于 `off` 模式：网页和 API 暂时免登录，仅限可信内网使用；A100 overlay 默认仍要求令牌（`token` 模式）。v0.9 的三种模式和 LDAP 配置见 3.4。部署脚本首次运行仍会创建 `vla-web-auth` Secret，便于以后恢复鉴权。生成高随机性的静态令牌并通过环境变量交给部署脚本，不要把真实令牌写入仓库、镜像或脚本：

```bash
export PLATFORM_API_TOKEN="$(openssl rand -hex 32)"
PROFILE=4090 bash scripts/deploy-web.sh
```

A100 使用 `PROFILE=a100`。也可用权限受控的单行令牌文件并设置 `PLATFORM_API_TOKEN_FILE`，两种令牌输入方式不要同时使用。对已有 Web 再次提供令牌会更新 Secret 并重启 Deployment，客户端需要同步换用新令牌。

要在 4090 上恢复登录，将 `deploy/web/overlays/4090/deployment.yaml` 中 `PLATFORM_AUTH_REQUIRED` 的值改为 `"true"`，保留有效的 `vla-web-auth` Secret，再执行 `PROFILE=4090 bash scripts/deploy-web.sh`。

Web 后端还支持契约中定义的 `PLATFORM_NAMESPACE`、`PLATFORM_QUEUE`、`PLATFORM_DATASET_HOST_PATH`、`PLATFORM_CHECKPOINT_HOST_PATH`、`PLATFORM_DB_PATH`，以及 v0.9 的 `PLATFORM_AUTH_MODE`、`PLATFORM_SESSION_TTL_HOURS`、`PLATFORM_LDAP_*`、`PLATFORM_DEFAULT_MAX_GPUS`、`PLATFORM_DEFAULT_MAX_ACTIVE_JOBS`、`PLATFORM_MAX_NODES` 等配置；首版通常保留默认值。`token` 模式下所有 API（健康检查与登录接口除外）都要求静态令牌，请只通过受控方式分发。

检查服务：

```bash
kubectl -n vla-platform get deployment,pod,service
curl -fsS http://127.0.0.1:30080/api/v1/health
```

`off` 模式预期健康响应为 `{"status":"ok","auth_required":false}`；`token` / `ldap` 模式为 `true`。

主要 HTTP 接口（前缀 `/api/v1`；开启鉴权时除 `/health` 外都要带 `Authorization: Bearer <token>`）：

- `GET /health`、`GET /config`
- `GET /jobs`、`POST /jobs`
- `GET /jobs/{name}`、`DELETE /jobs/{name}`、`POST /jobs/{name}/clone`
- `GET /jobs/{name}/logs?tail_lines=500`
- `GET /jobs/{name}/events`：返回该任务的 PyTorchJob 与训练 Pod 事件，按 `last_seen` 倒序；任务不存在返回 404，已取消且对象已删除时返回空 `items`。镜像拉取失败、Pod 调度失败等问题先用它定位。
- `GET /queues`、`GET /cluster/summary`
- `POST /auth/login`、`POST /auth/logout`、`GET /auth/me`（v0.9 新增）
- `GET /users`、`PUT /users/{name}/quota`（v0.9 新增，仅 admin）

完整字段与语义见 `docs/api.md`（HTTP 细节）与 `CONTRACTS.md`（跨模块契约）。

### 3.4 鉴权与用户

v0.9.0 起，登录方式由 `PLATFORM_AUTH_MODE` 决定，默认 `token`（与 v0.8 完全一致）：

| 模式 | 怎么设置 | 登录方式 | 当前用户 |
|---|---|---|---|
| `off` | `PLATFORM_AUTH_MODE=off`，或沿用 `PLATFORM_AUTH_REQUIRED=false` | 不需要，网页直接进平台 | `anonymous`（admin） |
| `token` | 默认 | 网页 `/login` 填「访问令牌」= `PLATFORM_API_TOKEN` | `token`（admin） |
| `ldap` | `PLATFORM_AUTH_MODE=ldap` | 网页填公司 LDAP/AD 用户名和密码 | LDAP `uid`，按组映射角色 |

**`token` 与 `off` 的区别**（快速开始最容易踩的两个点）：

- `token`：除 `/health`、`/auth/login` 外的接口都要带 `Authorization: Bearer <token>`，
  令牌就是 `PLATFORM_API_TOKEN`；浏览器登录页输入它，CLI 用 `--token` 传入。没有令牌，
  网页拿不到数据，CLI 报 401。
- `off`：所有接口都不校验令牌，请求带了也不会报错；网页跳过登录页，所有人都是
  `anonymous` 且是 admin，平台不区分提交者、没有审计。**只用于可信内网和短期验收。**

切换模式后 `GET /api/v1/health` 的 `auth_required` 会同步变化（`off` → `false`，
`token`/`ldap` → `true`），网页靠它决定是否显示登录页。

**LDAP 模式最小配置**（完整示例、OpenLDAP/AD 差异见 `docs/auth.md`）：

```yaml
env:
  - name: PLATFORM_AUTH_MODE
    value: ldap
  - name: PLATFORM_LDAP_URL
    value: "ldap://ldap.corp.example.com:389"
  - name: PLATFORM_LDAP_BASE_DN
    value: dc=corp,dc=example,dc=com
  - name: PLATFORM_LDAP_USER_TEMPLATE
    value: uid={username},ou=people,dc=corp,dc=example,dc=com
  - name: PLATFORM_LDAP_GROUP_DN_ADMIN
    value: cn=vla-admin,ou=groups,dc=corp,dc=example,dc=com
  - name: PLATFORM_SESSION_TTL_HOURS
    value: "12"
```

登录成功后平台签发会话令牌（有效期 `PLATFORM_SESSION_TTL_HOURS`，默认 12 小时），服务端只
保存其 SHA-256 哈希，**不下发也不保存 LDAP 密码**，Web 重启后会话仍然有效。

**按用户配额**：用 `PLATFORM_DEFAULT_MAX_GPUS` / `PLATFORM_DEFAULT_MAX_ACTIVE_JOBS` 设置全局
默认（留空 = 不限），再用 `/users` 页面或 `PUT /users/{username}/quota` 给单个用户覆盖。
超限的提交返回 422，例如 `GPU quota exceeded: 4 used of 4 allowed`。admin 同样受配额约束。

```bash
export TOKEN=<admin 令牌或会话令牌>
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:30080/api/v1/auth/me
curl -fsS -X PUT http://127.0.0.1:30080/api/v1/users/alice/quota \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"max_gpus": 4, "max_active_jobs": 2}'
```

排障记住一条：**登录 401 是凭据不对，503 是平台连不上 LDAP**，后者不要反复重试密码。
详见 `docs/auth.md`。

### 3.5 多机训练

多机训练在 v0.9.0 只有接口与校验，**默认关闭**：`PLATFORM_MAX_NODES` 默认 `1`，此时提交
`nodes > 1` 会返回 422 `multi-node training is not enabled`。这不是客户端开关，需要先改服务端
配置并应用多机 Kueue profile（`deploy/kueue/multi-node/`），前置条件清单见
`docs/multi-node.md`。

打开后提交方式：

```bash
export BASE=http://127.0.0.1:30080/api/v1
curl -fsS -X POST "$BASE/jobs" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "name": "mn-smoke-1",
    "nodes": 2,
    "gpus_per_node": 1,
    "command": "torchrun --nnodes=2 --nproc_per_node=1 --node_rank=$RANK /app/train.py --distributed --steps 20"
  }'
```

- `nodes > 1` 时 `gpus` 可省略，服务端按 `nodes × gpus_per_node` 计算；显式传入必须一致。
- `gpus_per_node` 必须属于 `PLATFORM_ALLOWED_GPUS`；2×1 卡占用 2 卡配额。
- 训练脚本从 `MASTER_ADDR` / `MASTER_PORT` / `RANK` / `WORLD_SIZE` 读 rendezvous，走 `env://`；
  smoke 的 `--distributed` 会打印每个 rank 的 `HOSTNAME`、GPU 和 NCCL 初始化结果。
- Kueue 侧用 topology + nodeLabels 表达「每节点 1 卡、一个任务跨 N 节点」，避免把大任务放行到
  装不下的单节点。

回滚到单机：删掉 Web Deployment 的 `PLATFORM_MAX_NODES` 并 `rollout restart`，再 apply 回
`deploy/kueue/4090/` 或 `deploy/kueue/a100/`。NCCL 排查与性能预期见 `docs/multi-node.md`。

### 3.6 高可用

Web 是**单副本 + `Recreate`**，原因是历史库是本地 SQLite 文件：同一时刻只允许一个写入者。
因此：

- 训练不受影响：Web 挂掉或升级期间，已在跑的 PyTorchJob 继续运行，恢复后状态、日志、事件照常可查。
- 升级有明确停服窗口：`Recreate` 先删旧 Pod 再建新 Pod，期间网页和 API 不可用（通常几十秒），
  **这是用可用性换数据安全，不要改成 `RollingUpdate`**。
- 不能简单扩副本：PV 是 `hostPath` + `ReadWriteOnce`，两个 Pod 同时写一个 SQLite 文件会损坏；
  多副本的前提是把历史写入迁到共享存储上的外部数据库，契约尚未覆盖。
- 恢复靠备份：历史库按 `docs/operations.md` 2.2 在线备份，恢复时先停 Web、删 `-wal`/`-shm`/
  `-journal`、`integrity_check` 通过后再启动。

单机部署下可承诺的是「分钟级恢复 + 最近一次备份前的历史」，不是「永不断线」。升级、回滚步骤
和 RTO/RPO 口径见 `docs/high-availability.md`。
## 4. 网页使用

在内网浏览器打开：

```text
http://<目标机器IP>:30080
```

如当前机器防火墙或云安全组限制端口，只向内网可信来源开放 TCP `30080`。当前 4090 配置会直接进入平台；`token` 模式需在 `/login` 输入访问令牌，`ldap` 模式输入公司账号和密码（见 3.4）。主要页面：

- `/dashboard`：GPU 总量、已分配量、空闲量、节点和任务统计，以及每张卡的核心利用率、显存占用和温度。
- `/submit`：填写任务名、镜像、GPU 数、优先级、命令、环境变量和重启次数。
- `/jobs`：查看排队/运行状态、取消任务或复制任务。
- `/jobs/<job-name>`：查看 Kueue、PyTorchJob、Pod 事件、日志和输出目录。
- `/queues`：查看配额、已用量、已放行和待放行任务。
- `/users`：用户列表、角色、活跃任务、GPU 占用与配额（v0.9 新增，仅 admin 可见）。

网页每 3～5 秒轮询。“GPU 分配情况”来自 Kubernetes，表示训练任务获得多少张卡；“GPU 实时使用情况”来自 `nvidia-smi`，表示核心利用率、显存和温度，两者含义不同。`vla-gpu-metrics` DaemonSet 只读采集指标，不申请训练 GPU 配额。排队时还没有训练 Pod，因此日志为空是正常现象；先查看任务详情和队列页，不要把“无日志”直接判断为日志系统故障。

## 5. CLI

CLI 只调用 REST API，不直接操作 Kubernetes。配置文件以 `0600` 权限保存在当前用户配置目录。

```bash
cd /path/to/vla-train-platform
cli/vla-train config --server http://127.0.0.1:30080 --token "$PLATFORM_API_TOKEN"
cli/vla-train submit \
  --name smoke001 \
  --gpus 1 \
  --image vla-smoke:latest \
  --priority normal \
  --command 'python /app/train.py --steps 100'
cli/vla-train list
cli/vla-train status
cli/vla-train logs smoke001
cli/vla-train events smoke001
```

`status` 查看任务列表/状态，`logs` 读取日志。queued 任务可能返回空日志；运行后应看到 CUDA/GPU、训练进度和完成信息。

### 取消宽限期

`--termination-grace-period`（1-600，默认 30）决定取消任务时训练进程还有多少秒可以退出。
宽限期越大越不容易丢未保存的 checkpoint，但 GPU 释放越慢，排队任务等待越久：

```bash
cli/vla-train submit --name short001 --gpus 1 --command 'python /app/train.py' --termination-grace-period 5
```

本机实测：宽限期 5 秒时，取消运行中任务后排队任务约 8 秒开始运行；用默认 30 秒时约 32 秒。
网页提交表单里有同名字段，任务详情页会显示实际取值。

`events` 按时间倒序列出 PyTorchJob 与训练 Pod 的事件，用于排查「已放行但没跑起来」这类问题：

```bash
cli/vla-train events smoke001          # 人类可读
cli/vla-train events smoke001 --json   # 原始 JSON，便于脚本处理
cli/vla-train events                   # 逐个列出所有任务的事件
```

典型输出会直接给出镜像拉取失败、被 Kueue 放行、Pod 创建等关键节点。

### 取消与 clone

```bash
cli/vla-train cancel smoke001
cli/vla-train clone smoke001 --name smoke002
```

取消会记录取消历史并删除对应 PyTorchJob，从而释放 GPU；重复取消同一任务不会报错。clone 使用原提交参数，但必须提供一个未使用的新任务名。网页中的 clone 会打开已填充原参数的提交表单。

## 6. 提交两个任务验证排队

在空闲集群上使用足够长的 smoke 步骤，避免第一个任务在第二个提交前就结束。以下名称如已存在，请换成新的合法名称。

### 4090

```bash
cli/vla-train submit --name queue4090a --gpus 1 --image vla-smoke:latest \
  --priority normal --command 'python /app/train.py --steps 1000'
cli/vla-train submit --name queue4090b --gpus 1 --image vla-smoke:latest \
  --priority normal --command 'python /app/train.py --steps 1000'
cli/vla-train list
cli/vla-train status
```

预期一个为 `RUNNING`、另一个为 `QUEUED`。取消运行任务：

```bash
cli/vla-train cancel queue4090a
cli/vla-train logs queue4090b
```

第二个任务应被自动放行并运行，说明取消释放了 1 卡配额。

### 单机 8×A100

要验证 8 卡排队，两个任务都必须申请 8 卡；两个 1 卡任务可以在 8 卡配额内同时运行，不能证明排队。

```bash
cli/vla-train submit --name queuea100a --gpus 8 --image vla-smoke:latest \
  --priority normal \
  --command 'torchrun --standalone --nproc_per_node=8 /app/train.py --distributed --steps 1000'
cli/vla-train submit --name queuea100b --gpus 8 --image vla-smoke:latest \
  --priority normal \
  --command 'torchrun --standalone --nproc_per_node=8 /app/train.py --distributed --steps 1000'
cli/vla-train list
cli/vla-train cancel queuea100a
```

预期同样是一个运行、一个排队，取消后第二个任务自动运行。8 卡日志还应确认 8 个进程均启动且无 NCCL 错误。

也可以显式使用验收脚本的可选端到端模式；默认模式只检查，不提交训练：

```bash
PLATFORM_API_TOKEN="$PLATFORM_API_TOKEN" bash scripts/verify-4090.sh --e2e
PLATFORM_API_TOKEN="$PLATFORM_API_TOKEN" bash scripts/verify-a100.sh --e2e
PLATFORM_API_TOKEN="$PLATFORM_API_TOKEN" bash scripts/verify-a100.sh --e2e --8gpu-smoke
```

4090 的 `--e2e` 和 A100 的 `--8gpu-smoke` 会真实占用 GPU。脚本会验证排队与取消，成功任务保留供查看日志/checkpoint；失败时尝试取消本次创建的任务。普通 A100 `--e2e` 只运行一个短 smoke，不验证 8 卡排队。

## 7. Checkpoint 与历史

训练容器内写入：

```text
/checkpoints/<job-name>
```

对应宿主机：

```text
/data/vla-platform/checkpoints/<job-name>
```

任务结束后检查文件仍存在：

```bash
ls -la /data/vla-platform/checkpoints/smoke001
```

Smoke 程序使用 `CHECKPOINT_DIR`；平台按任务提供该目录。自有训练代码应把 checkpoint 写入该环境变量指定的位置。API/CLI 不允许把任意宿主机目录挂载进容器。删除 Kubernetes 任务不等于删除宿主机 checkpoint；删除宿主机目录则不可恢复，请先备份。

Web 重启不应影响 K3s 中的实时任务。SQLite 历史依赖 `/data/history/platform.db` 所在数据卷持久化，实时调度状态绝不从 SQLite 恢复。

## 8. 验收

默认验收不启动训练：

```bash
bash scripts/verify-4090.sh
bash scripts/verify-a100.sh
```

分别在与硬件匹配的环境执行，不要在 4090 机器上期待 A100 检查通过。脚本优先使用能访问集群的 `kubectl`，否则尝试 `k3s kubectl`（需要时使用 `sudo`）；也可用 `KUBECTL_BIN` 指定其他 kubectl 可执行文件。可用 `WEB_URL` 指定 API 地址，默认 `http://127.0.0.1:30080`；可用 `E2E_TIMEOUT` 调整可选 smoke 的等待秒数。

完整人工清单见：

- `docs/acceptance-4090.md`
- `docs/acceptance-a100.md`
- `docs/troubleshooting.md`

功能与运维专题文档：

- `docs/api.md`：HTTP 接口字段表与错误码（含 v0.9 的鉴权与用户端点）
- `docs/auth.md`：三种鉴权模式、LDAP 接入、按用户配额、401/503 排查
- `docs/multi-node.md`：多机前置条件、Kueue topology、NCCL 排查、性能预期
- `docs/high-availability.md`：Web 可用性边界、备份恢复窗口、升级与回滚
- `docs/operations.md`：日常巡检、备份、升级、监控

只有真实提交 CUDA 训练并检查输出后，才能勾选 GPU/NCCL/checkpoint 实测项。

## 9. 卸载

首版没有一个会同时删除 K3s、NVIDIA、Kueue 和平台数据的无差别卸载脚本。按以下顺序手动操作，并在每条删除命令前确认目标机器和命名空间。

1. 在网页或 CLI 取消不再需要的活动任务，备份历史与 checkpoint。
2. 只删除平台和训练命名空间：

   ```bash
   kubectl delete namespace vla-platform vla-training
   ```

   这会删除 Web、训练任务及命名空间内资源。`deploy/base/storage.yaml` 将历史 PV `vla-web-history` 的回收策略设为 `Retain`，删除 Namespace 不会清除宿主机历史文件。备份历史后，如确认不再保留 PV，可另行执行 `kubectl delete persistentvolume vla-web-history`；这仍不会清空其 hostPath。

3. 如果这是专用集群，且确认不再使用本项目队列，可删除集群级队列资源：

   ```bash
   kubectl delete clusterqueue vla-cluster-queue
   kubectl delete resourceflavor gpu
   kubectl delete workloadpriorityclass low normal high
   ```

4. Kueue、Training Operator、NVIDIA Device Plugin 可能被其他工作负载使用；只有在确认整个集群不再需要时，才按各自安装版本的官方方式卸载。不要直接删除共享 CRD 来“清理”平台。
5. 只有在整台机器专用于此平台且确认所有 K3s 工作负载都可删除时，才运行 K3s 自带的 `/usr/local/bin/k3s-uninstall.sh`。该操作不局限于本项目。
6. `/data/vla-platform/datasets`、`/data/vla-platform/checkpoints` 和 `/data/vla-platform/history` 不会因上述 Kubernetes 命令自动清理。备份后再由管理员手动删除不再需要的宿主机数据；删除 history 即永久删除平台提交/取消历史。

## 10. 常见故障速查

| 现象 | 首先检查 |
|---|---|
| 容器内 `nvidia-smi` 失败 | NVIDIA Container Toolkit、K3s containerd runtime，重启后节点是否重新 Ready |
| 节点 GPU 为 0 | Device Plugin Pod、驱动、插件日志和节点 `nvidia.com/gpu` Capacity |
| PyTorchJob 一直 Pending | PyTorchJob/Pod 事件、LocalQueue、ClusterQueue 配额和现有占用 |
| Pod 已建但不受配额约束 | 队列标签、Kueue controller、LocalQueue/ClusterQueue 对应关系 |
| 8 卡任务失败 | `torchrun` 参数、8 卡可见性、NCCL 日志和容器网络 |
| 镜像拉取失败 | 镜像是否导入 K3s/containerd、标签是否一致、`imagePullPolicy` |
| 日志为空 | 任务是否仍排队、Pod 是否存在、容器是否已写 stdout/stderr |
| Web 401/403 | Bearer 令牌是否正确、后端 ServiceAccount RBAC、请求是否经过正确 API 地址 |
| 登录 401 | 用户名/密码错误（不区分用户不存在）；核对 `PLATFORM_LDAP_USER_TEMPLATE` 拼出的 DN |
| 登录 503 | 平台连不上 LDAP：网络/DNS/证书/`PLATFORM_LDAP_URL`；不是密码问题 |
| 提交 422 `quota exceeded` | 当前用户 GPU 或活跃任务数达到上限，用 `/users` 调整或等任务结束 |

### 镜像拉取失败：先用 events 接口定位

```bash
curl -fsS "http://127.0.0.1:30080/api/v1/jobs/<job-name>/events" | python3 -m json.tool
```

按 `last_seen` 倒序查看，找 `type=Warning`、`reason=Failed` 且 `message` 含 `ImagePullBackOff`、`ErrImagePull` 或 `manifest unknown` 的条目，`object_name` 指出是哪个 Pod。随后用 `sudo k3s ctr -n k8s.io images ls` 与 PyTorchJob/Deployment 里的 `image` 字符串逐字比对（含标签），并确认 `imagePullPolicy` 为 `IfNotPresent`。若 `items` 为空，通常说明任务还不存在、已取消并清理，或事件已过保留期（`kube-apiserver` 默认 `--event-ttl=1h`），此时退回 `kubectl -n vla-training describe pod <pod-name>` 查看。

### Kueue 集成没开：检查 frameworks 和 Workload owner

```bash
kubectl -n kueue-system get cm kueue-manager-config -o yaml | grep -A20 "frameworks:"
kubectl -n vla-training get workload -o yaml | grep -A3 "ownerReferences:"
```

`frameworks` 列表必须包含 `kubeflow.org/pytorchjob`。若只有 `pod` 等条目，Kueue 会按 Pod 逐个放行：Workload 名为 `pod-<pod>-<hash>`，`ownerReferences` 指向 Pod，多副本任务会失去作业级原子放行的语义。修复方式是用 `deploy/kueue/base/kueue-values.yaml`（chart 默认值，仅启用 `kubeflow.org/pytorchjob`）执行 `helm upgrade --install kueue <chart> -n kueue-system -f deploy/kueue/base/kueue-values.yaml`；`kubectl apply -k deploy/kueue/<profile>/` 只管理队列与优先级，不会修改 Kueue 自身配置。

详细诊断命令见 `docs/troubleshooting.md`。验收脚本失败时也会打印只读诊断命令，且不会输出访问令牌。

## 测试

前后端各有一套单元测试，都不需要集群即可运行。

后端 `backend/tests/`（pytest）用内存假实现替代 Kubernetes 客户端，覆盖：参数校验与默认值、
状态推导、取消语义、事件映射与排序、PyTorchJob 模板渲染、历史表迁移、GPU 指标解析。

前端 `frontend/src/**/*.test.ts(x)`（vitest + Testing Library）覆盖格式化工具、API 客户端错误处理、
状态标签渲染与轮询 hook 的生命周期。

```bash
scripts/test.sh              # 前后端都跑
scripts/test.sh backend -q   # 只跑后端，其余参数透传给 pytest
scripts/test.sh frontend     # 只跑前端，其余参数透传给 vitest
```

首次运行会自动创建后端 `.venv`；前端缺少 `node_modules` 时会先 `npm ci`，
Node 版本低于 18 时自动改用 `/opt/node20/bin`（可用 `NODE_BIN_DIR` 覆盖）。

代码规范检查用 `ruff`（规则配置在仓库根 `pyproject.toml`），CI 会在 pytest 之前执行
`ruff check backend`，本地可用同样入口尽早发现问题：

```bash
scripts/lint.sh          # 检查 backend
scripts/lint.sh --fix    # 自动修复可修复项
```

`scripts/release.sh` 发版前会跑完整测试，未通过直接中止。集群侧端到端验收仍由
`scripts/verify-4090.sh` 负责，两者互补：单测保证逻辑正确，验收保证真实集群跑得通。

## 维护

### 清理历史任务

任务结束后对应的 PyTorchJob 对象会一直保留，占用名字也占用 etcd。定期清理：

```bash
scripts/cleanup-jobs.py                      # 预览：7 天前结束的 SUCCEEDED/FAILED 任务
scripts/cleanup-jobs.py --older-than-days 1  # 预览：1 天前的
scripts/cleanup-jobs.py --status SUCCEEDED   # 只看成功的
scripts/cleanup-jobs.py --keep smoke001      # 明确保留某些任务
scripts/cleanup-jobs.py --apply              # 真正删除
```

删除走 REST API（`DELETE /api/v1/jobs/{name}`），所以会进入平台历史，并且**保留真实终态**：
已成功的任务删除后仍是 `SUCCEEDED`，不会被改写成 `CANCELLED`。运行中和排队中的任务不会被清理。

### 仓库离线备份

仓库目前没有配置 git remote，用 git bundle 做离线备份：

```bash
make bundle
# 生成 artifacts/backups/vla-train-platform-<version>.bundle，把它复制到别的机器
git clone vla-train-platform-<version>.bundle restored-repo   # 恢复完整历史与标签
```

建议每次 `make release` 之后执行一次 `make bundle`，并把 bundle 同步到另一台机器。

## 版本管理

仓库根目录的 `VERSION` 是当前版本号，`CHANGELOG.md` 记录每个大版本的变更。

约定：

- 每个**大版本**（功能集变化、部署方式变化、破坏性接口调整）完成并通过验证后提交一次 commit，同时打 `vX.Y.Z` tag。
- 日常的小修可以直接 commit，不单独打 tag，但要在 `CHANGELOG.md` 的对应版本条目下补一行。
- commit message 用 `type: 摘要` 形式，`type` 取 `feat` / `fix` / `docs` / `refactor` / `chore`，正文写清楚动机和验证方式。
- 提交前跑 `bash scripts/verify-4090.sh`，未通过不要提交到主分支。
- 下列内容不入库：`node_modules/`、`frontend/dist/`、`artifacts/`（含镜像 tar 与访问令牌）、SQLite 数据库、`__pycache__`。

发版流程（用 `scripts/release.sh` 固化上面的约定）：

```bash
# 1. 正常开发并提交改动
git add -A && git commit -m "feat: ..."

# 2. 在 CHANGELOG.md 顶部补一个 "## [0.2.0] - YYYY-MM-DD" 小节
$EDITOR CHANGELOG.md
git add CHANGELOG.md && git commit -m "docs: 记录 v0.2.0 变更"

# 3. 发版：校验版本号与 CHANGELOG、跑验收、更新 VERSION、提交并打 tag
scripts/release.sh 0.2.0

# 只预览不执行
scripts/release.sh 0.2.0 --dry-run
```

脚本会拒绝：版本号非 `X.Y.Z`、不高于当前版本、CHANGELOG 缺少对应小节、工作区不干净、
以及 `verify-4090.sh` 不通过（确认要跳过时用 `SKIP_VERIFY=1`）。

## 持续集成

仓库提供两套等价的 CI 配置，只跑不需要集群的单元测试与前端构建：

- **GitHub Actions**：`.github/workflows/ci.yml`，在 `push` 和 `pull_request` 时触发；仅修改 `docs/**` 或根目录 Markdown 时跳过。两个并行 job：
  - 后端：Python 3.12，`pip install -r backend/requirements-dev.txt`，先跑 `ruff check backend`，再跑 `python -m pytest backend/tests`。
  - 前端：Node 20，在 `frontend/` 下执行 `npm ci`、`npm run test`（vitest）、`npm run build`（`tsc --noEmit && vite build`）。
- **GitLab CI**：`.gitlab-ci.yml`，同样分 `backend` 与 `frontend` 两个 job，分别使用 `python:3.12-slim` 和 `node:20` 镜像，执行与上面相同的命令。

两套配置都不引用任何密钥，也不需要证书或访问令牌。

### 为什么 CI 不跑 `scripts/verify-4090.sh`

`scripts/verify-4090.sh` 是目标机器上的端到端验收脚本：它需要访问真实的 K3s 集群（`kubectl` 或 `k3s kubectl`），检查 NVIDIA GPU、Device Plugin、Kueue 和存储，并可能创建、清理验收用的工作负载。托管 CI runner 没有这些集群资源与 GPU，也不应持有集群凭据，所以 CI 只跑单元测试；真实集群验收仍在 4090/A100 目标机上执行。

### 本地复现 CI 检查

```bash
scripts/test.sh                                          # 后端 pytest + 前端 vitest
cd frontend && npm ci && npm run test && npm run build  # 前端单独跑并构建
```

如果 `frontend/node_modules` 已存在，可以跳过 `npm ci`；`scripts/test.sh` 也会复用已有依赖。CI 使用 Python 3.12 与 Node 20，本地建议保持一致（脚本在 Node 低于 18 时会自动改用 `/opt/node20`）。
