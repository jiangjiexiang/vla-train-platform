# openpi / π0 / π0.5 / π0-FAST 训练参数体系调研报告

> 调研基准：`Physical-Intelligence/openpi` 上游 **main @ `215abfb217dbac7d5f1273282331b9b1866c0479`**（commit message `docs(droid): fix config search instruction (#1023)`，GitHub commit date 2026-08-24）。
> 远端镜像 `/home/jjx/workspace/openpi` 的 HEAD 与该 SHA 一致（已通过 GitHub `/commits` API 核对，remote 为 `ghfast.top` 镜像）。
> 本报告中所有 flag 均通过**实际运行** `.venv/bin/python scripts/train.py <config> --help`（tyro 生成的 CLI）逐条核对，不依赖博客与二手资料。
> 无法确证的条目一律标注「**未核实**」，不做推断填充。

---

## 0. 结论速览（TL;DR）

| 问题 | 结论 |
|---|---|
| 最小可用训练命令 | `XLA_PYTHON_CLIENT_MEM_FRACTION=0.9 uv run scripts/train.py <config> --exp-name=<name> --overwrite`，但**前置必须**先跑 `uv run scripts/compute_norm_stats.py --config-name=<config>` |
| CLI 入口形态 | `train.py` **只有一个位置参数**（config 名，来自 `_CONFIGS`），其余全是 tyro 从 dataclass 自动派生的 `--xxx`；**没有** `--config-name` 这种 flag（`--config-name` 只存在于 `compute_norm_stats.py`） |
| 参数总量 | `TrainConfig` 共 26 个字段，其中 **1 个无 CLI**（`name`，`tyro.conf.Suppress`）、**1 个退化**（`policy_metadata` 显示为 `{fixed}` 不可改）；**`freeze_filter` 同样被 Suppress，CLI 上根本不存在 `--freeze-filter`** |
| 默认值陷阱 | `--data.extra-delta-transform` 的 help 永远打印 `(default: False)`，但真实值随 config 而定（`pi0_libero=True`、`pi05_libero=False`）——**不能把 help 文本当默认值** |
| norm stats 是否必须 | **LeRobot 数据集必跑**；缺失时 `data_loader.py:176-180` 直接 `ValueError`。**不能自动在训练容器内静默生成**，但可以在同一容器里先跑脚本（见 §3.3） |
| LeRobot 版本 | pin = `huggingface/lerobot @ 0cf864870cf29f4738d3ade893e6fd13fbd7cdb5`（2025-05-28），`CODEBASE_VERSION = "v2.1"`，**支持 v2.0 / v2.1（后者自动兼容前者）；不支持 v3.0** |
| 单卡 4090 48G 跑 pi05 | 官方口径：LoRA >22.5 GB、全量 >70 GB。**4090 上 pi05 只能走 LoRA**（`--model.paligemma-variant gemma_2b_lora`），但 `--freeze-filter` 不在 CLI 上，需改 config（见 §5） |
| 无 grad accum | 当前 `train.py` **没有梯度累积参数**（全局 batch = 每进程 batch，按 device 数均分） |

---

## 1. 训练入口与 CLI 形态

### 1.1 入口

`scripts/train.py` 末尾（`scripts/train.py:279-280`）：

```python
if __name__ == "__main__":
    main(_config.cli())
```

`src/openpi/training/config.py:978-979`：

```python
def cli() -> TrainConfig:
    return tyro.extras.overridable_config_cli({k: (k, v) for k, v in _CONFIGS_DICT.items()})
```

即：**`scripts/train.py <config_name> [flags]`**。config 名是位置参数，`--help` 会按该 config 的 `TrainConfig` 实例渲染出 flag 集合。

### 1.2 最小可用命令（官方 README 口径）

`README.md:154` 与 `README.md:159-160`（本地镜像行号，原文）：

```bash
# 1) 先算归一化统计量
uv run scripts/compute_norm_stats.py --config-name pi05_libero

# 2) 训练（--overwrite 用于覆盖已存在的 checkpoint 目录）
XLA_PYTHON_CLIENT_MEM_FRACTION=0.9 uv run scripts/train.py pi05_libero --exp-name=my_experiment --overwrite
```

最小信息集 = **3 项**：`<config_name>`（位置参数）、`--exp-name`（required，否则 `checkpoint_dir` property 抛 `ValueError("--exp_name must be set")`，`config.py:545-546`）、以及「checkpoint 目录已存在时必须二选一」的 `--overwrite` / `--resume`。

### 1.3 一个反直觉点：`--exp-name` 其实是 required

`config.py:471-472`：`exp_name: str = tyro.MISSING`。tyro 会把它渲染成 `(required)`，`--help` 实测确认。表单里必须标 `required: true`。

### 1.4 完整参数列表（`pi05_libero` 实测 `--help` 全量，按分组）

> 下列顺序即 tyro help 的实际输出顺序；"源码行号"指 `src/openpi/training/config.py` 中 `TrainConfig`/子 dataclass 的字段定义行。

**顶层（TrainConfig，`config.py:465-557`）**

| flag | 类型/取值 | 该 config 默认 | 源码行 |
|---|---|---|---|
| `--project-name` | str | `openpi` | 470 |
| `--exp-name` | str | **required** | 472 |
| `--pytorch-weight-path` | str\|None | `None`（pi05_libero 里为占位路径） | 483 |
| `--pytorch-training-precision` | `bfloat16`\|`float32` | `bfloat16` | 486 |
| `--ema-decay` | float\|None | `0.999` | 490 |
| `--assets-base-dir` | str | `./assets` | 499 |
| `--checkpoint-base-dir` | str | `./checkpoints` | 501 |
| `--seed` | int | `42` | 504 |
| `--batch-size` | int | `256` | 506 |
| `--num-workers` | int | `2` | 509 |
| `--num-train-steps` | int | `30000` | 511 |
| `--log-interval` | int | `100` | 514 |
| `--save-interval` | int | `1000` | 516 |
| `--keep-period` | int\|None | `5000` | 518 |
| `--overwrite` / `--no-overwrite` | bool | `False` | 521 |
| `--resume` / `--no-resume` | bool | `False` | 523 |
| `--wandb-enabled` / `--no-wandb-enabled` | bool | `True` | 526 |
| `--policy-metadata` | `{fixed}`（不可改） | 该 config 的 dict | 529 |
| `--fsdp-devices` | int | `1` | 535 |

**`--model.*`（`Pi0Config`，`src/openpi/models/pi0_config.py:18-48`）**

| flag | 取值 | pi05_libero 默认 | 行 |
|---|---|---|---|
| `--model.action-dim` | int | `32` | 25 |
| `--model.action-horizon` | int | `10` | 26 |
| `--model.max-token-len` | int | `200`（pi05）/`48`（pi0），见 `__post_init__` 37-39 | 27 |
| `--model.dtype` | str | `bfloat16` | 20 |
| `--model.paligemma-variant` | `dummy`/`gemma_300m`/`gemma_300m_lora`/`gemma_2b`/`gemma_2b_lora` | `gemma_2b` | 21 |
| `--model.action-expert-variant` | 同上 | `gemma_300m` | 22 |
| `--model.pi05` / `--model.no-pi05` | bool | `True` | 31 |
| `--model.discrete-state-input` / `--no-…` | bool | `False` | 33 |
| `--model.pytorch-compile-mode` | None\|str | `max-autotune` | 35 |

**`--weight-loader.*`（`src/openpi/training/weight_loaders.py:37-54`）**

| flag | 取值 | 默认 | 行 |
|---|---|---|---|
| `--weight-loader.params-path` | str | `gs://openpi-assets/checkpoints/pi05_base/params` | 48 |

**`--lr-schedule.*`（`CosineDecaySchedule`，`src/openpi/training/optimizer.py:15-31`）**

| flag | 默认（pi05_libero） | 全局默认（`CosineDecaySchedule()`） | 行 |
|---|---|---|---|
| `--lr-schedule.warmup-steps` | `10000` | `1000` | 19 |
| `--lr-schedule.peak-lr` | `5e-05` | `2.5e-5` | 20 |
| `--lr-schedule.decay-steps` | `1000000` | `30000` | 21 |
| `--lr-schedule.decay-lr` | `5e-05` | `2.5e-6` | 22 |

**`--optimizer.*`（`AdamW`，`optimizer.py:65-85`）**

| flag | 默认 | 行 |
|---|---|---|
| `--optimizer.b1` | `0.9` | 69 |
| `--optimizer.b2` | `0.95` | 70 |
| `--optimizer.eps` | `1e-8` | 71 |
| `--optimizer.weight-decay` | `1e-10`（注释：设 0 会 OOM，`optimizer.py:72`） | 73 |
| `--optimizer.clip-gradient-norm` | `1.0` | 74 |

**`--data.*` / `--data.assets.*`**

| flag | 取值 | pi05_libero 默认 | 源码 |
|---|---|---|---|
| `--data.repo-id` | str | `physical-intelligence/libero` | `config.py:168-169` |
| `--data.extra-delta-transform` | bool | `False`（pi0_libero 为 `True`） | `config.py:289` |
| `--data.assets.assets-dir` | str\|None | `None` | `config.py:57` |
| `--data.assets.asset-id` | str\|None | `None` | `config.py:61` |

### 1.5 哪些字段**没有** CLI（关键）

| 字段 | 原因 | 位置 |
|---|---|---|
| `name` | `tyro.conf.Suppress[str]` | `config.py:468` |
| `freeze_filter` | `tyro.conf.Suppress[Filter]` | `config.py:493` |
| `data_transforms` / `model_transforms`（SimpleDataConfig） | `tyro.conf.Suppress` | `config.py:215,217` |
| `base_config`（DataConfigFactory） | `tyro.conf.Suppress` | `config.py:173` |
| `repack_transforms`（Aloha） | `tyro.conf.Suppress` | `config.py:241` |

⇒ **「LoRA 微调」在纯 CLI 层面无法一步完成**：LoRA 需要 `freeze_filter=<对应 model config>.get_freeze_filter()`（见 `config.py:693-695` 与 `737-739`），而 `freeze_filter` 不在 CLI 上。表单若要做 LoRA，必须在训练镜像内注入自定义 config 模块（见 §5.2）。

### 1.6 不同 config 的 flag 集合**不同**（不是固定清单）

实测对比（`--help` 差异）：

| config | 独有的 data flag |
|---|---|
| `pi0_aloha` / `pi05_aloha` | `--data.use-delta-joint-actions`、`--data.default-prompt`、`--data.adapt-to-pi`、`--data.action-sequence-keys` |
| `pi05_libero` / `pi0_libero` | `--data.extra-delta-transform` |
| `pi0_fast_full_droid_finetune` / `pi05_full_droid_finetune` | `--data.rlds-data-dir`、`--data.action-space`、`--data.datasets.0.{name,version,weight,filter-dict-path}` |
| `pi0_fast_droid` / `pi0_fast_libero` | `--model.paligemma-variant` 选项只有 `{gemma_2b, gemma_2b_lora}`（**无 action-expert**）、`--model.fast-model-tokenizer`（fixed）、`--weight-loader.params-path` |

> 对表单实现的直接含义：**参数清单必须按所选 base config 动态生成**，不要硬编码一份"全量"清单，否则会出现 `--data.default-prompt` 在 `pi05_libero` 下报 unknown argument 的问题。

---

## 2. TrainConfig 全字段与模型差异

### 2.1 `TrainConfig` 26 字段（`config.py:465-557`）

`name`(468)、`project_name`(470)、`exp_name`(472)、`model`(477)、`weight_loader`(480)、`pytorch_weight_path`(483)、`pytorch_training_precision`(486)、`lr_schedule`(488)、`optimizer`(489)、`ema_decay`(490)、`freeze_filter`(493)、`data`(496)、`assets_base_dir`(499)、`checkpoint_base_dir`(501)、`seed`(504)、`batch_size`(506)、`num_workers`(509)、`num_train_steps`(511)、`log_interval`(514)、`save_interval`(516)、`keep_period`(518)、`overwrite`(521)、`resume`(523)、`wandb_enabled`(526)、`policy_metadata`(529)、`fsdp_devices`(535)。

约束（`__post_init__`，`config.py:554-556`）：`resume` 与 `overwrite` **不能同时为 True**，否则 `ValueError("Cannot resume and overwrite at the same time.")`。

### 2.2 三种模型的关键超参差异

| | **π0** (`Pi0Config()`) | **π0.5** (`Pi0Config(pi05=True)`) | **π0-FAST** (`Pi0FASTConfig`) |
|---|---|---|---|
| model_type | `PI0` | `PI05` | `PI0_FAST` |
| 默认 `action_dim` | 32 | 32 | 32 |
| 默认 `action_horizon` | **50** | **50** | **32** |
| 默认 `max_token_len` | **48** | **200** | **250** |
| `discrete_state_input` | `False` | `True`（`__post_init__` 跟随 pi05） | n/a |
| 主干/专家 | `gemma_2b` + `gemma_300m` | 同左（专家用 adaRMSNorm 注入 timestep） | 仅 `gemma_2b`（无 action expert） |
| 归一化方式 | **z-score**（`use_quantile_norm=False`） | **quantile**（1%/99%） | **quantile** |
| 损失 | flow matching | flow matching | **自回归 CE**（`pi0_fast.py:229-233`） |
| 图像 key | `base_0_rgb`/`left_wrist_0_rgb`/`right_wrist_0_rgb` | 同 π0 | `base_0_rgb`/`base_1_rgb`/`wrist_0_rgb` |
| LoRA freeze filter | 支持双 lora（`pi0_config.py:88-117`） | 同 π0 | 仅 paligemma lora（`pi0_fast.py:127-131`） |

来源：`pi0_config.py:20-48`、`pi0_fast.py:77-89`、`config.py:187`（`use_quantile_norm=model_config.model_type != ModelType.PI0`）。

> **注意 `max_token_len`**：π0-FAST 的注释（`config.py:704-711`）给出经验值——单臂约 180、双臂约 250；太小会截断并告警，太大会因 padding 浪费显存。

### 2.3 官方 config 的"推荐超参"（可直接抄）

| config | 模型 | batch_size | peak_lr / warmup / decay_steps | ema_decay | steps | 源码行 |
|---|---|---|---|---|---|---|
| `pi0_libero` | π0 全量 | **32**（`TrainConfig` 默认） | 2.5e-5 / 1000 / 30000 | 0.99 | 30000 | 651-677 |
| `pi0_libero_low_mem_finetune` | π0 LoRA | 32 | 同上 | **None** | 30000 | 678-698 |
| `pi0_fast_libero` | π0-FAST 全量 | 32 | 同上 | 0.99 | 30000 | 699-720（注意此处 **`action_dim=7`**、`action_horizon=10`、`max_token_len=180`，非类默认值） |
| `pi0_fast_libero_low_mem_finetune` | π0-FAST LoRA | 32 | 同上 | **None** | 30000 | 721-742 |
| **`pi05_libero`** | **π0.5 全量** | **256** | **5e-5 / 10000 / 1e6** | **0.999** | 30000 | 743-763 |
| `pi05_aloha_pen_uncap` | π0.5 | **64** | 默认 | 0.99 | 20000 | 798-827 |
| `pi0_aloha_pen_uncap` | π0 | 32 | 默认 | 0.99 | 20000 | 769-797 |
| `pi0_fast_full_droid_finetune` | π0-FAST RLDS | 256 | 5e-5 / 1000 / 1e6 | 0.99 | 100000（8×H100 约 2 天） | 831-860 |
| `pi05_full_droid_finetune` | π0.5 RLDS | 256 | 5e-5 / 1000 / 1e6 | 0.99 | 100000 | 861-894 |
| `pi05_droid_finetune` | π0.5 LeRobot | **32** | 默认 | 0.99 | 20000 | 895-918 |

两个必须知道的细节：

1. **π0.5 的 cosine schedule 实际上是"常数 LR"**：`peak_lr=5e-5` 且 `decay_lr=5e-5`、`decay_steps=1e6` ≫ `num_train_steps=30000`，因此 30k 步内 LR 基本恒为 5e-5。抄配置时若改小 `decay_steps` 会无意中引入衰减。
2. **LoRA 一律 `ema_decay=None`**（`config.py:696-697`、`740-741` 原文注释 "Turn off EMA for LoRA finetuning."）。

### 2.4 π0 / π0.5 论文口径 —— **部分未核实（重要）**

| 项 | 论文/官方出处 | 状态 |
|---|---|---|
| π0 预训练步数 700k（对比实验用 160k 的 "compute parity" 版本） | [π0 论文 arXiv:2410.24164v4](https://arxiv.org/abs/2410.24164) 第 VI-A 节 | ✅ 已核实（HTML 正文） |
| π0 动作块 H=50；推理 10 步 Euler；总量 ~3.3B（PaliGemma 3B + action expert 300M） | 同上 第 IV 节 | ✅ 已核实 |
| π0 数据配比：按任务-机器人组合权重 `n^0.43` 降采样；9.1% 为开源数据 | 同上 第 V-A 节 | ✅ 已核实 |
| **π0 预训练的 batch size、学习率、EMA decay** | 论文附录 A-B "Model Architecture Details" | ❌ **未核实**：arXiv HTML 正文在附录前被截断，且本会话 web_search 不可用（无 API key），无法取得附录原文。**不要编造具体数值**。 |
| **π0.5 论文的训练 batch / LR / 步数** | [π0.5 论文 arXiv:2504.16054](https://arxiv.org/abs/2504.16054) | ❌ **未核实**：仅取到摘要页。 |
| π0.5 微调 batch 256 / lr 5e-5 / ema 0.999 / 30k 步 | openpi `pi05_libero` config（`config.py:743-763`） | ✅ 已核实 —— **这是官方开源实现里最权威的"推荐微调配置"** |
| LIBERO 上 π0.5 @30k 微调：平均 96.85% | `examples/libero/README.md` Results 表 | ✅ 已核实 |

> 建议对外表述：**"π0 预训练用 700k 步（论文）"** ✅；**"π0 预训练 batch=?, lr=?"** 一律标"论文附录未公开于可访问正文 / 未核实"。

---

## 3. norm stats 与数据归一化

### 3.1 机制

- `DataConfig.norm_stats`（`config.py:70-71`）：为 None 时**不做归一化**。
- 加载路径：`DataConfigFactory._load_norm_stats`（`config.py:190-200`）从 `assets_dir / asset_id / norm_stats.json` 读；文件缺失 → **仅打 log "skipping"**，不报错。
- 真正报错在数据加载器：`data_loader.py:175-181`（及 203-209）
  ```python
  if data_config.repo_id != "fake" and not skip_norm_stats:
      if data_config.norm_stats is None:
          raise ValueError("Normalization stats not found. "
                           "Make sure to run `scripts/compute_norm_stats.py --config-name=<your-config>`.")
  ```
- 归一化算子插在 `repack → data_transforms` **之后**、`model_transforms` 之前（`data_loader.py:183-191`）。
- 统计量结构 `NormStats(mean, std, q01, q99)`（`src/openpi/shared/normalize.py:9-14`）；z-score vs quantile 由 `use_quantile_norm` 切换，而它由**模型类型硬决定**（π0 → z-score，π0.5/FAST → quantile，`config.py:187`）。

### 3.2 `scripts/compute_norm_stats.py`

- CLI：`tyro.cli(main)`，参数为 **`--config-name`（必填）** 与 **`--max-frames`（可选，int|None）**（`compute_norm_stats.py:89, 116-117`）。
- 只统计两个 key：`["state", "actions"]`（第 102 行）——**不含图像**。
- 复用训练 config 的 `batch_size` 与 `num_workers`（第 95、99 行）→ 若训练 config 的 `batch_size` 大于数据集样本数，会直接抛错（`data_loader.py:415-416`：`Local batch size (...) is larger than the dataset size`）。
- 输出路径（第 111-113 行）：
  ```python
  output_path = config.assets_dirs / data_config.repo_id
  ```
  即 `./assets/<config.name>/<repo_id>/norm_stats.json`。

> ⚠️ **不对称坑**：写入用 `repo_id`，读取用 `asset_id`（`config.py:181,186,194`）。二者只在 `asset_id` 未显式指定时才相等（`asset_id = self.assets.asset_id or repo_id`）。若设置了 `--data.assets.asset-id foo`，必须把 `norm_stats.json` 放到 `./assets/<config>/foo/`，或干脆改用"复用预训练统计量"路线。

### 3.3 是否必须前置执行？能不能在训练容器内自动做？

- **必须**（LeRobot 数据集）。否则训练在 `iter(data_loader)` 取第一个 batch 时就抛错（`train.py:220-226`）。
- **不能"自动"由 `train.py` 完成**：`train.py` 任何路径都不会调用统计计算。
- **但可以在训练容器内自动化的**（推荐做法，无需改源码）：在容器 entrypoint 里，先判存在性再跑脚本——
  ```bash
  test -f "./assets/${CFG}/${REPO_ID}/norm_stats.json" || \
    uv run scripts/compute_norm_stats.py --config-name="${CFG}" --max-frames=200000
  ```
  这与训练共享同一 `.venv`、同一 LeRobot pin、同一 `./assets` 目录，不会引入版本漂移。（`--max-frames` 用于大数据集时抽样加速，`compute_norm_stats.py:44-49` 会因此改为 shuffle 采样。）
- **完全跳过的唯一路径**：`create_data_loader(..., skip_norm_stats=True)`（`data_loader.py:229`）——但 `train.py` 调用时**没有**传该参数（`train.py:220-224`），所以从 CLI 无法跳过。 ✅ 只能改代码。
- **复用预训练统计量**（`docs/norm_stats.md:9-24`）：当你的机器人属于下表之一时，可设 `--data.assets.assets-dir gs://openpi-assets/checkpoints/pi05_base/assets --data.assets.asset-id <id>`，**从而完全跳过 compute_norm_stats**。可用 asset_id：`trossen`、`trossen_mobile`、`droid`、`franka`、`ur5e`、`ur5e_dual`、`arx`、`arx_mobile`、`fibocom_mobile`（`docs/norm_stats.md:34-44`）。
  > 官方明确建议两种方案都试（`docs/norm_stats.md:28`）。
  > ⚠️ 复用前提：动作空间定义必须与预训练一致（`docs/norm_stats.md:47-69`：关节角弧度、夹爪 [0,1]、UR5e/Franka 20Hz、ARX/Trossen 50Hz、DROID 15Hz 速度动作）。

### 3.4 自定义数据集流程（README「Fine-Tuning Base Models on Your Own Data」）

1. **转成 LeRobot 数据集**——参考 `examples/libero/convert_libero_data_to_lerobot.py --data_dir ...`、`examples/aloha_real/convert_aloha_data_to_lerobot.py`、`examples/droid/convert_droid_data_to_lerobot.py`（`README.md:126-138`）。
2. **写 3 个东西**（`README.md:144-148`）：
   - `XxxInputs` / `XxxOutputs`（仿 `src/openpi/policies/libero_policy.py`）——训练与推理**共用**；
   - `LeRobotXxxDataConfig`（仿 `config.py:281-355`，核心是 `RepackTransform` 的 key 映射）；
   - `TrainConfig` 实例加入 `_CONFIGS`（`config.py:560-971`）。
3. **`compute_norm_stats.py --config-name <new>`**
4. **`train.py <new> --exp-name ... --overwrite`**

> ⚠️ **注意**：仓库里**没有 `docs/custom_data.md`**（`docs/` 实际只有 `docker.md`、`norm_stats.md`、`remote_inference.md`，已 `ls` 核实）。任务描述中的该文件名**不存在**；等价内容分散在 `README.md:124-165` 与 `config.py` 的 `LeRobotLiberoDataConfig` docstring 注释里。

---

## 4. 数据集格式：LeRobot v2.0 / v2.1 / v3.0 与 openpi 支持面

### 4.1 openpi 实际钉住的版本

- `pyproject.toml`（`[tool.uv.sources]`）：`lerobot = { git = "https://github.com/huggingface/lerobot", rev = "0cf864870cf29f4738d3ade893e6fd13fbd7cdb5" }`。经 GitHub API 核实该 commit 为 **2025-05-28**，标题 *"[Fix] Unpin torch beyond 2.6.0 & torchcodec beyond 0.2.1 (#1127)"*。
- 远端 venv 内 `lerobot/__version__ = 0.1.0`，`lerobot/common/datasets/lerobot_dataset.py:77` → **`CODEBASE_VERSION = "v2.1"`**。
- 数据集实现目录仅有 `common/datasets/v2` 与 `common/datasets/v21`（**无 `v30`**）→ 结论：**不支持 v3.0**。

### 4.2 三个版本的差异（据该 pin 的 `backward_compatibility.py`）

| 版本 | 状态 | 差异 |
|---|---|---|
| **v1.x** | ❌ 不支持 | 抛 `BackwardCompatibilityError`，要求用 `lerobot/common/datasets/v2/convert_dataset_v1_to_v2.py` 转换 |
| **v2.0** | ✅ 支持（有告警） | 兼容，但 stats 为**全局**而非**按 episode**；提示用 `v21/convert_dataset_v20_to_v21.py` 升级。差异点是 episode 统计量的组织方式（per-episode stats），**不影响 openpi 用法**（openpi 自己算 norm stats，不读 lerobot 的 stats） |
| **v2.1** | ✅ 原生支持（`CODEBASE_VERSION`） | 当前默认 |
| **v3.0** | ❌ **会出现 `FUTURE_MESSAGE` 报错**："The dataset you requested is only available in {version} format... please update your current version of lerobot." | v3 改为**每个 episode 一个 parquet + 不同的 meta 结构**；需升 lerobot，而升 lerobot 会与 openpi 的 `LeRobotDatasetMetadata` / `LeRobotDataset(repo_id, delta_timestamps=...)` 调用签名（`data_loader.py:140-146`）不兼容 |

> ⚠️ **未核实**：v3.0 的细节（parquet 布局、meta 字段）来自 HuggingFace lerobot 上游文档/发布说明，本会话未逐条抓取上游 v3 文档；上表 v3 行为中"必然报 FUTURE_MESSAGE"是基于 pin 里 `backward_compatibility.py` 的逻辑推出的（`dataset version > CODEBASE_VERSION` → FUTURE_MESSAGE）。若你的数据是 v3，**建议先 `git log` 确认你实际使用的 openpi 分支是否已升级 lerobot pin**。

### 4.3 对实现表单的直接影响

- 表单应在数据集选择时做**前置校验**：读 `meta/info.json` 的 `codebase_version`，落库为 `v2.0|v2.1`，`v3.0` 直接拦截并给出"需转换/需升级 lerobot"提示。
- openpi 用到的 LeRobot 字段：`data_config.repo_id`（HF repo id 或本地路径）、`action_sequence_keys`（默认 `("actions",)`，Aloha 为 `("action",)`，`config.py:88/255`）、`prompt_from_task`（从数据集的 `task` 字段取指令，`data_loader.py:148-149`）。

---

## 5. 单卡 4090 48G 跑 pi05 微调的现实参数

### 5.1 官方显存口径（`README.md:24-30`）

| 模式 | 显存需求 | 示例 GPU |
|---|---|---|
| 推理 | > 8 GB | RTX 4090 |
| **微调（LoRA）** | **> 22.5 GB** | RTX 4090 |
| 微调（全量） | > 70 GB | A100 80GB / H100 |

⇒ **48G 的 4090 变体上，pi05 只能跑 LoRA**；全量微调 4090 装不下。
⇒ 另注：`README.md:24` 明说"current training script does not yet support multi-node training"，`data_loader.py:412-413` 也禁止多进程数据加载。

### 5.2 现实配置建议（基于源码可验证的机制）

**（A）LoRA —— 4090 48G 唯一可行路线**

由于 `--freeze-filter` 不在 CLI 上（§1.5），必须在训练镜像里注册一个自定义 config。最小改法（照抄 `config.py:678-698` 的 `pi0_libero_low_mem_finetune` 模式，改成 π0.5）：

```python
# 放到容器内 openpi 仓库的 src/openpi/training/config.py 的 _CONFIGS 里，或 -m 注入模块
TrainConfig(
    name="pi05_lora_custom",
    model=pi0_config.Pi0Config(
        pi05=True, action_horizon=10, discrete_state_input=False,
        paligemma_variant="gemma_2b_lora", action_expert_variant="gemma_300m_lora",
    ),
    data=LeRobotXxxDataConfig(repo_id="<你的数据集>", base_config=DataConfig(prompt_from_task=True)),
    weight_loader=weight_loaders.CheckpointWeightLoader("gs://openpi-assets/checkpoints/pi05_base/params"),
    batch_size=4,                 # 见下方显存预估
    num_train_steps=30_000,
    lr_schedule=_optimizer.CosineDecaySchedule(warmup_steps=1_000, peak_lr=5e-5,
                                               decay_steps=30_000, decay_lr=5e-5),
    optimizer=_optimizer.AdamW(clip_gradient_norm=1.0),
    ema_decay=None,               # 官方 LoRA 约定：关 EMA
    freeze_filter=pi0_config.Pi0Config(
        pi05=True, paligemma_variant="gemma_2b_lora", action_expert_variant="gemma_300m_lora"
    ).get_freeze_filter(),
    fsdp_devices=1,               # 单卡 = 1
    num_workers=4,
    wandb_enabled=False,
)
```

配套环境变量（来自源码与 README）：
- `XLA_PYTHON_CLIENT_MEM_FRACTION=0.9`（README `README.md:163` 明确说明；JAX 默认只用 75%）
- `--model.dtype bfloat16`（默认即是；4090 支持 bf16）
- `--num-workers 4`（默认 2；多 worker 会通过 `_worker_init_fn` 关闭子进程的 GPU 预分配，`data_loader.py:478-483`）

**（B）batch size 选择约束（硬约束，来自源码）**

- `train.py:198-201`：`batch_size % jax.device_count() != 0` → **报错**。单卡 device_count=1，任何 batch 都通过；但仍是表单必须校验的规则（多卡时）。
- `data_loader.py:415-416`：`local_batch_size > len(dataset)` → 报错。**小数据集（<100 帧）必须先降 batch**。
- `sharding.py:18-21`：`device_count % fsdp_devices != 0` → 报错。单卡时 `fsdp_devices` 只能是 1。

**（C）没有梯度累积**

`train.py` 全文无 `grad_accum` / `accumulate` 相关字段；`train_step`（`train.py:136-191`）每步做一次 `nnx.value_and_grad` + `tx.update`。要做"等效大 batch"，只能：
- 接受小 batch（LoRA 下常见 4~8）；
- 或改 `train.py` 自己加累积（需改源码，非表单项）。
> 建议表单里 **不要** 放 grad accum 字段——它不存在，放进去就是编造。

**（D）显存预估**

| 项 | 数值 | 依据 |
|---|---|---|
| LoRA 微调最低 | **>22.5 GB** | `README.md:29` |
| 全量微调最低 | **>70 GB** | `README.md:30` |
| 单步显存随 batch 线性增长 | 粗略 `Mem ≈ 22.5GB + k·batch` | **估算，未实测**；4090 48G 下 LoRA + batch 8 大概率可行，batch 16 需实测 |
| 显存膨胀风险点 | `--optimizer.weight-decay 0` 会导致 OOM（官方注释 `optimizer.py:72-73`）；`max_token_len` 过大浪费显存（`config.py:706-710`） | 已核实注释 |

> **明确标注**：上表"k"与"batch 8/16 可行"属于**未实测推断**。本会话无法在目标机器上跑 benchmark（沙箱只读工作区、无 GPU）。给出可执行的实测方案见 §7.1。

### 5.3 单卡下应固定/隐藏的参数

- `--fsdp-devices`：单卡强制 `1`（否则 `sharding.py:19` 报错）。
- `--policy-metadata`：`{fixed}` 不可改，**不要渲染**。
- `--pytorch-weight-path` / `--pytorch-training-precision` / `--model.pytorch-compile-mode`：只服务于 `scripts/train_pytorch.py`（PyTorch 后端）。且注意 `pi05_libero` 里 `pytorch_weight_path="/path/to/your/pytorch_weight_path"` 是个**占位路径**（`config.py:761`）——若 UI 照抄会引导用户填错。建议在 JAX 后端表单里隐藏这 3 项。

---

## 6. 常见失败模式与处理

| # | 现象 / 报错 | 根因（源码证据） | 处理 |
|---|---|---|---|
| 1 | `ValueError: Normalization stats not found. Make sure to run scripts/compute_norm_stats.py ...` | `data_loader.py:176-180` | 先跑 compute_norm_stats；或设 `--data.assets.*` 复用预训练统计量；注意 §3.2 的 `repo_id` vs `asset_id` 目录不对称 |
| 2 | 训练能启动但 loss/动作全乱，不收敛 | `_load_norm_stats` 仅打 log "Norm stats not found ... skipping"（`config.py:198-199`），**不中断**；但随后由 #1 触发 ValueError。若 `repo_id == "fake"` 则静默跳过归一化 | 检查 `info.json` 的 codebase_version、`action_sequence_keys` 是否与数据列名一致（默认 `actions`，Aloha 用 `action`） |
| 3 | `FileExistsError: Checkpoint directory ... already exists. Use --overwrite or --resume` | `checkpoints.py:33-36` | 三选一：`--overwrite`（**会 rmtree 删目录**，`checkpoints.py:27`）／`--resume`／改 `--exp-name`。注意 `__post_init__` 禁止 overwrite+resume 同开（`config.py:555-556`） |
| 4 | resume 后从 step 0 重新开始 | `checkpoints.py:58-60`：目录存在但**无 checkpoint 或只有 step 0** 时放弃 resume | 说明上次还没存过 ckpt；正常现象 |
| 5 | `Batch size N must be divisible by the number of devices M` | `train.py:198-201` | 调整 `--batch-size` 为设备数整数倍 |
| 6 | `Local batch size (...) is larger than the dataset size (...)` | `data_loader.py:415-416` | 降 `--batch-size` / 增加数据（compute_norm_stats 也受此影响） |
| 7 | `Number of devices M must be divisible by the number of FSDP devices N` | `sharding.py:18-21` | 单卡 `--fsdp-devices 1`；多卡取约数 |
| 8 | `ValueError: The length of new vectors does not match the initialized vector length.` | `normalize.py:50-51`：RunningStats 各 batch 的 state/action 维度不一致 | 数据集内 state/action 维度不一（脏帧）；修数据或统一 `action_dim` |
| 9 | 维度不匹配（action_dim / 图像通道 / 相机数量） | `PadStatesAndActions(model_config.action_dim)` 在 `ModelTransformFactory` 中（`config.py:123,136`）；π0/π0.5 需 3 个图像槽 `base_0_rgb`/`left_wrist_0_rgb`/`right_wrist_0_rgb`，FAST 为 `base_0_rgb`/`base_1_rgb`/`wrist_0_rgb`（`pi0_config.py:70-74`、`pi0_fast.py:107-111`） | 少于 3 路相机时靠 `mask_ar`/image_mask 置零补齐（官方支持 2 路）；超出会导致 Pad 张量维度错误 → 检查 `RepackTransform` 映射 |
| 10 | 权重加载 `check_pytree_equality` 失败 / 形状 dtype 不符 | `train.py:73-81`：加载后 `at.check_pytree_equality(..., check_shapes=True, check_dtypes=True)`；`_merge_params` 只合并 `.*lora.*` 缺失键（`weight_loaders.py:54,76-103`） | ① base checkpoint 必须与 model 家族一致（pi05_* 配 `pi05_base`，pi0_fast 配 `pi0_fast_base`，见 `config.py:718` 注释）；② 改 `action_dim` 不改变 PaliGemma 主体但会改 action expert，混用会维度冲突；③ LoRA 必须同时设对 `freeze_filter`，否则 LoRA 参数会被当作"缺失"从 ref 初始化（随机） |
| 11 | `gs://` 下载失败 / `maybe_download` 报错 | `download.maybe_download`（`weight_loaders.py:52`、`config.py:195`）依赖 `openpi.shared.download` 与 `gcsfs` | 内网环境先 `gsutil -m cp -r gs://openpi-assets/checkpoints/<model>/params ./local_params`，再 `--weight-loader.params-path ./local_params`；国内建议预下载 + 本地路径 |
| 12 | OOM（显存） | 见 §5 | 降 batch；开 LoRA；`--model.dtype bfloat16`；**不要**把 `--optimizer.weight-decay` 设为 0（`optimizer.py:72-73` 明示会 OOM）；`XLA_PYTHON_CLIENT_MEM_FRACTION=0.9` 抽屉式调高占用，OOM 时反而要调低（0.7~0.8）留出碎片空间 |
| 13 | OOM（**主机内存**，非 GPU） | 官方 PR #616：`_merge_params` 峰值 RSS 曾达 ~26 GB，优化后 ~22 GB（GitHub commit `0fffc729`，见 `weight_loaders.py:90-96` 的现行实现） | 训练容器内存至少 32 GB；大模型加载阶段监控 RSS |
| 14 | 数据加载慢 / GPU 空闲 | 默认 `num_workers=2`（`config.py:509`）；每步同步 `next(data_iter)`（`train.py:270`）；视频解码是瓶颈 | 提 `--num-workers`（经验 8~16，受 CPU 核数限制）；数据集尽量用 image 而非 video 编码；多 worker 会自动禁用子进程 GPU 预分配（`data_loader.py:478-483`） |
| 15 | RLDS/DROID 路径下报 `NotImplementedError: Data loading with multiple processes is not supported` | `data_loader.py:412-413` | RLDS 必须 `--num-workers 0`（官方 config 注释 `config.py:859`） |
| 16 | `--data.default-prompt` 在 libero/droid config 下 unknown argument | 该字段只存在于 `LeRobotAlohaDataConfig`（`config.py:234`） | 表单按 config 类型动态出参数（§1.6） |
| 17 | `ValueError: --exp_name must be set` | `config.py:545-546`（`checkpoint_dir` property） | 必填校验 |
| 18 | wandb 登录阻塞/失败 | `init_wandb`（`train.py:50-71`）；`wandb_enabled=False` 时走 `wandb.init(mode="disabled")` | 离线环境置 `--no-wandb-enabled`（或 `WANDB_MODE=offline`） |

---

## 7. 版本差异说明（与早期版本对比）

远端镜像 `git log` 只有 1 个 squashed commit，无法本地 diff；以下通过 GitHub API + `raw.githubusercontent.com` 拉取历史文件比对得出：

| 变化 | 旧 → 新 | 影响 |
|---|---|---|
| `TrainConfig` 新增字段 | 2025-08-30（`5a93876`）有 24 字段；HEAD 有 26 字段 | 新增 **`pytorch_weight_path`**、**`pytorch_training_precision`**（`config.py:483,486`）。**旧版没有这两个 flag**，表单若标注"兼容旧版"需可选 |
| EMA 曾被删又回归 | commit `aa7853c`（2025-09-05/08）*"remove ema, ... set float32 by default for finetuning"*；当前 HEAD **EMA 已回归**（`config.py:490`、`train.py:169-175`、`checkpoints` 中 `ema_params`） | 若基于 2025-09 的快照实现，`--ema-decay` 可能不存在。**以 HEAD 为准** |
| `pi05_*` config 出现时间 | 2025-09-07（`ab2855a`）时 `pi05_libero` **已存在**；2025-08-30（`5a93876`）**尚无** | 2025-08 及更早的文档/教程里没有 π0.5 微调示例 |
| `pi05_libero` 超参稳定性 | `batch_size=256`、`peak_lr=5e-5`、`warmup=10000`、`decay_steps=1e6`、`ema_decay=0.999` 在 2025-09-07 与 2025-12-06 两个快照中**完全一致** | 可放心引用 |
| `weight_decay` 默认 | commit `1d29406` *"Default weight decay to 0"*；HEAD 为 `1e-10`（`optimizer.py:73`，注释解释避免 OOM） | 旧版可能为 0 → 与官方注释冲突的 OOM 来源 |

---

## 8. JSON 参数模板

> 说明：
> - 所有 `flag` 均来自实测 `--help` 输出（§1.4），**无编造**。
> - `default` 取 **`pi05_libero`**（π0.5 微调最常用的参考 config）的实际默认值；`help` 全部 ≤60 字。
> - 该模板覆盖 24 项，远超 12 项下限，且覆盖要求的 8 个维度：训练步数、batch size、学习率、保存间隔、日志间隔、worker 数、权重加载、数据集配置、设备/精度。
> - 需与 `extra_args.enabled=true` 搭配：tyro 会拒绝未知 flag，所以"高级节能项"必须由后端白名单校验后再拼进命令行。

```json
{
  "schema_version": 1,
  "entrypoint": "XLA_PYTHON_CLIENT_MEM_FRACTION=0.9 uv run scripts/train.py {{config_name}} --exp-name={{exp_name}} --checkpoint-base-dir={{checkpoint_base_dir}} --batch-size={{batch_size}} --num-train-steps={{num_train_steps}} --lr-schedule.peak-lr={{peak_lr}} --lr-schedule.warmup-steps={{warmup_steps}} --save-interval={{save_interval}} --log-interval={{log_interval}} --num-workers={{num_workers}} --weight-loader.params-path={{weight_loader_params_path}} --data.repo-id={{repo_id}} --model.action-horizon={{action_horizon}} --fsdp-devices={{fsdp_devices}} --model.dtype={{model_dtype}} --wandb-enabled --overwrite",
  "params": [
    {
      "key": "config_name", "flag": "<positional>", "label": "训练配置名（位置参数）",
      "type": "enum", "default": "pi05_libero",
      "options": ["pi05_libero", "pi0_libero", "pi0_libero_low_mem_finetune", "pi0_fast_libero", "pi0_fast_libero_low_mem_finetune", "pi05_aloha_pen_uncap", "pi0_aloha_pen_uncap", "pi05_droid_finetune", "pi05_full_droid_finetune", "pi0_fast_full_droid_finetune", "pi0_aloha", "pi05_aloha", "pi0_aloha_sim", "debug"],
      "required": true,
      "help": "train.py 的位置参数，决定模型家族与全部默认超参",
      "source": "scripts/train.py:279-280 → src/openpi/training/config.py:560-971 (_CONFIGS) 与 978-979 (cli)"
    },
    {
      "key": "exp_name", "flag": "--exp-name", "label": "实验名",
      "type": "str", "default": null, "required": true,
      "help": "决定 checkpoint 子目录；未填会报 --exp_name must be set",
      "source": "src/openpi/training/config.py:472; checkpoint_dir property 545-547"
    },
    {
      "key": "num_train_steps", "flag": "--num-train-steps", "label": "训练总步数",
      "type": "int", "default": 30000, "min": 1, "max": 1000000, "required": false,
      "help": "优化器更新步数（非 epoch）；π0/π0.5 微调官方用 30000",
      "source": "src/openpi/training/config.py:511; 用法 scripts/train.py:251-272"
    },
    {
      "key": "batch_size", "flag": "--batch-size", "label": "全局批大小",
      "type": "int", "default": 256, "min": 1, "max": 4096, "required": false,
      "help": "必须能被设备数整除，且需小于数据集长度；单卡4090建议4-8",
      "source": "src/openpi/training/config.py:506; 校验 scripts/train.py:198-201; data_loader.py:415-416"
    },
    {
      "key": "peak_lr", "flag": "--lr-schedule.peak-lr", "label": "峰值学习率",
      "type": "float", "default": 5e-5, "min": 1e-7, "max": 1e-2, "required": false,
      "help": "warmup 后的学习率；π0.5 建议 5e-5，π0 建议 2.5e-5",
      "source": "src/openpi/training/optimizer.py:20 (CosineDecaySchedule.peak_lr); 被 25 行传入 warmup_cosine_decay_schedule"
    },
    {
      "key": "warmup_steps", "flag": "--lr-schedule.warmup-steps", "label": "学习率预热步数",
      "type": "int", "default": 10000, "min": 0, "max": 100000, "required": false,
      "help": "线性 warmup 步数；初始 lr = peak_lr/(warmup+1)",
      "source": "src/openpi/training/optimizer.py:19,26"
    },
    {
      "key": "decay_steps", "flag": "--lr-schedule.decay-steps", "label": "余弦衰减总步数",
      "type": "int", "default": 1000000, "min": 1, "max": 5000000, "required": false,
      "help": "衰减周期；远大于训练步数时等效恒定学习率",
      "source": "src/openpi/training/optimizer.py:21,29"
    },
    {
      "key": "decay_lr", "flag": "--lr-schedule.decay-lr", "label": "末端学习率",
      "type": "float", "default": 5e-5, "min": 0.0, "max": 1e-2, "required": false,
      "help": "衰减结束时的 lr；与 peak_lr 相等即为常数学习率",
      "source": "src/openpi/training/optimizer.py:22,30"
    },
    {
      "key": "save_interval", "flag": "--save-interval", "label": "Checkpoint 保存间隔",
      "type": "int", "default": 1000, "min": 1, "max": 100000, "required": false,
      "help": "每隔多少步存一次；末步必定保存",
      "source": "src/openpi/training/config.py:516; 触发逻辑 scripts/train.py:272-273"
    },
    {
      "key": "keep_period", "flag": "--keep-period", "label": "长期保留周期",
      "type": "int", "default": 5000, "min": 1, "max": 1000000, "required": false,
      "help": "step 能被该值整除的 checkpoint 不删；其余只留最近1个",
      "source": "src/openpi/training/config.py:518; checkpoints.py:47-52 (max_to_keep=1)"
    },
    {
      "key": "log_interval", "flag": "--log-interval", "label": "日志间隔",
      "type": "int", "default": 100, "min": 1, "max": 10000, "required": false,
      "help": "每隔多少步聚合打印 loss/grad_norm 并写 wandb",
      "source": "src/openpi/training/config.py:514; scripts/train.py:263-269"
    },
    {
      "key": "num_workers", "flag": "--num-workers", "label": "DataLoader 进程数",
      "type": "int", "default": 2, "min": 0, "max": 64, "required": false,
      "help": "CPU 进程数；0=主进程加载，RLDS 必须为0",
      "source": "src/openpi/training/config.py:509; data_loader.py:264,429-446; 478-483"
    },
    {
      "key": "weight_loader_params_path", "flag": "--weight-loader.params-path", "label": "预训练权重路径",
      "type": "path", "default": "gs://openpi-assets/checkpoints/pi05_base/params", "required": false,
      "help": "base checkpoint 目录；须与模型家族匹配，支持gs://或本地路径",
      "source": "src/openpi/training/weight_loaders.py:48-54 (CheckpointWeightLoader); 加载校验 train.py:73-81"
    },
    {
      "key": "repo_id", "flag": "--data.repo-id", "label": "数据集 Repo ID",
      "type": "str", "default": "physical-intelligence/libero", "required": false,
      "help": "LeRobot 数据集标识或本地路径；同时决定 norm_stats 目录名",
      "source": "src/openpi/training/config.py:168-169, 180-182; data_loader.py:134-146"
    },
    {
      "key": "asset_id", "flag": "--data.assets.asset-id", "label": "归一化统计量 ID",
      "type": "str", "default": null, "required": false,
      "help": "可复用预训练统计量（trossen/droid/franka 等）；留空用 repo_id",
      "source": "src/openpi/training/config.py:61, 181; docs/norm_stats.md:9-44"
    },
    {
      "key": "assets_dir", "flag": "--data.assets.assets-dir", "label": "资产目录",
      "type": "path", "default": null, "required": false,
      "help": "norm stats 的源目录；指向预训练 checkpoint 的 assets",
      "source": "src/openpi/training/config.py:57, 186"
    },
    {
      "key": "assets_base_dir", "flag": "--assets-base-dir", "label": "本地资产根目录",
      "type": "path", "default": "./assets", "required": false,
      "help": "compute_norm_stats 输出根目录，实际路径为 <该目录>/<config>/<id>",
      "source": "src/openpi/training/config.py:499, 538-540; compute_norm_stats.py:111-113"
    },
    {
      "key": "checkpoint_base_dir", "flag": "--checkpoint-base-dir", "label": "Checkpoint 根目录",
      "type": "path", "default": "./checkpoints", "required": false,
      "help": "实际保存路径为 <该目录>/<config>/<exp_name>",
      "source": "src/openpi/training/config.py:501, 543-547"
    },
    {
      "key": "action_horizon", "flag": "--model.action-horizon", "label": "动作块长度",
      "type": "int", "default": 10, "min": 1, "max": 100, "required": false,
      "help": "每次推理输出的动作数；须与数据增强时序窗口一致",
      "source": "src/openpi/models/pi0_config.py:26; 用于 data_loader.py:143-145 的 delta_timestamps"
    },
    {
      "key": "action_dim", "flag": "--model.action-dim", "label": "动作维度",
      "type": "int", "default": 32, "min": 1, "max": 64, "required": false,
      "help": "动作/状态向量维度，不足补零；π0.5 训练用32维动作空间",
      "source": "src/openpi/models/pi0_config.py:25; PadStatesAndActions config.py:123,136; docs/norm_stats.md:47-69"
    },
    {
      "key": "model_dtype", "flag": "--model.dtype", "label": "模型计算精度",
      "type": "enum", "default": "bfloat16", "options": ["bfloat16", "float32"], "required": false,
      "help": "bfloat16 省显存且4090原生支持；float32 仅排障用",
      "source": "src/openpi/models/pi0_config.py:20; pi0_fast.py:78; 冻结参数转 bf16 train.py:102-104"
    },
    {
      "key": "ema_decay", "flag": "--ema-decay", "label": "EMA 衰减系数",
      "type": "float", "default": 0.999, "min": 0.0, "max": 0.9999, "required": false,
      "help": "权重指数滑动平均；官方LoRA配置设为None(关闭)",
      "source": "src/openpi/training/config.py:490; EMA 更新 train.py:169-175; LoRA 关 EMA config.py:696-697"
    },
    {
      "key": "clip_gradient_norm", "flag": "--optimizer.clip-gradient-norm", "label": "梯度裁剪范数",
      "type": "float", "default": 1.0, "min": 0.0, "max": 100.0, "required": false,
      "help": "全局梯度范数裁剪阈值；0 表示不裁剪",
      "source": "src/openpi/training/optimizer.py:74, 85 (optax.clip_by_global_norm)"
    },
    {
      "key": "fsdp_devices", "flag": "--fsdp-devices", "label": "FSDP 切分设备数",
      "type": "int", "default": 1, "min": 1, "max": 64, "required": false,
      "help": "1=纯数据并行；须整除设备总数，单卡固定1",
      "source": "src/openpi/training/config.py:535; 校验 src/openpi/training/sharding.py:17-23"
    }
  ],
  "extra_args": {"enabled": true}
}
```

### 8.1 补充：应纳入模板但**必须折叠**的参数

以下参数真实存在，但普通用户不该看到（原因见 §9）：

| flag | 默认 | 折叠理由 |
|---|---|---|
| `--overwrite` / `--resume` | `False` | **破坏性**（`--overwrite` 会 `rmtree`，`checkpoints.py:27`）；互斥 |
| `--seed` | `42` | 复现性用，非常规调整 |
| `--project-name` | `openpi` | wandb 工程名，形式项 |
| `--wandb-enabled` | `True` | 离线环境必备开关，放"日志"折叠区 |
| `--num-workers` 以外的性能项 | — | 见下 |
| `--model.paligemma-variant` | `gemma_2b` | **改为 LoRA 后无 CLI 配套 freeze_filter → 单独选它会让 LoRA 参数随机初始化，必须整体走 §5.2 方案** |
| `--model.action-expert-variant` | `gemma_300m` | 同上 |
| `--model.max-token-len` | 200 | 仅影响显存/截断，误调易静默损失动作 token |
| `--model.pi05` / `--model.discrete-state-input` | `True` / `False` | 架构开关，选错=训练出坏模型 |
| `--optimizer.b1` / `--optimizer.b2` / `--optimizer.eps` / `--optimizer.weight-decay` | 0.9 / 0.95 / 1e-8 / 1e-10 | 极少需要调；`weight_decay=0` 会 OOM（`optimizer.py:72-73`） |
| `--pytorch-weight-path` / `--pytorch-training-precision` / `--model.pytorch-compile-mode` | None / bfloat16 / max-autotune | 只对 PyTorch 后端生效；`pi05_libero` 的默认值是占位符 `/path/to/your/pytorch_weight_path`（`config.py:761`） |
| `--policy-metadata` | `{fixed}` | tyro 标记 fixed，**不可改，不应渲染** |
| `--data.extra-delta-transform` | False（该 config） | help 显示值与真实值不一致（§0），误读风险高 |
| `--data.use-delta-joint-actions` / `--data.adapt-to-pi` / `--data.default-prompt` / `--data.action-sequence-keys` | — | 仅 Aloha 系 config 存在（§1.6） |
| `--data.rlds-data-dir` / `--data.action-space` / `--data.datasets.*` | — | 仅 RLDS/DROID config 存在，且 `num_workers` 必须 0 |

---

## 9. 给表单实现的建议（优先级）

### 9.1 建议"默认折叠"的参数（**核心结论**）

**第一层（永远不露出）：**
1. `--policy-metadata` —— CLI 上是 `{fixed}`，渲染出来只会误导。
2. `--pytorch-weight-path` / `--pytorch-training-precision` / `--model.pytorch-compile-mode` —— 与 JAX 主路径无关，且默认值是占位符字符串。

**第二层（"高级/架构"折叠，改了容易练坏）：**
3. `--model.pi05`、`--model.discrete-state-input`、`--model.paligemma-variant`、`--model.action-expert-variant`、`--model.max-token-len` —— 这 5 个决定"你到底在训哪个模型"，应由 `config_name` 唯一决定；单独暴露会让用户拼出不存在的组合（尤其 `*_lora` 变体缺少 `freeze_filter` 配套）。
4. `--optimizer.b1/b2/eps/weight-decay` —— 官方从未在外层 config 里覆盖过（只有 `clip-gravity-norm`，`config.py:758`）。
5. `--data.*` 的 transforms 相关（`extra-delta-transform`、`use-delta-joint-actions`、`adapt-to-pi`、`action-sequence-keys`） —— 与数据集强耦合，应由数据集模板推导。

**第三层（"运维"折叠，但要能一键展开）：**
6. `--overwrite` / `--resume` —— 有破坏性，建议改用"新实验/续训"两个业务按钮映射，而不是让用户直接勾选；并做互斥校验。
7. `--seed`、`--project-name`、`--wandb-enabled`、`--keep-period`、`--assets-base-dir`、`--checkpoint-base-dir`（后两者建议由平台统一注入）。
8. `--fsdp-devices` —— 单卡场景固定 1 并隐藏；多卡场景自动按设备数推荐约数。

**默认展开给普通用户的（建议就这 6 个）：**
`config_name`（选模型）、`exp_name`、`num_train_steps`、`batch_size`、`peak_lr`、`repo_id`。
其余（`save_interval`、`log_interval`、`num_workers`、`weight_loader_params_path`、`ema_decay`、`clip_gradient_norm`、`action_horizon`、`action_dim`、`model_dtype`）放"第二屏/高级设置"展开，默认不打断主流程。

### 9.2 表单必须内建的业务校验（来自源码硬约束）

1. `exp_name` 非空。
2. `batch_size % device_count == 0`（`train.py:198-201`）。
3. `batch_size < dataset_len`（`data_loader.py:415-416`）。
4. `device_count % fsdp_devices == 0`（`sharding.py:18-21`）。
5. `! (overwrite && resume)`（`config.py:554-556`）。
6. 训练前存在 `<assets_base_dir>/<config_name>/<asset_id or repo_id>/norm_stats.json`，否则拦下并提示"先算 norm stats"或"复用预训练统计量"。
7. 数据集 `meta/info.json.codebase_version ∈ {v2.0, v2.1}`，v3.0 拦截（§4）。

### 9.3 "在训练容器里自动算 norm stats"的落地建议

容器 entrypoint 伪代码（不改 openpi 源码，安全）：

```bash
CFG="$1"; REPO_ID="$2"; STATS="${ASSETS_BASE_DIR:-./assets}/${CFG}/${ASSET_ID:-$REPO_ID}/norm_stats.json"
if [ ! -f "$STATS" ]; then
  uv run scripts/compute_norm_stats.py --config-name="$CFG" ${MAX_FRAMES:+--max-frames="$MAX_FRAMES"}
fi
exec env XLA_PYTHON_CLIENT_MEM_FRACTION="${MEM_FRACTION:-0.9}" \
  uv run scripts/train.py "$CFG" --exp-name="$EXP_NAME" "${OVERWRITE_OR_RESUME}"
```

- 幂等（存在即跳过），失败时训练阶段会以 `ValueError: Normalization stats not found` 明确报错（不会静默）。
- 若用户显式选择了"复用预训练统计量"（设了 `--data.assets.assets-dir`），上面分支永远不会命中，自然跳过。

### 9.4 4090 48G 的推荐预设（可作为表单的"预设按钮"）

| 预设 | 取值 |
|---|---|
| 模式 | LoRA（需 §5.2 的 config 方案，**不是**纯 CLI 能实现） |
| `num_train_steps` | 30000 |
| `batch_size` | 4（保守）/ 8（乐观，需实测） |
| `peak_lr` / `warmup_steps` | 5e-5 / 1000（LoRA 时缩短 warmup；官方 LoRA 例程用全局默认 1000） |
| `ema_decay` | **None**（LoRA 官方约定） |
| `fsdp_devices` | 1 |
| `action_horizon` | 10（π0.5 微调常用） |
| `num_workers` | 4~8 |
| `save_interval` | 2000 |
| 环境变量 | `XLA_PYTHON_CLIENT_MEM_FRACTION=0.9` |
| 预期显存 | ≥ 22.5 GB 起（官方口径），**48G 有充足余量但 batch 上限未实测** |

---

## 10. 明确"未核实 / 查不到"清单（请勿当作事实使用）

1. **π0 论文附录 A-B 的训练超参（batch size、lr、optimizer、EMA decay）** —— arXiv HTML 正文在附录前截断，`web_search` 在本会话不可用（报 `DEEPSEEK_API_KEY` 缺失），未能取得。已核实的只有 700k 步、H=50、10 步 Euler、3.3B 参数、`n^0.43` 数据加权。
2. **π0.5 论文的具体 batch/lr/步数** —— 只取到摘要页。
3. **π0-FAST 论文（FAST tokenizer）的推荐训练配置** —— 本次未调研论文原文；仓库内 `pi0_fast_libero` 数值（batch 32、30k 步、默认 lr 2.5e-5）已核实。
4. **4090 48G 上的实际吞吐 / 显存曲线** —— 无 GPU 可实测；只有官方"LoRA >22.5GB、全量 >70GB"的粗口径。
5. **LeRobot v3.0 的具体格式细节**（parquet 布局、meta 字段变化） —— 未逐条抓取上游 v3 文档；"openpi 不支持 v3" 这一结论基于 pin 的 `CODEBASE_VERSION="v2.1"` + `backward_compatibility.py` 的 FUTURE_MESSAGE 逻辑，**属于强推断而非实测**（未拿真实 v3 数据集跑过）。
6. **`docs/custom_data.md`** —— **该文件不存在**；`docs/` 仅 `docker.md`、`norm_stats.md`、`remote_inference.md`。等价说明见 `README.md:124-165`。
7. **梯度累积** —— 源码中不存在，任何"openpi 支持 grad accum"的说法都是错的（除非你自行改 `train.py`）。
8. **`pi05_libero` 中 `pytorch_weight_path` 的占位值** `/path/to/your/pytorch_weight_path`（`config.py:761`）—— 这是仓库里遗留的占位符，不是可用路径；JAX 后端不使用该字段。

---

## 11. 源码出处索引（便于逐条复核）

| 主题 | 文件:行 |
|---|---|
| CLI 入口 | `scripts/train.py:279-280`；`src/openpi/training/config.py:978-979` |
| 参数校验（batch/device） | `scripts/train.py:198-201` |
| 训练主循环 / 日志 / 存盘 | `scripts/train.py:250-276` |
| EMA 更新 | `scripts/train.py:169-175` |
| 冻结参数转 bf16 | `scripts/train.py:102-104` |
| 权重加载 + 形状校验 | `scripts/train.py:73-81` |
| `TrainConfig` 全字段 | `src/openpi/training/config.py:465-557` |
| 各 config 超参 | `config.py:651-763`（libero）、`764-827`（aloha）、`828-918`（droid） |
| `extra_delta_transform` | `config.py:289`、`669`(True)、`749`(False) |
| `freeze_filter` / `get_freeze_filter` | `config.py:493`、`pi0_config.py:88-117`、`pi0_fast.py:127-131` |
| LR schedule / AdamW | `src/openpi/training/optimizer.py:15-31`、`65-85` |
| 权重加载器 | `src/openpi/training/weight_loaders.py:31-54`、`76-103` |
| 模型默认超参 | `src/openpi/models/pi0_config.py:18-48`、`src/openpi/models/pi0_fast.py:76-131` |
| 归一化 | `src/openpi/training/data_loader.py:172-220`、`src/openpi/shared/normalize.py:9-146` |
| norm stats 脚本 | `scripts/compute_norm_stats.py:89-117` |
| 数据集构建（LeRobot） | `data_loader.py:130-151`、`381-483` |
| 检查点目录/overwrite/resume | `src/openpi/training/checkpoints.py:20-86` |
| 分片约束 | `src/openpi/training/sharding.py:17-23` |
| LeRobot pin / 版本 | `pyproject.toml` `[tool.uv.sources]`；venv `lerobot/common/datasets/lerobot_dataset.py:77`、`backward_compatibility.py` |
| 显存口径 | `README.md:24-30`、`README.md:154-163` |
| 预训练统计量表 | `docs/norm_stats.md:31-69` |
| LIBERO 复现结果 | `examples/libero/README.md` Results 表 |