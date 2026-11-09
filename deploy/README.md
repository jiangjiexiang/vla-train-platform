# deploy/ 目录说明

Kubernetes/K3s 部署清单。所有清单都用 kustomize 组织，`kubectl apply -k <dir>` 应用。

## 目录结构

```
deploy/
├── base/                     命名空间、RBAC、历史库 PV/PVC、GPU 指标
├── kueue/
│   ├── base/                 单机共用：WorkloadPriorityClass(low/normal/high)、两个 Kueue 配置副本
│   ├── 4090/                 单机 1 卡配额（nominalQuota: 1）
│   ├── a100/                 单机 8 卡配额（nominalQuota: 8）
│   └── multi-node/           ★ 多机 profile（Topology + TAS + 多机队列），与上面互不影响
└── web/
    ├── base/                 Web Deployment + NodePort Service + PDB + 拓扑分布
    └── overlays/
        ├── 4090/             1 卡硬件 profile
        ├── a100/             8 卡硬件 profile
        └── ha/               ★ Web 高可用（2 副本 + 滚动升级 + PDB + 反亲和）
```

★ 标记的是 v0.9 多机/高可用分区新增的目录，各自有 README。

## 常用命令

| 目的 | 命令 |
|---|---|
| 渲染不应用（推荐先做） | `kubectl kustomize deploy/kueue/multi-node` |
| 客户端 dry-run | `kubectl apply --dry-run=client -k deploy/web/overlays/ha` |
| 服务端校验（CRD schema + webhook，不写 etcd） | `kubectl apply --dry-run=server -k deploy/kueue/multi-node` |
| 应用单机配额 + Web | `kubectl apply -k deploy/kueue/4090 && kubectl apply -k deploy/web/overlays/4090` |
| 一键部署 Web | `scripts/deploy-web.sh`（见 `scripts/README.md`） |
| 静态全量校验 | `scripts/verify-multi-node.sh --apply-dry-run` |

## 三条部署线的关系

```
单机（默认，已有部署，不要乱动）
  deploy/base + deploy/kueue/{4090,a100} + deploy/web/overlays/{4090,a100}

多机（新增，需 ≥2 个 GPU 节点 + TAS feature gate）
  deploy/base + deploy/kueue/multi-node + （Web 仍用 overlays/4090）

Web 高可用（新增，可叠加在任意硬件 profile 上）
  deploy/web/overlays/4090 + deploy/web/overlays/ha
```

三条线**不共享对象名**：单机队列 `vla-cluster-queue` / flavor `gpu`，多机队列
`vla-multinode` / flavor `gpu-multinode`，因此可以在同一个集群里共存。

## 重要约束

- **不要**在没有 mask 的情况下 `kubectl apply -k deploy/kueue/multi-node` 到单卡生产集群：
  它会新增一个匹配不到任何节点的 ResourceFlavor 和空队列，虽然无害但会造成困惑。
  多机配置请在带标签的 ≥2 节点集群上应用，步骤见 `deploy/kueue/multi-node/README.md`。
- 修改 `deploy/base/deployment.yaml` 会影响所有 Web profile，改完至少跑一次
  `kubectl kustomize deploy/web/overlays/4090` 与 `overlays/ha` 确认两者都能渲染。
- Web 历史库是 hostPath 上的 SQLite（RWO），多副本的边界见 `deploy/web/overlays/ha/README.md`。