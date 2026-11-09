# Kueue 队列与多机 profile

## 结构

```
deploy/kueue/
├── base/
│   ├── kustomization.yaml              只含 WorkloadPriorityClass
│   ├── workload-priority-classes.yaml  low=100 / normal=200 / high=300
│   ├── controller_manager_config.yaml  服务器上实际生效的 Kueue 配置（install-stack.sh 用它补 PyTorchJob 集成）
│   └── kueue-values.yaml               Helm values 版本（与上一份内容一致）
├── 4090/     单机 1 卡：ResourceFlavor gpu + ClusterQueue vla-cluster-queue(quota 1) + LocalQueue default
├── a100/     单机 8 卡：同上，quota 8
└── multi-node/  ★ 多机：Topology vla-multinode + ResourceFlavor gpu-multinode + ClusterQueue vla-multinode + LocalQueue multinode
```

单机 profile（4090/a100）**不引用** `multi-node/`，`install-stack.sh` 也不会碰它。
两者的对象名互不相同，可以共存。

## 单机 profile

```bash
kubectl apply -k deploy/kueue/4090
kubectl get clusterqueue vla-cluster-queue -o yaml | grep -A3 nominalQuota
kubectl -n vla-training get localqueue default
```

`nominalQuota` 要等于节点上可分配的 `nvidia.com/gpu` 总数，否则会出现
「Kueue 认为有空闲、实际没有 Pod 能起来」或者反过来。

回滚：

```bash
kubectl apply -k deploy/kueue/4090      # 改坏配额时用仓库里的定义覆盖
kubectl delete -k deploy/kueue/4090     # 彻底移除单机队列（会影响提交，慎用）
```

## 多机 profile

见 [`multi-node/README.md`](multi-node/README.md)，要点：

- **Kueue v0.20.5 的 PodSet 拓扑字段是 `topologyRequest`，不是 `topology`**（实测 CRD）；
- TopologyAwareScheduling 默认关闭，需要 `kueue-values-multi-node.yaml` + **重启** controller；
- 反亲和只是防御，不能替代 TAS；
- 本仓库单机环境**没有真实多机验证过**，README 里给了完整的验证步骤清单。

## 优先级类

`low` / `normal` / `high` 是 `WorkloadPriorityClass`，由单机 profile 一并创建。
多机任务在 `labels: kueue.x-k8s.io/priority-class: <name>` 里指定，多机队列与单机队列
共享同一组优先级类，不需要重复创建。