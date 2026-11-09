# 运维手册

本手册面向当前单节点部署：K3s v1.36.6+k3s1、Kueue v0.20.5（已启用
`kubeflow.org/pytorchjob` 集成）、Training Operator v1.9.4、NVIDIA Device Plugin
v0.20.1（驱动 595.71.05）。命令默认在平台节点执行；`<...>` 表示需要替换的值。

诊断优先只读，任何 `apply`、`delete`、`scale`、`rollout restart` 之前先备份，并确认
操作对象是 `vla-platform` / `vla-training`，不要动共享的 Kueue、Training Operator 和
Device Plugin。

## 0. 环境基线

| 项目 | 值 |
|---|---|
| 集群 | 单节点 K3s，节点 Ready，`nvidia.com/gpu` 可分配 |
| Web | `vla-platform/vla-web`，单容器，NodePort `30080` |
| 训练 | `vla-training`，LocalQueue `default` → ClusterQueue `vla-cluster-queue` → ResourceFlavor `gpu` |
| GPU 配额 | 4090：`nominalQuota: 1`；A100：`nominalQuota: 8` |
| 历史库 | 宿主机 `/data/vla-platform/history/platform.db`，PVC `vla-web-history` |
| 数据集 | 宿主机 `/data/vla-platform/datasets` → 容器 `/datasets`（只读） |
| Checkpoint | 宿主机 `/data/vla-platform/checkpoints` → 容器 `/checkpoints`（可写） |
| Web 镜像 | `vla-platform:local`，直接 import 到 containerd，无外部 registry |
| 版本 | 仓库根目录 `VERSION`，当前 `0.4.0`；变更见 `CHANGELOG.md` |

当前用户可直接执行 `kubectl`。换用其他账号时，用
`echo '<sudo密码>' | sudo -S -p '' /usr/local/bin/k3s kubectl ...`，或先
`export KUBECONFIG=/etc/rancher/k3s/k3s.yaml`。`helm` 不读取 K3s 默认 kubeconfig，
操作 Kueue release 前必须先 `export KUBECONFIG=/etc/rancher/k3s/k3s.yaml`。

## 1. 日常巡检

建议每天一次，升级前后和异常时加做。先跑完这一组，再按下面的判断标准逐项看：

```bash
kubectl get nodes -o wide
kubectl -n vla-platform get deploy,pod,svc,pvc -o wide
kubectl -n vla-training get pytorchjob,pod,workload -o wide
kubectl get clusterqueue vla-cluster-queue
nvidia-smi
df -h /data
curl -fsS -m 3 http://127.0.0.1:30080/api/v1/health
```

`/api/v1/health` 不需要令牌；开启鉴权后，本节其余 `/api/v1` 请求都要加
`-H "Authorization: Bearer $PLATFORM_API_TOKEN"`。

### 1.1 节点与 GPU

```bash
kubectl get nodes -o wide
kubectl get node -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.conditions[?(@.type=="Ready")].status}{"\t"}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'
kubectl get node -o jsonpath='{range .items[*]}{range .status.conditions[*]}{.type}={.status}{" "}{end}{"\n"}{end}'
nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
```

正常：`Ready=True`；`MemoryPressure=False`、`DiskPressure=False`、`PIDPressure=False`；
4090 的 `nvidia.com/gpu` 为 1，A100 为 8；`nvidia-smi` 能看到全部卡。

异常：

- `Ready=False` 或出现 `DiskPressure=True`：先处理节点，不要继续提交训练。
- `allocatable` 为 0 或小于物理卡数：Device Plugin 异常，见 `docs/troubleshooting.md` 第 2 节。
- `allocatable` 正常但 `nvidia-smi` 报错：宿主机驱动问题。
- `nvidia-smi` 有 compute process，而 `kubectl` 中没有对应训练 Pod：GPU 被平台外进程占用。
  编写本手册时就有一个 `ray::FSDPVlaSftWorker.run_training` 进程占用约 38 GB 显存，
  但 Kubernetes 仍显示 1 张卡空闲。此时提交任务会在真正分配显存时失败，见 4.5。

判断 GPU 是否真空闲，要把 Kubernetes 分配量和 `nvidia-smi` 一起看，不能只看
`/cluster/summary` 的 `gpu_available`。

### 1.2 Web 健康

```bash
kubectl -n vla-platform get deploy,pod -o wide
curl -fsS http://127.0.0.1:30080/api/v1/health
curl -fsS http://127.0.0.1:30080/api/v1/cluster/summary | python3 -m json.tool | head -40
kubectl -n vla-platform logs deploy/vla-web --tail=100
```

正常：Deployment `1/1`，Pod `Running`、`RESTARTS` 为 0，健康响应为
`{"status":"ok","auth_required":false}`（开启鉴权后为 `true`），`/cluster/summary`
能返回节点和 GPU 数据。

异常：

- `0/1`、`CrashLoopBackOff`、`RESTARTS` 持续增长：见 4.1。
- 健康检查超时但 Pod 是 Running：读 Web 日志和 Pod 事件，区分应用卡死、探针失败和节点问题。
- 健康检查返回 200 但 `auth_required` 与预期不一致：检查 Deployment 的
  `PLATFORM_AUTH_REQUIRED` 和实际使用的 overlay。

### 1.3 队列积压

```bash
kubectl -n vla-training get workload -o wide
kubectl -n vla-training get pytorchjob -o wide
kubectl get clusterqueue vla-cluster-queue -o jsonpath='{.status.pendingWorkloads}{"\t"}{.status.admittedWorkloads}{"\t"}{.status.flavorsUsage[0].resources[0].total}{"\n"}'
curl -fsS http://127.0.0.1:30080/api/v1/queues | python3 -m json.tool
```

正常：没有任务时 `pendingWorkloads`、`admittedWorkloads`、GPU 用量都为 0；有运行时
`flavorsUsage[0].resources[0].total` 表示已占用 GPU 数，不应超过 `nominalQuota`；
`pendingWorkloads` 表示排队数；已结束任务的 Workload 显示 `FINISHED=True`。

异常：

- `pendingWorkloads > 0` 且剩余 GPU 足够满足队首任务，但任务仍不放行：Kueue 没有放行，
  检查 controller 和 LocalQueue/ClusterQueue 关系，见 4.3。剩余 GPU 小于任务请求时，
  排队是正常的。
- GPU 用量等于配额且仍有排队：配额确实被占满，见 4.6。
- 已取消任务仍出现在 `workload` 中且 `ADMITTED=True`：确认 Pod 是否已删除；宽限期内
  GPU 尚未释放是正常的，见 4.6。

任务列表接口只反映 Kubernetes 当前对象和取消历史，不能用它反推历史总数：

```bash
curl -fsS http://127.0.0.1:30080/api/v1/jobs | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print(d["total"], {s:sum(1 for i in d["items"] if i["status"]==s) for s in {i["status"] for i in d["items"]}})'
```

### 1.4 磁盘余量

```bash
df -h /data
df -i /data
du -sh /data/vla-platform/datasets /data/vla-platform/checkpoints /data/vla-platform/history
du -sh /data/vla-platform/checkpoints/* 2>/dev/null | sort -h | tail -10
```

正常（建议阈值）：`Use%` 低于 80%，inode 充足，checkpoint 增长与最近任务数量相符。

异常：

- 80%～90%：安排清理旧 checkpoint 或扩容，先确认已备份。
- 超过 90% 或出现 `DiskPressure=True`：暂停提交新任务。磁盘写满会导致 SQLite 写入失败、
  checkpoint 不落盘，甚至 Web Pod 被驱逐。
- checkpoint 目录异常增大：检查失败任务是否反复重试写文件，见 4.7。

清理 checkpoint 前必须先备份；删除宿主机目录不可恢复。Kubernetes 中删除任务不会删除
宿主机 checkpoint。

### 1.5 历史库完整性

SQLite 是单文件、当前为 `journal_mode=delete`（非 WAL）。日常只读检查：

```bash
ls -lh /data/vla-platform/history/platform.db*
python3 - <<'PY'
import sqlite3
p = "/data/vla-platform/history/platform.db"
c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
print("integrity:", c.execute("pragma integrity_check").fetchone()[0])
print("submissions:", c.execute("select count(*) from submissions").fetchone()[0])
print("cancellations:", c.execute("select count(*) from cancellations").fetchone()[0])
PY
```

正常：`integrity: ok`，两个计数能正常返回。异常：返回错误或计数失败时不要继续写入，
按第 2 节从最近备份恢复。

### 1.6 每日记录

至少记录：节点 Ready/GPU 可分配数、Web 是否 1/1、队列 pending 数、`nvidia-smi`
显存、`/data` 使用率、当天提交/成功/失败任务数。出现异常时同时保存任务名、Pod 名、
事件时间和完整错误信息。

## 2. 备份与恢复

### 2.1 备份范围

| 对象 | 频率 | 方式 | 保留 |
|---|---|---|---|
| SQLite 历史库 | 每天 + 每次升级前 | Python `sqlite3` 在线备份 API | 最近 7 天 + 4 周 |
| checkpoint | 每天 + 重要任务结束后 | `rsync --link-dest` 增量快照 | 按项目要求，至少保留最近一次可恢复版本 |
| 数据集 | 按数据版本 | 单独归档，不由平台写入 | 与数据版本一致 |
| Web 镜像 | 每次升级前 | `k3s ctr images export` | 最近 2 个版本 |
| 清单/配置 | 每次发版 | Git tag（`deploy/`、`scripts/`、`VERSION`） | 全部 tag |
| `vla-web-auth` Secret | 轮换前后 | 受控的密钥管理，不要明文入库 | 按安全策略 |

备份必须复制到另一台机器或另一块磁盘；只放在 `/data/vla-platform/backups` 不能抵御
本机磁盘故障。

### 2.2 SQLite 历史库的安全备份

不要在 Web 正在写入时直接 `cp platform.db`。当前数据库使用 rollback journal，
复制过程中可能拿到半个事务。优先使用 SQLite 在线备份 API；本机没有 `sqlite3` 命令行，
用 Python 标准库即可（本机 Python 3.10，已验证可用）：

```bash
sudo install -d -m 0750 /data/vla-platform/backups/history
STAMP=$(date +%Y%m%dT%H%M%S)
sudo python3 - "/data/vla-platform/backups/history/platform-$STAMP.db" <<'PY'
import os, sqlite3, sys

src = "/data/vla-platform/history/platform.db"
dst = sys.argv[1]
if os.path.exists(dst):
    raise SystemExit(f"refusing to overwrite {dst}")

source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
target = sqlite3.connect(dst, timeout=30)
try:
    source.backup(target)
    print("integrity:", target.execute("pragma integrity_check").fetchone()[0])
    print("submissions:", target.execute("select count(*) from submissions").fetchone()[0])
    print("cancellations:", target.execute("select count(*) from cancellations").fetchone()[0])
finally:
    target.close()
    source.close()
PY
sudo chmod 0640 "/data/vla-platform/backups/history/platform-$STAMP.db"
sudo ls -lh "/data/vla-platform/backups/history/platform-$STAMP.db"
```

`Connection.backup()` 会在写入过程中自动重试，不需要停 Web；脚本最后打印的
`integrity: ok` 和两个计数就是这次备份的初步验证。

只有在 Web 已停止时，才可以冷复制：

```bash
kubectl -n vla-platform scale deployment/vla-web --replicas=0
kubectl -n vla-platform get pods -l app.kubernetes.io/component=web   # 确认无 Pod
sudo rm -f /data/vla-platform/history/platform.db-wal \
           /data/vla-platform/history/platform.db-shm \
           /data/vla-platform/history/platform.db-journal
sudo cp -a /data/vla-platform/history/platform.db \
           /data/vla-platform/backups/history/platform-$(date +%Y%m%dT%H%M%S).db
kubectl -n vla-platform scale deployment/vla-web --replicas=1
kubectl -n vla-platform rollout status deployment/vla-web --timeout=300s
```

删除 `-wal`/`-shm`/`-journal` 前必须确认 Web 已停止；这些文件可能包含尚未合并的事务。
如果没有停 Web 就复制，备份只能视为“可能不一致”，必须按 2.5 验证。

### 2.3 checkpoint 备份

checkpoint 按任务名分目录，位于 `/data/vla-platform/checkpoints/<job-name>`。用
硬链接快照保存每日增量，未变化的文件不重复占空间（本机已验证 `--link-dest` 生效）：

```bash
sudo install -d -m 0750 /data/vla-platform/backups/checkpoints
STAMP=$(date +%Y%m%dT%H%M%S)
sudo rsync -a --numeric-ids \
  --link-dest=/data/vla-platform/backups/checkpoints/latest \
  /data/vla-platform/checkpoints/ \
  "/data/vla-platform/backups/checkpoints/$STAMP/"
sudo ln -sfn "$STAMP" /data/vla-platform/backups/checkpoints/latest
sudo find /data/vla-platform/backups/checkpoints -maxdepth 1 -mindepth 1 -type d -exec du -sh {} +
```

第一次运行 `latest` 不存在时，`rsync` 会打印
`--link-dest arg does not exist` 但仍继续，这是正常的；第二次起才是硬链接增量。

一致性要求：

- checkpoint 正在写入时，`rsync` 只保证单文件级别的“某次读取”，可能拿到未写完的文件。
  最稳妥的做法是只备份已 `SUCCEEDED` 的任务目录，或等运行中任务结束后再跑快照。
- 需要备份运行中任务时，先在网页/`vla-train` 确认任务状态，并接受该目录可能是
  crash-consistent；恢复后必须让训练代码实际加载验证。
- 数据集是只读输入，平台不修改；按数据版本单独备份，不要和 checkpoint 混在一起。

异机副本（示例，需要目标机 SSH 免密）：

```bash
# 先确认源目录完整；--delete 会让目标端与源端完全一致，不要反向执行
sudo rsync -a --delete /data/vla-platform/backups/ \
  backup@<backup-host>:/srv/vla-platform-backups/
```

### 2.4 恢复到新机器

前提：新机器驱动、CUDA、K3s 版本与备份来源兼容，且 `nvidia-smi` 正常。路径保持
`/data/vla-platform/...`，否则 `deploy/base/storage.yaml` 和 PyTorchJob 模板的
hostPath 需要同步修改。

1. 取回代码和基础组件（`<repo-url>` 由管理员提供；当前仓库未配置 remote）：

```bash
sudo install -d -o jjx -g jjx /home/jjx
git clone <repo-url> /home/jjx/vla-train-platform
cd /home/jjx/vla-train-platform
git checkout v0.4.0
PROFILE=4090 bash scripts/preflight.sh
PROFILE=4090 bash scripts/install-k3s-gpu.sh
# 先核对脚本头部的 KUEUE_VERSION/TRAINING_OPERATOR_VERSION/NVIDIA_DEVICE_PLUGIN_VERSION
PROFILE=4090 bash scripts/install-stack.sh
```

注意：`scripts/install-stack.sh` 的默认组件版本（Kueue v0.20.5、Training Operator v1.9.4、
Device Plugin v0.20.1）已与本仓库验证过的版本对齐。恢复时仍建议用同名环境变量或
`*_MANIFEST` / `IMAGE_TAR_DIR` 显式固定与生产一致的版本，不要直接降级；
离线安装用法见脚本 `--help`。

`install-stack.sh` 会创建 `/data/vla-platform/{datasets,checkpoints,history}` 并安装
Kueue 队列；此时还不要启动 Web，避免它先在空库上初始化。

2. 恢复数据（把 `/mnt/backup` 换成实际备份挂载点）：

```bash
sudo install -d -m 0755 /data/vla-platform/history
sudo rsync -a /mnt/backup/checkpoints/latest/ /data/vla-platform/checkpoints/
sudo rsync -a /mnt/backup/datasets/ /data/vla-platform/datasets/   # 如有单独备份
sudo rm -f /data/vla-platform/history/platform.db-wal \
           /data/vla-platform/history/platform.db-shm \
           /data/vla-platform/history/platform.db-journal
sudo cp /mnt/backup/history/platform-<STAMP>.db /data/vla-platform/history/platform.db
sudo chown root:root /data/vla-platform/history/platform.db
sudo chmod 0644 /data/vla-platform/history/platform.db
```

3. 恢复镜像并部署 Web（二选一）：

```bash
# 路径 A：有镜像归档
sudo k3s ctr images import /mnt/backup/images/vla-platform-local-<STAMP>.tar
sudo k3s ctr images import /mnt/backup/images/vla-smoke-latest.tar

export PLATFORM_API_TOKEN="$(openssl rand -hex 32)"
WEB_IMAGE_TAR=/mnt/backup/images/vla-platform-local-<STAMP>.tar \
SMOKE_IMAGE_TAR=/mnt/backup/images/vla-smoke-latest.tar \
PROFILE=4090 bash scripts/deploy-web.sh
```

```bash
# 路径 B：从 tag 重新构建（本机无 Docker，用 buildah）
cd /home/jjx/vla-train-platform
cd frontend && /opt/node20/bin/npm ci && /opt/node20/bin/npm run build && cd ..
buildah bud --isolation=chroot -t vla-platform:local -f backend/Dockerfile .
buildah bud -t vla-smoke:latest images/smoke-train
buildah push vla-platform:local docker-archive:/tmp/vla-platform.tar:vla-platform:local
buildah push vla-smoke:latest docker-archive:/tmp/vla-smoke.tar:vla-smoke:latest
sudo k3s ctr images import /tmp/vla-platform.tar
sudo k3s ctr images import /tmp/vla-smoke.tar
rm -f /tmp/vla-platform.tar /tmp/vla-smoke.tar

export PLATFORM_API_TOKEN="$(openssl rand -hex 32)"
BUILD_IMAGE=0 PROFILE=4090 bash scripts/deploy-web.sh
```

第一次部署必须提供令牌；如果恢复旧令牌，改用旧的 `vla-web-auth` Secret，并同步更新
所有客户端。`deploy-web.sh` 会应用 `deploy/base`、Web overlay 并等待 rollout。

4. 验证：见 2.6；随后提交一个短 smoke 任务，确认调度、日志、checkpoint 三条链路。

### 2.5 原地恢复历史库

用于 Web 数据损坏或回滚到某个备份。必须停写，不能覆盖正在使用的数据库：

```bash
kubectl -n vla-platform scale deployment/vla-web --replicas=0
kubectl -n vla-platform get pods -l app.kubernetes.io/component=web   # 等到无输出
sudo install -d -m 0750 /data/vla-platform/backups/history
sudo cp /data/vla-platform/history/platform.db \
        /data/vla-platform/backups/history/platform-before-restore-$(date +%Y%m%dT%H%M%S).db
sudo rm -f /data/vla-platform/history/platform.db-wal \
           /data/vla-platform/history/platform.db-shm \
           /data/vla-platform/history/platform.db-journal
sudo cp /data/vla-platform/backups/history/platform-<STAMP>.db \
        /data/vla-platform/history/platform.db
sudo chown root:root /data/vla-platform/history/platform.db
sudo chmod 0644 /data/vla-platform/history/platform.db
kubectl -n vla-platform scale deployment/vla-web --replicas=1
kubectl -n vla-platform rollout status deployment/vla-web --timeout=300s
```

Web 容器当前以 root 运行，`0644 root:root` 可读写；若以后改为非 root，需要把文件
chown 到对应 UID。恢复后不要再手工改数据库文件。

### 2.6 备份验证

每次备份至少做一次，恢复演练每季度一次：

```bash
# 1. SQLite：完整性 + 计数
sudo python3 - <<'PY'
import sqlite3
p = "/data/vla-platform/backups/history/platform-<STAMP>.db"
c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
print("integrity:", c.execute("pragma integrity_check").fetchone()[0])
print("submissions:", c.execute("select count(*) from submissions").fetchone()[0])
print("cancellations:", c.execute("select count(*) from cancellations").fetchone()[0])
print("latest:", c.execute("select name, submitted_at from submissions order by id desc limit 1").fetchone())
PY

# 2. checkpoint：文件数和大小与源目录对比
find /data/vla-platform/checkpoints -type f | wc -l
sudo find -L /data/vla-platform/backups/checkpoints/latest -type f | wc -l
du -sh /data/vla-platform/checkpoints
sudo du -sh /data/vla-platform/backups/checkpoints/latest/

# 3. 抽一个 checkpoint 做哈希，恢复后比对
sudo sha256sum /data/vla-platform/checkpoints/<job-name>/<file>
```

SQLite 的 `integrity: ok` 只证明文件结构完整；还必须能读出最近的提交记录。checkpoint
的数量/大小一致只是必要条件，最终要用训练代码实际 `load` 一次。恢复演练不要直接在
生产库上做，复制到临时路径验证即可。

## 3. 升级与回滚

### 3.1 从 git clone 到首次部署

先确认工作区干净（`git status --porcelain` 无输出）且目标 tag 已存在；`release.sh`
同样拒绝脏工作区。

```bash
git clone <repo-url> /home/jjx/vla-train-platform
cd /home/jjx/vla-train-platform
git checkout v0.4.0

PROFILE=4090 bash scripts/preflight.sh
PROFILE=4090 bash scripts/install-k3s-gpu.sh
# 组件版本必须与当前生产一致；脚本默认值可能更低，先核对脚本头部变量
PROFILE=4090 bash scripts/install-stack.sh
```

`install-stack.sh` 默认值可能低于当前部署（见 2.4 的版本说明）。首次部署前先核对
`KUEUE_VERSION`、`TRAINING_OPERATOR_VERSION`、`NVIDIA_DEVICE_PLUGIN_VERSION`，需要时
用环境变量或本地 `*_MANIFEST` 覆盖，避免新机器组件版本与镜像/CRD 不兼容。

前端必须用 Node 20 构建（系统 Node 12 无法构建 Vite 6 工程），然后构建并导入 Web
和 smoke 镜像：

```bash
cd frontend
/opt/node20/bin/npm ci
/opt/node20/bin/npm run build
cd ..

buildah bud --isolation=chroot -t vla-platform:local -f backend/Dockerfile .
buildah bud -t vla-smoke:latest images/smoke-train

buildah push vla-platform:local docker-archive:/tmp/vla-platform-web.tar:vla-platform:local
buildah push vla-smoke:latest docker-archive:/tmp/vla-smoke.tar:vla-smoke:latest
sudo k3s ctr images import /tmp/vla-platform-web.tar
sudo k3s ctr images import /tmp/vla-smoke.tar
rm -f /tmp/vla-platform-web.tar /tmp/vla-smoke.tar

export PLATFORM_API_TOKEN="$(openssl rand -hex 32)"
PROFILE=4090 bash scripts/deploy-web.sh
```

`deploy-web.sh` 的默认行为是：镜像已存在则复用；不存在才构建；`BUILD_IMAGE=0` 强制
复用，`BUILD_IMAGE=1` 强制重建；`WEB_IMAGE_TAR`/`SMOKE_IMAGE_TAR` 可直接导入归档。
首次部署必须提供 `PLATFORM_API_TOKEN` 或 `PLATFORM_API_TOKEN_FILE`。

### 3.2 发版：release.sh

`release.sh` 只做版本与 Git 操作，不构建镜像、不部署。它要求工作区干净、`VERSION`
中的版本可递增、`CHANGELOG.md` 已有对应小节，并默认运行 `scripts/test.sh` 和
`scripts/verify-4090.sh`。当前 `VERSION` 为 `0.4.0`；下面以 `0.5.0` 为例：

```bash
cd /home/jjx/vla-train-platform
git switch -c release/0.5.0

# 修改代码后先跑测试
scripts/test.sh

# 在 CHANGELOG.md 顶部补 "## [0.5.0] - YYYY-MM-DD" 小节
$EDITOR CHANGELOG.md
git add -A && git commit -m "docs: 记录 v0.5.0 变更"

scripts/release.sh 0.5.0 --dry-run   # 只打印将执行的动作
scripts/release.sh 0.5.0             # 跑单测/验收、写 VERSION、提交、打 tag

git push origin HEAD:main --tags     # 仓库配置了 remote 时；主分支名按实际调整
```

脚本会用 `scripts/sync_frontend_version.py` 同步 `frontend/package.json`，并创建
`release: v0.5.0` 提交和 `v0.5.0` tag。验收不通过时不要用 `SKIP_VERIFY=1` 绕过，
除非已确认失败项与本次改动无关并在变更记录中写明。

### 3.3 升级已部署实例

顺序是：备份 → 取新版本 → 构建/导入 → 部署 → 验证。先按 2.2 备份 SQLite、按 2.3
做一次 checkpoint 快照；镜像 tag 仍是 `vla-platform:local`，升级前还必须保存旧镜像，
否则回滚没有旧层可用。

```bash
cd /home/jjx/vla-train-platform

# 1. 备份（历史库、checkpoint、旧镜像）
sudo install -d -m 0750 /data/vla-platform/backups/images
sudo k3s ctr images export \
  /data/vla-platform/backups/images/vla-platform-local-$(date +%Y%m%dT%H%M%S).tar \
  docker.io/library/vla-platform:local

# 2. 取新版本（先确认 git status --porcelain 为空）
git fetch --tags
git checkout v0.5.0

# 3. 构建前端和镜像（本机无 Docker）
cd frontend && /opt/node20/bin/npm ci && /opt/node20/bin/npm run build && cd ..
BUILD_IMAGE=1 PROFILE=4090 bash scripts/deploy-web.sh

# 同 tag 重建不会改变 Deployment 模板，必须显式重启才会用上新镜像
kubectl -n vla-platform rollout restart deployment/vla-web
kubectl -n vla-platform rollout status deployment/vla-web --timeout=300s

# 4. 验证
curl -fsS http://127.0.0.1:30080/api/v1/health
kubectl -n vla-platform get pod -l app.kubernetes.io/component=web \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}{"\n"}'
```

`BUILD_IMAGE=1` 会走 buildah 构建并 import；如果 `frontend/dist` 不存在会直接失败，
先执行上面的前端构建。已有镜像归档时用 `WEB_IMAGE_TAR=` 可跳过构建。注意：镜像 tag
仍是 `vla-platform:local` 时，`kubectl apply` 不会改 Pod 模板，`deploy-web.sh` 也不会
自动重启，因此必须执行上面的 `rollout restart`，否则新镜像不会被使用。

升级后至少检查：Web `1/1`、健康接口正常、`kubectl get pytorchjob` 能看到旧任务历史、
提交一个 smoke 任务走完 `QUEUED → RUNNING → SUCCEEDED`、checkpoint 落到
`/data/vla-platform/checkpoints/<job-name>`。

### 3.4 镜像回滚

**路径 A：导入旧 tar（推荐，最快）**

```bash
sudo ls -lh /data/vla-platform/backups/images/
sudo k3s ctr images import /data/vla-platform/backups/images/vla-platform-local-<STAMP>.tar
kubectl -n vla-platform rollout restart deployment/vla-web
kubectl -n vla-platform rollout status deployment/vla-web --timeout=300s
kubectl -n vla-platform get pod -l app.kubernetes.io/component=web \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}{"\n"}'
```

导入会覆盖 containerd 中的同名 tag；`rollout restart` 让新 Pod 重新解析
`vla-platform:local`。必须核对 `imageID` 与旧镜像 digest 一致，不能只看
`deploy-web.sh` 成功。

**路径 B：从旧 tag 重新构建**

```bash
cd /home/jjx/vla-train-platform
git checkout v0.4.0
cd frontend && /opt/node20/bin/npm ci && /opt/node20/bin/npm run build && cd ..
buildah bud --isolation=chroot -t vla-platform:rollback-0.4.0 -f backend/Dockerfile .
buildah push vla-platform:rollback-0.4.0 \
  docker-archive:/tmp/vla-platform-rollback.tar:vla-platform:rollback-0.4.0
sudo k3s ctr images import /tmp/vla-platform-rollback.tar
rm -f /tmp/vla-platform-rollback.tar

WEB_IMAGE=vla-platform:rollback-0.4.0 BUILD_IMAGE=0 PROFILE=4090 \
  bash scripts/deploy-web.sh
```

`deploy-web.sh` 会在 apply 后执行 `kubectl set image`，把 Deployment 指向
`vla-platform:rollback-0.4.0`。下一次正常执行 `deploy-web.sh` 会把它改回
`vla-platform:local`，这是预期行为。

注意：`kubectl rollout undo` 只回退 Pod 模板。重新构建同 tag 镜像通常不会产生新的
Deployment revision，或者回退后仍指向同一个 tag，因此不能依赖 `rollout undo` 做镜像
回滚；要么导入旧 tar，要么用新 tag + `WEB_IMAGE=`。回滚镜像前先确认数据库结构兼容；
如果不兼容，按 2.5 一并恢复历史库备份。

### 3.5 Kueue 配置回滚

Kueue 的 `kueue-manager-config` ConfigMap 可能被 `scripts/install-stack.sh` 直接
patch，不一定是 Helm 管理的最后状态。改配置前先备份 ConfigMap：

```bash
sudo install -d -m 0750 /data/vla-platform/backups/kueue
kubectl -n kueue-system get cm kueue-manager-config -o yaml \
  | sudo tee /data/vla-platform/backups/kueue/kueue-manager-config-$(date +%Y%m%dT%H%M%S).yaml >/dev/null
```

**配置回滚（优先）**：从已知 Git tag 取出配置并重建 ConfigMap，方式与
`install-stack.sh` 一致：

```bash
cd /home/jjx/vla-train-platform
git show v0.4.0:deploy/kueue/base/controller_manager_config.yaml > /tmp/kueue-manager-config.yaml

kubectl -n kueue-system create configmap kueue-manager-config \
  --from-file=controller_manager_config.yaml=/tmp/kueue-manager-config.yaml \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n kueue-system rollout restart deployment/kueue-controller-manager
kubectl -n kueue-system rollout status deployment/kueue-controller-manager --timeout=300s

kubectl -n kueue-system get cm kueue-manager-config -o yaml \
  | sed -n '/frameworks:/,/^[^ ]/p'
rm -f /tmp/kueue-manager-config.yaml
```

`frameworks` 必须包含 `kubeflow.org/pytorchjob`。只看到 `pod` 时，排队会退化为
Pod 级，必须立即回滚。

**Helm 级回滚**（仅当问题来自 chart values，而不是手工 patch 的 ConfigMap）：

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
helm history kueue -n kueue-system
helm rollback kueue <GOOD_REVISION> -n kueue-system --dry-run
helm rollback kueue <GOOD_REVISION> -n kueue-system
```

当前 `helm history` 中 revision 1 是 failed，revision 2 是 deployed；不要把 1 作为
回滚目标。Helm 回滚不一定覆盖 `install-stack.sh` 直接 patch 的 ConfigMap，回滚后仍要
用上面的 `frameworks` 检查确认。

**队列/配额配置回滚**：从 tag 取回 `deploy/kueue/<profile>/queues.yaml`，先确认没有
运行中任务依赖旧配额，再 apply：

```bash
# A100 机器把 4090 换成 a100
git show v0.4.0:deploy/kueue/4090/queues.yaml > /tmp/queues.yaml
kubectl apply --dry-run=client -f /tmp/queues.yaml
kubectl apply -f /tmp/queues.yaml
kubectl wait clusterqueue/vla-cluster-queue --for=condition=Active --timeout=120s
kubectl get clusterqueue vla-cluster-queue -o jsonpath='{.spec.resourceGroups[0].flavors[0].resources[0].nominalQuota}{"\n"}'
rm -f /tmp/queues.yaml
```

`queues.yaml` 同时包含 ResourceFlavor、ClusterQueue、LocalQueue。回滚配额会让排队任务
的放行行为立即变化，但不会驱逐已经在运行的 Pod；需要先取消或等待现有任务结束。

## 4. 故障处理

下面每条都按“判断依据 → 处理动作”组织。先保存任务名、Pod 名、事件和完整报错，再做
变更。开启鉴权后，所有 `/api/v1` 请求（健康检查除外）都要带
`Authorization: Bearer <token>`。

### 4.1 Web Pod 起不来

判断依据：

```bash
kubectl -n vla-platform get deploy,pod -o wide
kubectl -n vla-platform describe pod -l app.kubernetes.io/component=web
kubectl -n vla-platform logs deploy/vla-web --tail=200
kubectl -n vla-platform logs <web-pod-name> --previous --tail=200
kubectl -n vla-platform get events --sort-by=.lastTimestamp | tail -20
kubectl -n vla-platform get pvc vla-web-history
```

- `ImagePullBackOff` / `ErrImagePull`：按 4.2 处理。
- `CreateContainerConfigError` 且事件提到 `secret "vla-web-auth" not found`：Secret 缺失。
  用 `scripts/deploy-web.sh` 并提供 `PLATFORM_API_TOKEN` 重建，不要手写空 Secret。
- `Pending`：看 `describe pod` 的 Events，重点查节点资源、PVC 绑定、hostPath 和污点。
- `CrashLoopBackOff`：看 `--previous` 日志，常见是数据库目录不可写、启动异常、端口占用。
- `Running` 但 `READY 0/1`：探针失败；读日志并确认 `/api/v1/health` 在容器内可访问。

处理动作：只修复根因，然后 `kubectl -n vla-platform rollout restart deployment/vla-web`
并 `rollout status`。不要删除 PVC、Namespace 或历史库来“重试”。Deployment 使用
`Recreate`，重启期间 NodePort 会短暂不可用，这是预期行为。

### 4.2 镜像拉取失败

判断依据：

```bash
curl -fsS "http://127.0.0.1:30080/api/v1/jobs/<job-name>/events" | python3 -m json.tool
cli/vla-train --server http://127.0.0.1:30080 --token local events <job-name>
kubectl -n vla-training get events --sort-by=.lastTimestamp | tail -30
sudo k3s ctr images ls | grep -E 'vla-platform|vla-smoke'
kubectl -n vla-training describe pod <pod-name>
```

事件里出现 `ErrImagePull`、`ImagePullBackOff`、`manifest unknown`，且
`object_name` 指向训练 Pod 或 Web Pod。把事件中的镜像名与 PyTorchJob/Deployment 里的
镜像字符串逐字比对，包括 tag：

```bash
kubectl -n vla-training get pytorchjob <job-name> -o yaml | grep -E 'image:'
kubectl -n vla-platform get deploy vla-web -o yaml | grep -E 'image:'
```

处理动作：

- 训练镜像必须已在**每个**目标节点的 containerd 中：`sudo k3s ctr images import`。
  导入后 `ctr images ls` 通常显示为 `docker.io/library/<name>:<tag>`，比对时注意前缀。
- 本地导入镜像的 Deployment/PyTorchJob 使用 `IfNotPresent`；不要把 `latest` 改成
  `Always`，否则会尝试访问不存在的 registry。
- Web 镜像 tag 必须是 `vla-platform:local`，与 `deploy/web/base/deployment.yaml` 一致。
- 修正后 Web 用 `rollout restart`；训练任务取消后重新提交，让 Training Operator 重建
  Pod（不要只删 Pod 等它自己恢复）。
- 平台不提供镜像仓库；不要假设节点能访问外网 registry。

当前免登录时 CLI 仍要求非空令牌，可用 `--token local` 占位；开启鉴权后换成真实令牌。

### 4.3 任务一直 QUEUED

判断依据：

```bash
kubectl -n vla-training get pytorchjob <job-name> -o yaml
kubectl -n vla-training get workload -o wide
kubectl -n vla-training describe workload <workload-name>
kubectl get clusterqueue vla-cluster-queue -o yaml
curl -fsS http://127.0.0.1:30080/api/v1/jobs/<job-name> | python3 -m json.tool
```

先看 API 的 `message` 和 `pod_status`：

- `message` 为 `Waiting for Kueue admission`、`pod_status` 为 `null`：还没放行，正常排队。
  检查 ClusterQueue 的 `pendingWorkloads`、`admittedWorkloads` 和已运行任务。
- `message` 为 `Admitted by Kueue; waiting for training pod`：已放行但 Pod 未创建，检查
  Training Operator 和 PyTorchJob 事件。
- `message` 为 `Admitted but pod is not ready: ImagePullBackOff...` /
  `NotScheduled: ...`：按 4.2 或调度问题处理，不是队列问题。
- `workload_status` 长期 `Pending` 且配额空闲：检查 Kueue controller 日志、LocalQueue
  `default` 是否指向 `vla-cluster-queue`，以及 PyTorchJob 是否有
  `kueue.x-k8s.io/queue-name: default` 标签和合法优先级：

```bash
kubectl -n kueue-system logs deployment/kueue-controller-manager --tail=200
kubectl -n vla-training get pytorchjob <job-name> -o yaml | grep -E 'queue-name|priority-class'
```

处理动作：配额满就等待或取消运行中任务；Kueue 未接管就修 controller/标签；Pod 已建但
Pending 就查调度。不要删除 Workload 或去掉 Kueue 标签来绕过排队，这会破坏配额语义。

### 4.4 任务 RUNNING 但日志为空

判断依据：

```bash
kubectl -n vla-training get pytorchjob,pod -o wide
kubectl -n vla-training describe pod <pod-name>
kubectl -n vla-training logs <pod-name> -c pytorch --tail=500
kubectl -n vla-training logs <pod-name> -c pytorch --previous --tail=500
curl -fsS "http://127.0.0.1:30080/api/v1/jobs/<job-name>/logs?tail_lines=2000" \
  | python3 -m json.tool
```

- `QUEUED` 且 `pod_name: null`：还没有 Pod，空日志是正常的；先看 4.3。
- Pod `Running`、容器在跑但没有输出：训练程序可能缓冲 stdout，或根本没有打印。设置
  `PYTHONUNBUFFERED=1`，并确认代码写 stdout/stderr。
- 容器已 `Terminated`：用 `--previous` 取退出前日志；退出码 0 但无输出说明程序本身没打印。
- 日志被截断：API 默认只取尾部 500 行，按需提高 `tail_lines`，不要无限拉取。

处理动作：修训练命令/缓冲设置；已删除 Pod 的 Kubernetes 日志无法从平台恢复，取消历史
只保存提交参数，不保存永久日志。

### 4.5 GPU 显示可用但任务起不来

判断依据：

```bash
nvidia-smi
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
ps -o user,pid,etime,cmd -p <pid>
kubectl get node -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'
kubectl -n vla-training describe pod <pod-name>
kubectl -n vla-training logs <pod-name> -c pytorch --tail=500
```

判断：

- `allocatable` 为 0：Device Plugin 没发布 GPU，先修插件，不是任务问题。
- `allocatable` 为 1 但 Pod `Pending` 且事件是 `Insufficient nvidia.com/gpu`：节点上已有
  别的 Pod 占用，或配额/调度器统计不一致。
- Pod 已 Running 但日志出现 `CUDA out of memory`、`no CUDA-capable device`：GPU 被平台外
  进程占用，或 runtime/驱动不兼容。编写本手册时本机就有一个非 Kubernetes 的 Ray 进程占用约 38 GB，
  这正是“Kubernetes 显示空闲、任务实际跑不起来”的典型原因。
- `nvidia-smi` 里进程的 USER 不是运行容器的账号时，更要先确认谁在使用 GPU；不要直接
  kill 别人的训练进程。

处理动作：与占用者协调释放 GPU；确认训练容器 `requests` 与 `limits` 都是同一个
`nvidia.com/gpu` 整数；确认使用 `runtimeClassName: nvidia`；修 Device Plugin 后等节点
重新发布容量。不要用特权容器或宿主机 Docker Socket 绕过。

### 4.6 Kueue 配额被占满

判断依据：

```bash
kubectl get clusterqueue vla-cluster-queue -o yaml
kubectl -n vla-training get workload -o wide
kubectl -n vla-training get pytorchjob,pod -o wide
curl -fsS http://127.0.0.1:30080/api/v1/queues | python3 -m json.tool
```

`status.pendingWorkloads > 0`、`flavorsUsage[0].resources[0].total` 等于
`spec.resourceGroups[0].flavors[0].resources[0].nominalQuota`，说明 GPU 配额确实被占满。当前
`preemption.withinClusterQueue: Never`，高优先级任务不会抢占低优先级任务，只会继续排队。

处理动作：等待运行中任务结束，或取消它：

```bash
cli/vla-train --server http://127.0.0.1:30080 --token local cancel <job-name>
kubectl -n vla-training get pod -o wide
```

取消后 Pod 走 `terminationGracePeriodSeconds`（默认 30 秒，提交时的
`termination_grace_period_seconds` 可调）才释放 GPU；宽限期 5 秒时排队任务通常约 8 秒
开始运行，默认 30 秒时约 32 秒。不要通过把 `nominalQuota` 改到超过物理 GPU 数来“解决”
排队；那会让多个任务争抢同一张卡。

### 4.7 checkpoint 没落盘

判断依据：

```bash
kubectl -n vla-training get pod <pod-name> -o jsonpath='{range .spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' | grep CHECKPOINT
kubectl -n vla-training logs <pod-name> -c pytorch --tail=500 | grep -i checkpoint
ls -la /data/vla-platform/checkpoints/<job-name>
df -h /data
```

平台把 `CHECKPOINT_DIR` 固定为 `/checkpoints/<job-name>`，并把宿主机
`/data/vla-platform/checkpoints` 挂到 `/checkpoints`。逐项判断：

- 环境变量不是 `/checkpoints/<job-name>`：检查提交参数是否覆盖了 `CHECKPOINT_DIR`
  （平台会过滤用户传入值并强制注入，正常不会出现）。
- 宿主机目录不存在或为空：训练代码没写、写到了别处，或任务在保存前被取消/失败。
- 日志有 `Permission denied`：容器内用户对目录不可写；当前镜像以 root 运行，若自定义
  镜像改了用户，需要匹配目录权限。
- `df -h` 接近满：先清理/扩容，再重跑。
- 任务被取消：宽限期内没保存完就会丢；给关键任务设置更大的
  `termination_grace_period_seconds`。

处理动作：修训练代码的保存路径和原子写（临时文件 + rename），确认目录存在且可写，
释放磁盘后重跑。不要用 `chmod 777` 掩盖权限问题；不要删除别人的 checkpoint 目录。

### 4.8 NodePort 不通

判断依据：

```bash
# 节点本机
curl -v -m 3 http://127.0.0.1:30080/api/v1/health

# 局域网另一台机器（把 <node-ip> 换成节点地址）
curl -v -m 3 http://<node-ip>:30080/api/v1/health

kubectl -n vla-platform get svc vla-web -o wide
kubectl -n vla-platform get endpointslice -l kubernetes.io/service-name=vla-web -o wide
kubectl -n vla-platform get pods -l app.kubernetes.io/component=web -o wide
sudo ufw status
sudo journalctl -u k3s -n 100 --no-pager
```

判断：

- 节点本机通、局域网不通：防火墙/安全组/网络 ACL 拦截，或客户端路由/VLAN 不对。
  当前 `ufw` 为 inactive，本机没有主机防火墙规则；如果上游还有安全组/ACL，仍需单独检查。
- 本机也不通：Service、Endpoints、Pod 或 k3s 进程问题。`endpointslice` 的
  `ENDPOINTS` 为空通常表示 Pod 未 Ready 或 Service selector 不匹配。
- Service 的 `nodePort` 不是 30080：检查 `deploy/web/base/service.yaml` 是否被覆盖。
- k3s 服务异常：看 `journalctl -u k3s`，节点是否 Ready。

处理动作：只向可信内网开放 TCP 30080；确认 `kubectl get svc vla-web` 的
`PORT(S)` 为 `80:30080/TCP`，`endpointslice` 有 Pod IP。若是主机防火墙，可用
`sudo ufw allow from <内网网段> to any port 30080 proto tcp`，但 K3s 的 DNAT 可能绕过
部分 UFW 规则，改完必须从另一台机器实测。不要用 hostNetwork 或直接把 Web 改成
LoadBalancer 来绕过网络问题。

## 5. 扩容到多节点前的检查清单

当前架构大量假设“只有一台机器”。下面每一条都只在单节点成立，扩容前必须逐项改造并
验证，不能直接把节点加进集群。

| 当前假设 | 多节点下的问题 | 扩容前动作 |
|---|---|---|
| 历史库 PV `vla-web-history` 是 `hostPath` + `ReadWriteOnce`，且没有 nodeAffinity | Web 可能被调度到另一台节点，看到空目录并在那里新建空库，表现为历史“丢失” | 改成共享存储（NFS/CephFS 等 RWX），或给 PV/Web 加节点亲和并确保只在一台节点运行 |
| 数据集和 checkpoint 用 `hostPath` 直接挂载，模板没有 `nodeSelector`/affinity | 训练 Pod 可能落到没有 `/data/vla-platform/datasets` 的节点，启动时报 hostPath 类型检查失败或读不到数据 | 所有节点准备同一路径的数据，或改用共享存储；给训练 Pod 加节点标签/亲和 |
| 镜像只 import 到本机 containerd，没有 registry | 新节点没有 `vla-platform:local`/`vla-smoke:latest`，Pod 一直 `ImagePullBackOff` | 每个节点预导入，或搭建内网 registry 并把 `imagePullPolicy`/镜像名改成 registry 地址 |
| ResourceFlavor `gpu` 的 `spec: {}`，不区分节点和 GPU 型号 | Kueue 只按总配额计数，可能放行一个任何单节点都放不下的 8 卡任务，随后 Pod Pending；4090 和 A100 也会混排 | 给节点打标签，ResourceFlavor 加 `nodeLabels`，按型号拆分 flavor/ClusterQueue |
| `nominalQuota` 是单个数字，等于当前机器的卡数 | 加节点后配额不会自动变大，仍按 1/8 卡排队 | 按所有节点可用 GPU 之和设置配额，并确认 Kueue 能感知新节点容量 |
| Web 单副本 + SQLite 单文件 | 不能简单把 Web 扩成 2 副本；两个进程写同一个 SQLite 会锁冲突/损坏 | 保持单副本，或把历史库迁到支持并发的数据库；Web 用反亲和/单节点约束 |
| NodePort 只验证过当前节点 IP | 多节点时客户端应访问 VIP/负载均衡，而不是某台节点；节点故障会中断 | 在前面加 LB/VIP，或至少让客户端使用稳定的节点地址并做健康检查 |
| 备份脚本按单机路径执行 | 各节点的 `/data` 是独立的，只备份一台会漏数据 | 为每个节点建立备份任务，或统一到共享存储后只备份一份 |
| Device Plugin、Kueue、Training Operator 是集群级组件 | 新节点需要 Device Plugin 就绪、GPU 容量发布后才能被 Kueue 统计 | 新节点先跑 `nvidia-smi`、Device Plugin DaemonSet、`allocatable` 检查，再加入队列 |

扩容前至少执行并记录：

```bash
kubectl get nodes -o wide
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'
kubectl get pv vla-web-history -o yaml
kubectl get resourceflavor gpu -o yaml
kubectl get clusterqueue vla-cluster-queue -o yaml
kubectl -n kube-system get pods -o wide
kubectl -n nvidia-device-plugin get ds -o wide
```

重点确认：PV 是否有节点亲和、训练模板是否加了亲和、每个节点的镜像是否存在、所有节点
的 GPU 容量之和是否等于计划配额、异构 GPU 是否需要拆分 flavor。

## 6. 安全提示

### 6.1 当前状态和风险

截至编写时，4090 overlay 设置 `PLATFORM_AUTH_REQUIRED=false`，Web 以单容器运行，
NodePort `30080` 监听在节点所有地址上，本机 `ufw` 为 inactive。在没有上游 ACL/安全组的
情况下，同一局域网内任何人都能直接访问网页和 `/api/v1`，不需要令牌。

风险包括：

- 任意人可提交/取消/删除训练任务，占用 GPU，造成排队阻塞或资源争抢。
- 训练任务可以指定镜像和命令，默认镜像以 root 运行；虽然平台不允许任意宿主机路径，但容器内
  能读 `/datasets`、写 `/checkpoints`，可窃取数据集、篡改或删除他人的 checkpoint。
- 平台侧没有用户身份和操作审计，无法区分谁提交、谁取消；SQLite 历史只记录任务参数，不记录操作者。
- Web ServiceAccount 能管理 `vla-training` 的 PyTorchJob 和读取 Pod 日志；一旦 Web 被
  利用，影响范围至少覆盖整个训练命名空间。
- 本机 `/etc/rancher/k3s/k3s.yaml` 权限为 `0644`，任何本地用户都能获得集群管理权限。
  这与平台鉴权是两件事，不能互相替代。

因此 `PLATFORM_AUTH_REQUIRED=false` 只允许用于可信内网和短期验收，不应作为长期生产配置。

### 6.2 开启鉴权

1. 确认 `vla-web-auth` Secret 存在；如果令牌已经丢失，直接生成新的并轮换：

```bash
kubectl -n vla-platform get secret vla-web-auth
export PLATFORM_API_TOKEN="$(openssl rand -hex 32)"
```

2. 把 `deploy/web/overlays/4090/deployment.yaml` 中的
   `PLATFORM_AUTH_REQUIRED` 从 `"false"` 改为 `"true"`。这是唯一需要改的配置项，
   不需要改镜像或代码。

3. 重新部署（会 apply Secret、更新 Deployment 并等待 rollout）：

```bash
cd /home/jjx/vla-train-platform
PROFILE=4090 bash scripts/deploy-web.sh
```

如果只是切鉴权、不轮换令牌，可以省略 `PLATFORM_API_TOKEN`；脚本会复用已有
`vla-web-auth` Secret。提供新令牌会更新 Secret 并重启 Web，所有客户端必须同步换令牌。

4. 验证：

```bash
curl -fsS http://127.0.0.1:30080/api/v1/health
# {"status":"ok","auth_required":true}

curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:30080/api/v1/config
# 401

curl -fsS -H "Authorization: Bearer $PLATFORM_API_TOKEN" \
  http://127.0.0.1:30080/api/v1/config
# 200
```

5. 更新客户端：CLI 执行
   `cli/vla-train config --server http://<node-ip>:30080 --token "$PLATFORM_API_TOKEN"`，
   浏览器在 `/login` 输入令牌。不要把令牌写进仓库、脚本、命令行历史或聊天记录；用完
   清理 shell 变量。

### 6.3 网络与主机加固

- 只允许可信内网访问 TCP 30080；在交换机 ACL、云安全组或主机防火墙上限制来源，
  改完从另一台机器实测，不要只在节点本机 `curl`。
- 不要把 NodePort 暴露到公网；不要把 Web 改成 `LoadBalancer` 或 `hostNetwork` 来绕过限制。
- 定期轮换 `PLATFORM_API_TOKEN`，轮换后旧客户端会 401，属预期。
- 修复 kubeconfig 权限（可选，但会改变本机非 root 用户的 kubectl 能力）：
  `sudo chmod 600 /etc/rancher/k3s/k3s.yaml`。当前为 0644，任何本地用户都可管理集群。
- 不要给 Web ServiceAccount 扩到 cluster-admin；当前 Role 只覆盖 `vla-training` 的
  PyTorchJob、Pod、日志、事件和只读队列，保持最小权限。
- 训练镜像以 root 运行。若自定义镜像，尽量使用非 root 用户，并确保
  `/checkpoints/<job-name>` 对该用户可写；不要用特权容器或挂载宿主机 Docker Socket。