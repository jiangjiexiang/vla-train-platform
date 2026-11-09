# 高可用

本文说明 Web 层的可用性边界：为什么当前是单副本、SQLite 单写者带来什么限制、备份恢复
的可承诺窗口、以及滚动升级与回滚的准确步骤。

> 版本状态（0.9.0）：`deploy/web/overlays/ha/`（2 副本、PDB、反亲和、NodePort）与
> `deploy/web/base/` 的 `topologySpreadConstraints` / PDB 由部署分区交付，具体清单以
> overlay 目录下的 README 为准。本文描述的是**边界和操作口径**，不是「已经双活」的承诺。

## 1. 先明确「高可用」指什么

平台里有两层，可用性要求完全不同：

| 层 | 内容 | 故障后的影响 | 当前设计 |
|---|---|---|---|
| 训练层 | PyTorchJob / Pod / GPU | 训练中断 | 由 Kubernetes 与 Kueue 保证；Web 挂掉**不影响**已在跑的训练 |
| Web 层 | FastAPI 容器 + SQLite 历史库 | 网页和 API 不可用、无法提交/取消/看日志 | 单副本 + `Recreate`；可用性靠快速恢复，不靠多副本 |

因此这里的「高可用」不是「Web 永不断线」，而是：

1. Web 故障后能在分钟级恢复，**不丢历史**；
2. 升级过程有明确的停服窗口和回滚路径；
3. 明确写清**当前不具备**的能力，避免误以为可以扩副本。

## 2. 核心约束：SQLite 单写者

| 事实 | 后果 |
|---|---|
| 历史库是一个 SQLite 文件（`PLATFORM_DB_PATH`，默认 `/data/history/platform.db`） | 同一时刻只允许一个写入者；跨 Pod 写会锁冲突，极端情况下损坏 |
| 后端启动时建表/迁移（`CREATE TABLE IF NOT EXISTS`、`ALTER TABLE` 幂等） | 两个副本同时启动会竞争写锁 |
| 会话令牌（v0.9）也存在同一个文件 | 多副本共享同一文件时，登录会话的读写同样受限 |
| PV `vla-web-history` 是 `hostPath` + `ReadWriteOnce` | 同一时刻只能挂到一台节点的一个 Pod 上 |

> 更正说明：`ReadWriteOnce` 是**卷**级别的互斥，不是文件级别的。多个 Pod 挂在同一节点上
> 技术上可以同时访问同一个文件，但这正是最危险的形态——两个进程同时写一个 SQLite 文件，
> 迟早损坏。**不要依赖「同一节点就能多副本」**。

设计决定：Web 保持 `replicas: 1` + `strategy: Recreate`（先删旧 Pod 再建新 Pod），保证任何
时刻只有一个写者。`Recreate` 的代价是升级期间有一段完全不可用（通常几十秒），这是用
可用性换数据安全，**不要改成 `RollingUpdate`**。

## 3. 什么情况下可以做多副本

只有把「状态」从本地文件挪走之后，多副本才有意义。可选路径：

| 路径 | 改动 | 代价 | 结论 |
|---|---|---|---|
| A. 历史库迁到外部 PostgreSQL / MySQL | 改写 `history.py`、`sessions.py`、`quotas.py` 的存储层 | 需要数据库运维；契约未覆盖 | 中期方案，需单独设计与验收 |
| B. PV 改成共享存储（NFS/CephFS RWX）+ SQLite | 只改存储 | **无效**：SQLite 靠文件锁保证单写者，NFS 上的锁语义不可靠，多副本会损坏数据库 | 不推荐 |
| C. `ReadWriteOnce` + 保证同一时刻只有一个 Pod（`Recreate` 或 leader election） | 保持现状 | 没有真正提升可用性 | 当前方案 |

需要多副本且要求历史写入不受影响，就走路径 A；在此之前，把精力放在「快速恢复」上
（探针、PDB、快速重启、镜像预热），而不是副本数。

## 4. 当前可用的可用性措施

| 措施 | 当前状态 | 作用 |
|---|---|---|
| 就绪/存活探针 `/api/v1/health` | base 已有（`readinessProbe` / `livenessProbe` 均指向该路径） | 进程卡死时由 kubelet 重启；健康接口不依赖 Kubernetes API，避免 API 抖动连带重启 Web |
| `Recreate` 策略 | 已有 | 保证单写者 |
| PV 回收策略 `Retain` | 已有（`deploy/base/storage.yaml`） | 误删 Namespace/PVC 不清空宿主目录 |
| PodDisruptionBudget | base 增加（部署分区交付） | 节点维护/驱逐时不会把唯一副本同时赶走 |
| `topologySpreadConstraints` | base 增加（部署分区交付） | 多节点集群下副本分布更合理，不破坏单机 |
| NodePort `30080` | 已有 | 节点 IP 仍可访问；节点故障时不可用 |
| 历史库在线备份 | 见 [`operations.md` 2.2](operations.md#22-sqlite-历史库的安全备份) | 数据层恢复手段 |

注意：`/api/v1/health` 只表示**进程活着**，不代表提交功能可用。Kubernetes 不可用、
`vla-web-auth` Secret 缺失（Pod 根本起不来）、磁盘满等场景不会体现在这个探针上。

## 5. 备份与恢复窗口

### 5.1 能承诺什么

| 项 | 承诺 |
|---|---|
| 历史数据（提交/取消记录、会话令牌、用户配额） | 恢复到最近一次成功备份时的状态；备份之间新增的历史会丢失 |
| 训练任务 | 不丢。任务事实来源是 K3s；Web 恢复后重新读取即可看到 |
| 正在运行的训练 | Web 故障期间继续运行，恢复后状态、日志、事件照常可查 |
| 会话令牌 | 备份里有的令牌恢复后仍有效（前提是数据库文件一致） |

### 5.2 恢复时间与数据损失（RTO / RPO）

单机部署下，实际数字取决于「发现的快慢」和「备份的频率」，下表是操作层面的口径：

| 场景 | RTO（恢复时间） | RPO（数据损失窗口） |
|---|---|---|
| Web 进程崩溃 / OOM | 秒级（kubelet 重启 Pod） | 0 |
| Web 镜像升级 | 数十秒（`Recreate` 的停服窗口） | 0 |
| 历史库文件损坏 | 5–15 分钟（停 Web → 恢复备份 → 校验 → 启动） | 最近一次备份之后的增量 |
| 节点/磁盘故障 | 小时级（换机器、重建 K3s、恢复数据） | 最近一次**异机**备份之后的增量 |

RPO 由备份频率决定：按 [`operations.md` 2.1](operations.md#21-备份范围) 的计划每天备份一次 +
每次升级前一次，单日故障最多损失一天的历史记录。**训练结果不在 RPO 范围内**，checkpoint
另有备份策略（[operations.md 2.3](operations.md#23-checkpoint-备份)）。

### 5.3 恢复步骤（历史库）

```bash
# 0. 确认要恢复的文件，以及备份的完整性
sudo ls -lh /data/vla-platform/backups/history/

# 1. 停 Web（Recreate 下这是唯一的写者）
kubectl -n vla-platform scale deployment/vla-web --replicas=0
kubectl -n vla-platform get pods -l app.kubernetes.io/component=web   # 必须为空

# 2. 备份当前（可能损坏的）库，不要直接覆盖
STAMP=$(date +%Y%m%dT%H%M%S)
sudo cp -a /data/vla-platform/history/platform.db \
  /data/vla-platform/history/platform.db.broken-$STAMP

# 3. 恢复
sudo cp -a /data/vla-platform/backups/history/platform-<STAMP>.db \
  /data/vla-platform/history/platform.db
sudo rm -f /data/vla-platform/history/platform.db-wal \
           /data/vla-platform/history/platform.db-shm \
           /data/vla-platform/history/platform.db-journal

# 4. 校验（必须在启动 Web 之前）
sudo python3 - <<'PY'
import sqlite3
db = sqlite3.connect("/data/vla-platform/history/platform.db")
print("integrity:", db.execute("pragma integrity_check").fetchone()[0])
print("submissions:", db.execute("select count(*) from submissions").fetchone()[0])
print("cancellations:", db.execute("select count(*) from cancellations").fetchone()[0])
PY

# 5. 启动并验证
kubectl -n vla-platform scale deployment/vla-web --replicas=1
kubectl -n vla-platform rollout status deployment/vla-web --timeout=300s
curl -fsS http://127.0.0.1:30080/api/v1/health
```

`-wal` / `-shm` / `-journal` 必须在 Web 停止时删除，否则恢复出来的库会被旧事务覆盖。

## 6. 滚动升级

因为 `Recreate`，升级**不是**滚动升级，而是「停一次、起一次」。准确步骤：

### 6.1 升级前

1. 备份历史库（[operations.md 2.2](operations.md#22-sqlite-历史库的安全备份)）与旧镜像（[operations.md 3.3](operations.md#33-升级已部署实例)第 1 步）。
2. 记录当前版本：`git describe --tags`、`kubectl -n vla-platform get pod -o jsonpath='{...imageID}'`。
3. 确认没有「升级期间必须提交/取消任务」的操作在排队；运行中的训练不受影响，
   但升级窗口内新提交和取消会失败。

### 6.2 升级中

```bash
cd /path/to/vla-train-platform

# 取新版本
git fetch --tags
git checkout v0.9.0

# 构建并导入新镜像（本机无 Docker 时走 buildah）
BUILD_IMAGE=1 PROFILE=4090 bash scripts/deploy-web.sh

# 同 tag 重建不会改变 Pod 模板，必须显式重启
kubectl -n vla-platform rollout restart deployment/vla-web
kubectl -n vla-platform rollout status deployment/vla-web --timeout=300s
```

`rollout status` 返回成功即新 Pod 已就绪（readiness 探针通过）。如果它一直阻塞，说明新
Pod 起不来——此时**不要**手工改副本数硬撑，按 6.4 回滚。

### 6.3 升级后验证

| 检查项 | 命令 | 期望 |
|---|---|---|
| 健康 | `curl -fsS http://127.0.0.1:30080/api/v1/health` | `{"status":"ok",...}`，`auth_required` 与配置一致 |
| 历史库兼容 | `GET /jobs`（带令牌） | 老任务可见，`owner` 为空串而不是 500 |
| 新字段 | `GET /config` | 出现 `max_nodes` 等新字段 |
| 提交链路 | 提交一个短 smoke | `QUEUED → RUNNING → SUCCEEDED` |
| 训练不受影响 | `GET /jobs` | 升级前在跑的任务仍在，状态正常 |
| 镜像版本 | `kubectl -n vla-platform get pod -o jsonpath='{...imageID}'` | 与本次构建一致 |

数据库结构变更（v0.9 新增 `owner` 列、`user_quotas` 表）是幂等迁移，旧库首次启动时自动
补齐（`ALTER TABLE` 幂等）。0.8 的代码不读新增的列和表，因此回滚镜像本身不会因为库结构
变化而失败；但 0.9 期间写入的 `owner` 等信息在回滚后不可见。

### 6.4 回滚

| 场景 | 方式 | 说明 |
|---|---|---|
| 新版本启动失败 | 导入旧镜像 tar → `rollout restart` | [operations.md 3.4 路径 A](operations.md#34-镜像回滚)，最快 |
| 新版本功能异常但能启动 | 用旧 tag 重新构建并 `set image` | [operations.md 3.4 路径 B](operations.md#34-镜像回滚) |
| 新版本写坏了历史库 | 先回滚镜像，再按 5.3 恢复历史库 | 顺序不能反，否则旧代码又写一次坏库 |

`kubectl rollout undo` 只回退 Pod 模板；镜像 tag 仍是 `vla-platform:local` 时，回退后仍指向
同一个 tag，**不能依赖它做镜像回滚**。

## 7. 故障切换（多节点集群）

NodePort 绑在每台节点上，客户端访问的是某台节点的 IP：

| 故障 | 表现 | 处理 |
|---|---|---|
| 运行 Web 的节点宕机 | 30080 不通；PV 是 `hostPath`，新 Pod 可能被调度到别的节点并看到**空目录** | 给 Web 加节点亲和固定到数据所在节点，或迁移到共享存储；否则会出现「历史丢失」的假象（实际是换了个空库） |
| 节点重启 | Pod 在节点恢复后重启，历史在 PV 上，数据不丢 | 确认 PV 的 `hostPath.type: DirectoryOrCreate` 没有在别处新建目录 |
| 客户端只配了一个节点 IP | 该节点故障即不可用 | 客户端改用 VIP/LB，或至少配置多个节点地址轮询 |

因此：**多节点环境下，Web 的「高可用」前提是先解决 `hostPath` PV 的本地性**，具体清单见
[`operations.md` 第 5 节](operations.md#5-扩容到多节点前的检查清单)。

## 8. 未覆盖的风险

| 风险 | 现状 | 建议 |
|---|---|---|
| K3s 控制平面单点 | 单节点 K3s，节点故障即全部中断 | 生产化需多 Server 或外部数据库的 K3s |
| Web 单副本 | `Recreate`，升级/重建期间不可用 | 接受停服窗口，或按 §3 路径 A 做外部数据库后再扩副本 |
| 历史库无自动异地备份 | 依赖人工/定时任务 | 配置 [operations.md 2.3](operations.md#23-checkpoint-备份) 的异机副本并定期演练恢复 |
| 日志只保留在容器 stdout | Pod 被删后无法回溯 | 接入集中日志（Loki/ELK），平台不承诺保存已删除 Pod 的日志 |
| 无认证的 `off` 模式 | 任何人可提交/取消 | 只用于可信内网，见 [`auth.md`](auth.md) 第 2 节 |