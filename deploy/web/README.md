# Web 部署清单

平台 Web（FastAPI + React，单容器镜像 `vla-platform:local`）的 Kubernetes 清单。

## 结构

```
deploy/web/
├── base/                      所有 profile 的公共部分
│   ├── deployment.yaml        1 副本 + Recreate + /api/v1/health 探针 + 拓扑分布
│   ├── service.yaml           NodePort 30080，按 component=web 选择 Pod
│   ├── pdb.yaml               maxUnavailable: 0（kustomization 在单机下改成 1）
│   └── kustomization.yaml     资源列表 + PDB patch
└── overlays/
    ├── 4090/                  PLATFORM_AUTH_REQUIRED=false
    ├── a100/                  PLATFORM_ALLOWED_GPUS=1,2,4,8
    └── ha/                    ★ 高可用：2 副本 + RollingUpdate + PDB minAvailable=1 + 反亲和
```

## base 里的两个可用性策略（不破坏单机）

1. **`topologySpreadConstraints`**（DoNotSchedule, `minDomains: 2`, `maxSkew: 1`）
   - 多节点时把 Web 副本分散到不同节点；
   - 单节点时 `minDomains` 让约束自动放宽，第二个副本不会 Pending，`replicas: 1`
   的现有部署行为完全不变。
2. **`PodDisruptionBudget vla-web`**
   - 文件里写 `maxUnavailable: 0`（官方推荐的「不阻塞节点排空」写法）；
   - 但 1 副本 + `maxUnavailable: 0` 仍然等于「不可排空」，会把单机 `kubectl drain` 卡死，
     所以 `base/kustomization.yaml` 在单机 profile 下把它 patch 成 `maxUnavailable: 1`；
   - `overlays/ha` 用 JSON6902 patch 换成 `minAvailable: 1`（2 副本下才真的有保护意义）。

> kustomize 不允许两个资源占用同一个 id，所以 HA 的 PDB 不放在单独的 `pdb.yaml` 里，
> 而是在 `overlays/ha/kustomization.yaml` 里 patch base 的那个 PDB。

## 应用

```bash
kubectl apply -k deploy/web/overlays/4090          # 单机 1 卡（默认）
kubectl apply -k deploy/web/overlays/a100          # 单机 8 卡
kubectl apply -k deploy/web/overlays/ha            # 在已应用 profile 之后叠加高可用
```

或使用 `scripts/deploy-web.sh`（会顺带处理镜像导入、API Token Secret、PVC）。

## 验证

```bash
kubectl kustomize deploy/web/base                    # 渲染
kubectl -n vla-platform get deploy,pod,svc,pdb -o wide
curl -fsS http://<node-ip>:30080/api/v1/health       # {"status":"ok","auth_required":...}
kubectl -n vla-platform describe pdb vla-web
```

`/api/v1/health` 是实测存在的端点（返回 `{"status":"ok","auth_required":false}`），
readiness/liveness 都用它；不要改成 `/health`（不存在）。

## 回滚

```bash
kubectl apply -k deploy/web/overlays/4090            # 从 ha 退回单副本
kubectl -n vla-platform rollout undo deployment/vla-web
```

## 高可用说明

方案选型、SQLite 单写者边界、跨节点会分裂历史库的原因、回滚步骤：
见 [`overlays/ha/README.md`](overlays/ha/README.md)。