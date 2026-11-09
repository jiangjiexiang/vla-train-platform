# 验收报告：真实训练闭环

> 生成时间：2026-11-10　版本：v1.1.0+

## 一、这份报告回答什么

平台要回答的核心问题只有一个：**选了数据集、点了开始，能不能真的训练出东西来？**

答案是可以。并且有可对比的证据，而不是"跑通了"三个字。

## 二、决定性证据：一次失败与一次成功的对照

| | `smolvla-pick1028-001` | `smolvla-pick1028-002` |
|---|---|---|
| 训练参数 | batch=8, workers=4, 500 步 | batch=8, workers=4, 500 步 |
| 镜像 | 同一个 | 同一个 |
| Pod 的卷 | datasets, checkpoints | **dshm, datasets, checkpoints** |
| 结果 | **FAILED（29 秒）** | **SUCCEEDED（193 秒）** |
| 失败原因 | 第 0 步 `DataLoader worker killed by signal: Bus error` | 无 |
| loss 点数 | 0 | **500** |

唯一差别是 `/dev/shm` 从容器默认的 64MB 变成 4Gi。这不是猜测——
两个任务的 Pod 定义可以直接 `kubectl get pytorchjob ... -o jsonpath` 对上。

## 三、500 步训练的健康指标

| 指标 | 数值 | 说明 |
|---|---|---|
| 总步数 | 500 | 完整跑完 |
| 耗时 | 193 秒（3.01 步/秒） | 无中断 |
| loss | 0.99 → 0.08 | 下降 91.9% |
| 分段均值 | 0.376 → 0.123 → 0.093 → 0.080 | 单调收敛 |
| grad_norm | 0.79 ~ 5.89，均值 1.40 | 无爆炸、无消失 |
| grad_norm 趋势 | 前 5 步 5.89→2.23，后 5 步 0.87→0.85 | 随收敛自然下降 |
| lr | 4.0e-05 → 2.5e-06 | 余弦退火生效 |
| 产物 | 2.46 GB，2 个 checkpoint | step 250 / step 500 |

指标是**流式写入**的：训练过程中 metrics.jsonl 从 0 涨到 137 → 274 → 410 → 500，
说明不是训练结束后一次性补写的。

## 四、全链路能力清单

| 能力 | 状态 | 验证方式 |
|---|---|---|
| 选数据集 → 点开始 | ✅ | `POST /runs` 只需 recipe_id + dataset_id |
| 自动生成命令 | ✅ | 参数正确拼进 lerobot-train |
| 提交前预检 | ✅ | 格式不兼容会被拦住 |
| 排队与准入 | ✅ | Kueue 管理，带位置与 ETA |
| 真实训练 | ✅ | 500 步，见上表 |
| 训练指标 | ✅ | loss / grad_norm / lr，可切换查看 |
| 产物管理 | ✅ | 列出文件、区分权重、显示大小 |
| 阶段时间轴 | ✅ | 7 阶段，含时间戳 |
| 失败诊断 | ✅ | 18 类诊断码，含本次的 Bus error |
| 审计 | ✅ | 记录操作者与来源 |
| 多人协作 | ✅ | 多令牌 + admin/user 隔离 |
| 任务回收 | ✅ | TTL 3600 秒 + 清理接口 |

后端测试 545 个，前端 167 个，验收脚本 20/20 通过。

## 五、几个诚实的说明

### 1. 我曾经把测试数据当成真实数据汇报过

早期冒烟镜像写出的 loss 是脚本生成的衰减序列，我一度说成"loss 曲线可用"。
真正的 SmolVLA 训练在那之后很久才产出第一条真实 loss。
假绿比真红危险，这一条记在这里。

### 2. 指标链路修了四轮才生效

模块路径错 → 父进程 hook 不传子进程 → 输出目录被平台预创建导致框架拒绝 →
产物写进子目录。每轮都以为修好了。
根因是**每轮只验证了"以为出错的那一环"**，没有端到端跑完整链路。

### 3. 30 步能跑通不代表系统能跑

30 步用的是 batch=2 / workers=2，在 64MB 共享内存下压力小。
换成 batch=8 / workers=4 立刻暴露问题。
**用小参数验证环境是没有意义的。**

### 4. 当前不适合直接对外的部分

- **鉴权默认关闭**：所有访问者都是 admin，能取消他人任务。多令牌已实现，
  改一个环境变量即可启用，但默认仍是 off（内网单机自用的取舍）
- **无高可用**：单节点、单副本 Web、SQLite。节点挂就是全挂
- **训练规模**：500 步约 3 分钟，验证了链路与收敛趋势，
  但没有跑过真正需要数小时的完整训练

## 六、下一步该做什么

按性价比排序：

1. **启用 token 鉴权**：改 `PLATFORM_USER_TOKENS`，立刻让审计有意义。
   半天工作量，收益最大。
2. **跑一次数小时的完整训练**：验证长时间稳定性（内存、磁盘、断点续训）。
   当前只有 3 分钟的样本。
3. **高可用**：需要多节点，是当前架构最大的单点。

## 附：如何自己复核

```bash
# 看两个任务的 Pod 定义差异
kubectl -n vla-training get pytorchjob smolvla-pick1028-001 \
  -o jsonpath='{.spec.pytorchReplicaSpecs.Master.template.spec.volumes[*].name}'
# 输出: datasets checkpoints

kubectl -n vla-training get pytorchjob smolvla-pick1028-002 \
  -o jsonpath='{.spec.pytorchReplicaSpecs.Master.template.spec.volumes[*].name}'
# 输出: dshm datasets checkpoints

# 看 500 步的 loss
curl -s "http://172.16.122.37:30080/api/v1/jobs/smolvla-pick1028-002/metrics?limit=500" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);l=[i['loss'] for i in d['items']];print(len(l),'点',sum(l[:20])/20,'->',sum(l[-20:])/20)"
```
