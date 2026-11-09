# scripts/ 目录说明

运维脚本。全部为只读或显式确认后才写集群；`bash -n` 全部通过。

## 已有脚本（v0.8）

| 脚本 | 作用 |
|---|---|
| `install-k3s-gpu.sh` | 安装 K3s + NVIDIA 运行时 |
| `install-stack.sh` | 安装 NVIDIA Device Plugin / Training Operator / Kueue，应用单机 profile |
| `deploy-web.sh` | 导入镜像、创建 Secret、应用 `deploy/web/overlays/<PROFILE>` |
| `verify-4090.sh` / `verify-a100.sh` | 单机环境巡检与 E2E |
| `preflight.sh` | 装机前只读检查（**本次新增多机检查项**） |
| `cleanup-jobs.py` / `lint.sh` / `test.sh` / `release.sh` | 维护类 |

## 新增脚本（v0.9 多机 / 高可用分区）

| 脚本 | 是否写集群 | 作用 |
|---|---|---|
| `verify-multi-node.sh` | 否（dry-run 也不写 etcd） | 多机 + HA 全套**静态校验**：`bash -n`、`py_compile`、`kustomize build`、结构断言、rendezvous 场景、preflight 分支、可选 `apply --dry-run` |
| `check-smoke-train-rendezvous.py` | 否 | 假 torch 驱动 `train.py`，验证 8 个 rendezvous 场景（多机 rank0/rank1、torchrun、单机、缺参数报错） |
| `check-preflight-multinode.sh` | 否 | 假 kubectl 验证 `preflight.sh` 的多机分支（一致 / 部分缺失 / 不一致必须 FAIL） |
| `deploy-multi-node-ha.sh` | **默认 dry-run，`--apply` 才写** | 打节点多机标签、应用 `deploy/kueue/multi-node`、可选叠加 Web HA、校验并给回滚命令 |

### `verify-multi-node.sh`（最常用）

```bash
bash scripts/verify-multi-node.sh                  # 纯静态，不需要集群
bash scripts/verify-multi-node.sh --apply-dry-run  # 额外做 client/server dry-run（不写 etcd）
```

输出 PASS/WARN/FAIL 汇总，FAIL 时退出码 1。

### `preflight.sh` 的多机检查项

```bash
PROFILE=4090 bash scripts/preflight.sh               # 默认包含多机检查
MULTINODE_CHECK=0 bash scripts/preflight.sh          # 跳过多机组
```

新增的 `== Multi-node readiness (read-only) ==` 段（契约第 4 节第 5 条）：

1. **每节点 GPU 数**：读 `status.allocatable["nvidia.com/gpu"]`，≥2 个 GPU 节点 → PASS，
   否则 WARN（单机是预期结果，**不 FAIL**，保证现有单机巡检照常通过）。
2. **跨节点版本一致性**：读节点的 `nvidia.com/cuda.driver.*` 标签 / `nvidia.com/cuda.runtime-version.full`，
   全部一致 → PASS；部分节点缺信息 → WARN；**数值不一致 → FAIL**（NCCL 跨节点会不可靠）。
3. **Kueue Topology 配置**：Topology 是否存在、带 `vla-platform.io/node-pool=multinode`
   标签的节点是否有对应的 TAS `ResourceFlavor`（有标签无 flavor → **FAIL**）。
4. **TAS feature gate**：只读 controller 的启动参数，不做任何重启。

全部只读：`kubectl get` / `-o jsonpath`，不 `apply`、不 `label`、不重启。

### `deploy-multi-node-ha.sh`

```bash
LABEL_NODES="node-1 node-2" bash scripts/deploy-multi-node-ha.sh              # dry-run
sudo LABEL_NODES="node-1 node-2" bash scripts/deploy-multi-node-ha.sh --apply
sudo bash scripts/deploy-multi-node-ha.sh --with-ha --apply                   # 附带 Web HA
```

**不会**重启 kueue-controller-manager。启用 TAS gate 是单独的手工步骤，
见 `deploy/kueue/multi-node/README.md` 第 3.4 节。

## 注意

- 本仓库开发机是单机单卡，`verify-multi-node.sh` 只能证明配置正确，
  **不能**证明多机可用。真实验证清单见 `deploy/kueue/multi-node/README.md` 第 5 节。
- `preflight.sh` 的多机分支靠节点标签判断版本一致性；如果集群里 device plugin 不上报版本，
  会 WARN 提示人工 `nvidia-smi` 比对，不会伪造数据。
- `py_compile` 会生成 `__pycache__`；`verify-multi-node.sh` 在渲染测试前会清理，
  否则 `import` 可能加载到陈旧字节码。