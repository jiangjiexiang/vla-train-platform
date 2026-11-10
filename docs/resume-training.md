# 断点续训：从「假功能」到真正可用

> 2026-11-10　对应提交 be62271 → 395517b

## 一、为什么单独写一份

断点续训在需求清单里只是「训练中断后能接着跑」一句话。实际上它前后修了
**五轮**，每一轮的现象都是「训练能跑，但结果不对」，**没有任何报错**。
这类 bug 比崩溃更难发现，也更危险——用户以为省下了几小时算力，其实没有。

记录下来，是因为下一次改这块代码的人会踩同样的坑。

## 二、五个坑

### 坑 1：勾了续训，实际还是从头训练

lerobot 从 `<输出目录>/checkpoints/last` 找续训点。平台每次提交都生成
新的任务名和新的输出目录，那里空无一物。训练照常跑完，只是从第一步开始。

**修复**：新增 `resume_from` 参数，提交时把源任务的 checkpoint 复制过来。

### 坑 2：复制过去后必定崩溃

```
ValueError: Optimizer config is required but not provided in TrainPipelineConfig
```

lerobot 0.4.4 的分流逻辑：

```python
if policy_path:        # 传了 --policy.path 走这里
    self.policy = from_pretrained(policy_path)
elif self.resume:      # 只有不传 policy.path 才会进来
    self.checkpoint_path = policy_dir.parent
```

训练脚本一直无条件传 `--policy.path`，所以续训分支永远不执行，
`checkpoint_path` 恒为 None；后面 `elif ... and not self.resume` 又跳过了
建 optimizer，于是 optimizer 也是 None，直接崩在第一步。

**修复**：续训时不传 `--policy.path`，改用 `--config_path` 指向
checkpoint 里的 `train_config.json`。

### 坑 3：复制 `last` 软链接得到断链

源目录的 `last` 是指向 `003000` 的软链接。直接复制得到一个断链，
`train_config.json` 读不到。

**修复**：解析真实目录再复制，并在目标端重建软链接。

### 坑 4：指标步数从 1 重新开始

续训任务的 `metrics.jsonl` 从 1 计数，图上变成一条新曲线，
看起来像"又从第一步开始了"。

**修复**：`VLA_STEP_OFFSET`，续训时从 checkpoint 读出起始步数。

### 坑 5：偏移被不断累加

坑 4 的修复引入了新 bug：`_vla_step` 存的是**带偏移后**的值，
下一步又加一次偏移，步数一路累加。实测出现 `step 150050`。

**修复**：`_vla_step` 只存相对计数，偏移在写记录时再加。

> 这个 bug 是自己修 bug 时引入的，只有真跑一次才暴露——
> 单看代码很难分辨"存的是相对值还是绝对值"。
> 教训：**偏移量这类东西，存相对、算绝对，别混。**

## 三、最终验证

从 `smolvla-pick1028-003`（step 3000）续到 3050：

```
任务: smolvla-pick1028-007
状态: SUCCEEDED，43 秒
点数: 50
step 范围: 3001 -> 3050      ← 曲线正确衔接
首个 loss: 0.0805            ← 权重已恢复（首次训练是 0.99 起步）

产物: 003000  003050  last
日志: 续训：起点 step 3000
     续训：已从 smolvla-pick1028-003 复制 checkpoint 003000
     续训：使用配置 .../003000/pretrained_model/train_config.json
```

## 四、一个必须知道的行为差异

`resume=true` 时 lerobot 使用 checkpoint 里的 `train_config`，
命令行传的 `steps`、`lr`、`batch_size` 会被**静默忽略**。
这是框架的既定行为，不是平台丢参数。想改这些值只能新建一次训练。

## 五、怎么用

任务详情页点「续训」按钮，会自动填好续训开关与来源任务，
用户只需确认步数。

## 六、这轮暴露的一个流程问题

验收过程中出现过两次「功能明明是好的，但验收脚本挂了」——
因为脚本里硬编码的任务名对应的数据已被清理。

教训：**验收脚本不能依赖偶然存在的测试数据**。
应该先跑一个基线任务，再用它的名字去验收。
