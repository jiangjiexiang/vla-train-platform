# Web 高可用 overlay

`deploy/web/overlays/ha` 把平台 Web（FastAPI + React）从「单副本、Recreate、升级有秒级中断」
改成 **2 副本 + 滚动升级 + PDB + 反亲和 + 跨节点拓扑分布**。

它和 `overlays/4090` / `overlays/a100` 是**叠加**关系，不是替换：

```bash
kubectl apply -k deploy/web/overlays/4090   # 先应用硬件 profile
kubectl apply -k deploy/web/overlays/ha     # 再叠加高可用
```

只叠 `ha` 而忘了硬件 profile 时，会退回 base 的默认 `PLATFORM_ALLOWED_GPUS=1`。

---

## 1. 选了哪种方案，以及为什么

契约第 4 节给了两条路：**双副本共享存储** 或 **Recreate + 明确说明**。本仓库选的是
**「Recreate 单副本为默认 + 独立 HA overlay（2 副本）」**，理由：

1. **历史库是 SQLite 单写者**，而且落在 hostPath 的单节点 PV 上
   （`deploy/base/storage.yaml` 的 `vla-web-history`，`ReadWriteOnce`、
   `storageClassName: manual`）。没有 RWX 共享存储可用时，两个副本同时写会损坏数据库；
   K3s 默认的 `local-path` 也只在单节点可用。
2. **本环境是单节点 K3s**。强行把 base 改成 2 副本，第二个副本只能落在同一节点，
   既拿不到任何冗余，又会让 `ReadWriteOnce` 卷在两个 Pod 间争抢（第二个 Pod 直接
   `Multi-Attach` / 挂载失败），等于把可用性做低。
3. 因此 HA 必须**显式选择**：在真有 ≥2 个节点、且历史写入可以容忍
   （或已迁移到外部 DB / 共享存储）时，再叠 `overlays/ha`。

> ### ⚠️ SQLite 单写者边界（必须读）
> - 历史库读写仍然是 **同一台节点的同一个文件**。`overlays/ha` **不会**、也无法把
>   SQLite 变成多写者。
> - **多副本下历史写入需要迁移到共享存储或外部 DB**（例如 Postgres）之后才算真正安全。
>   在本仓库当前代码里，`sqlite3.connect(path, timeout=10)` 由文件锁串行化，
>   两个副本在**同一节点**共享同一 hostPath 时通常能工作但会互相等待；
>   在**不同节点**上，两个副本根本看不到对方的文件 —— 历史记录会分裂成两份。
> - **结论**：`overlays/ha` 只解决「Web 进程级别」的高可用（进程崩了/滚动升级不断服）。
>   它**不解决**历史数据的一致性与单点问题。跨节点部署 `overlays/ha` 之前，
>   必须先把 `PLATFORM_DB_PATH` 指向外部数据库或 RWX 存储，并按 `docs/high-availability.md`
>   的步骤迁移。
> - `PLATFORM_DB_PATH` 仍指向 PVC 上的 `/data/history/platform.db`：
>   `overlays/ha` **不修改** `PLATFORM_DB_PATH`，保持与 base 一致，所有副本看到同一路径。

---

## 2. 这个 overlay 做了什么

| 项 | base / 单机 profile | `overlays/ha` |
|---|---|---|
| `replicas` | 1 | **2** |
| `strategy` | `Recreate` | **`RollingUpdate`（maxSurge=1, maxUnavailable=0）** |
| PDB | `maxUnavailable: 1`（不阻塞排空） | **`minAvailable: 1`（至少保留 1 个就绪）** |
| 反亲和 | 无 | `podAntiAffinity` **preferred**，weight 100，按 `kubernetes.io/hostname` |
| 拓扑分布 | `topologySpreadConstraints`（DoNotSchedule, minDomains=2，base 自带，单机自动放宽） | 继承 base |
| 探针 | `/api/v1/health`（readiness 宽松：failureThreshold=12） | 覆盖为更贴近 HA 的取值：readiness failureThreshold=3，liveness initialDelay=15 |
| 终止 | 无 | `preStop: sleep 5`，让 Service 端点先摘除 |
| Service | NodePort 30080 | **沿用同一个 NodePort**（kube-proxy 自动负载均衡） |

为什么反亲和是 **preferred** 而拓扑分布是 **DoNotSchedule**：

- 单机集群里 `required` 反亲和会让第二个副本**永远 Pending**，直接把 Web 弄挂；
- `topologySpreadConstraints` 带 `minDomains: 2`，单域时约束自动放宽，不会 Pending；
  多节点时它才真正生效。
- 两者叠加后：多节点优先分散、单节点优雅降级。

### 为什么没有新建「前置 Service」

`deploy/web/base/service.yaml` 已经是 `NodePort 30080` 且按
`app.kubernetes.io/component: web` 选择 Pod，2 副本时由 kube-proxy 自动分发，
所以不需要再加一层。**不要**在 overlay 里再定义一个 NodePort 不同的 Service ——
同一集群重复 NodePort 会冲突。如果确实需要内部 ClusterIP 入口，另建一个
`type: ClusterIP` 的 Service（不能用同一个名字 `vla-web`，kustomize 不允许同 id 两个资源）。

---

## 3. 怎么应用

```bash
kubectl kustomize deploy/web/overlays/ha            # 1. 先渲染看一眼
kubectl apply -k deploy/web/overlays/ha             # 2. 应用
kubectl -n vla-platform rollout status deployment/vla-web --timeout=300s
kubectl -n vla-platform get deploy,pod,pdb -o wide
```

脚本方式（默认 dry-run）：

```bash
scripts/deploy-multi-node-ha.sh --with-ha            # dry-run
sudo scripts/deploy-multi-node-ha.sh --with-ha --apply
```

### 应用前确认（单机环境）

```bash
kubectl get nodes | wc -l      # 只有 1 个节点时，第二副本会落在同一节点，
                               # 历史库是同一个 hostPath 文件，可接受但无冗余
kubectl -n vla-platform get pvc vla-web-history -o wide   # 确认 RWO 绑定
```

---

## 4. 怎么验证

```bash
# 1. 渲染断言（无需集群）：2 副本 / RollingUpdate / PDB minAvailable=1 / 探针路径
bash scripts/verify-multi-node.sh --apply-dry-run

# 2. 两个副本都就绪
kubectl -n vla-platform get pods -l app.kubernetes.io/component=web -o wide
#    期望 READY 2/2；NODE 列在多节点集群里应不同

# 3. PDB 生效
kubectl -n vla-platform get pdb vla-web -o jsonpath='{.status.disruptionsAllowed}{"\n"}'
#    期望 1（= replicas - minAvailable）

# 4. 滚动升级不断服
kubectl -n vla-platform set env deployment/vla-web VLA_ROLLOUT_PROBE="$(date +%s)"
kubectl -n vla-platform rollout status deployment/vla-web --timeout=300s
curl -fsS http://<node-ip>:30080/api/v1/health

# 5. 主动干扰受保护（drain 一个节点时另一个副本继续服务）
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --dry-run=client
```

---

## 5. 回滚

```bash
# 回到单副本 Recreate（不删 PDB；PDB 变成 maxUnavailable: 1，不阻塞排空）
kubectl apply -k deploy/web/overlays/4090

# 发布过程中发现问题时立刻回到上一个 ReplicaSet
kubectl -n vla-platform rollout undo deployment/vla-web

# 确认副本数已回退
kubectl -n vla-platform get deployment vla-web -o jsonpath='{.spec.replicas} {.spec.strategy.type}{"\n"}'
```

注意：`kubectl apply -k deploy/web/overlays/4090` 会把 `replicas` 改回 1 并把 strategy
换回 `Recreate`，因为 base 里这两个字段是显式值，apply 会覆盖。回滚后建议再跑一次
`scripts/verify-multi-node.sh` 确认 PDB 是 `maxUnavailable: 1`。

---

## 6. 已知限制

- 单节点集群：2 副本在同一节点，节点宕机仍然整体不可用（物理上没有冗余）。
- 历史库：没有迁移到共享/外部存储前，多副本在不同节点会写出两份历史（见第 1 节）。
- 会话令牌（v0.9 的 `/auth/login`）存放在同一个 SQLite 里，因此**跨节点**多副本时
  登录态不共享：用户在 A 节点登录，请求落到 B 节点会 401。迁移到外部 DB 后这个问题
  一起解决；在那之前请勿跨节点部署 `overlays/ha`。