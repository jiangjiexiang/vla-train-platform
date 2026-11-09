# LeRobot + SmolVLA 训练参数体系调研报告

> 调研时间：2026-11-09
> 调研方法：**以真实安装的源码为准**（不是纯网络检索），交叉验证 4 个版本。

## 0. 版本矩阵与证据来源

| 版本 | 证据来源 | 说明 |
|---|---|---|
| **0.4.4** | 服务器 `172.16.122.37` 实际安装：`/home/jjx/workspace/smolvla/.venv/lib/python3.10/site-packages/lerobot` | **生产在用版本**，`lerobot-train --help` 实测输出 |
| 0.3.3 | PyPI wheel 解包 `/tmp/lv/x33/` | `lerobot-0.3.3-py3-none-any.whl` |
| 0.5.0 | PyPI wheel 解包 `/tmp/lv/x50/` | `lerobot-0.5.0-py3-none-any.whl` |
| 0.6.2 | 本地源码 `/home/robam/lerobot`（`pyproject.toml:28`） | 最新版，部分参数**已改名** |

> ⚠️ **最重要的一句话**：本任务的下游实现应**按 0.4.4 建模**（服务器实际版本），但要为 0.6.x 预留别名映射。

---

## 1. 核心结论：下划线 vs 中划线

**`lerobot-train` 全部使用下划线 + 点号分组**，实测自 0.4.4 `--help`：

```
--batch_size        --output_dir       --save_freq        --policy.device
--num_workers       --eval_freq        --log_freq         --policy.path
--dataset.repo_id   --dataset.root     --dataset.video_backend
--wandb.enable      --steps            --seed
```

**不存在** `--batch-size` / `--output-dir` 的中划线写法。

**实测证据**（服务器 0.4.4）：
```bash
$ lerobot-train --batch-size=16 --steps=10
lerobot-train: error: unrecognized arguments: --batch-size=16     # ← 直接拒绝

$ lerobot-train --batch_size=16 --steps=10
draccus.utils.DecodingError: Missing required field(s) `dataset`  # ← 参数被接受，只是缺 dataset
```

> 另注：`--policy.path` **不会出现在 `--help` 里**（因为它不是 dataclass 字段，而是由 `parser.py` 的
> `filter_path_args()` 在 draccus 解析前剥离处理）。但它是真实可用的——实测传
> `--policy.path=/nonexistent_dir_xyz` 会被当作 Hub repo id 去校验并报 `HFValidationError`，
> 说明该 flag 已被正确识别。表单实现时**不要**因为 `--help` 里搜不到就以为它不存在。

原因：参数由 `draccus` 从 dataclass 字段名直接生成（`src/lerobot/configs/parser.py` 的 `wrap()`），字段名即 flag 名。

> ⚠️ **例外（真实踩坑点）**：数据集迁移脚本 `python -m lerobot.datasets.v30.convert_dataset_v21_to_v30`
> 用的是 **`--repo-id`（中划线）**，见 0.4.4 `datasets/v30/convert_dataset_v21_to_v30.py:527`。
> 同一个项目里两种风格并存，**不能想当然**。

另一个格式约束：普通字段 `--key=value`。
但 **dict 类型字段（`--rename_map`）和 list 类型字段不支持 `key=value` 解析**，只能用空格分隔。例如
`--policy.optimizer_betas 0.9 0.95`。

---

## 2. 最小可用微调命令

### 2.1 官方最小示例（0.4.4 `docs/source/smolvla.mdx`）

```bash
lerobot-train \
  --policy.path=lerobot/smolvla_base \
  --dataset.repo_id=${HF_USER}/mydataset \
  --batch_size=64 \
  --steps=20000 \
  --output_dir=outputs/train/my_smolvla \
  --job_name=my_smolvla_training \
  --policy.device=cuda \
  --wandb.enable=true
```

### 2.2 纯离线 / 自定义数据集最小命令（本服务器已验证可用）

服务器上真实在跑的 `~/workspace/smolvla/train_smolvla.sh`：

```bash
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
lerobot-train \
  --policy.path=/home/jjx/workspace/models/smolvla_base \
  --dataset.repo_id=local/1028pick_3.0 \
  --dataset.root=/home/jjx/1028pick_3.0 \
  --dataset.video_backend=pyav \
  --output_dir=~/workspace/outputs/smolvla_pick3 \
  --job_name=smolvla_pick3 \
  --batch_size=16 --steps=30000 --save_freq=5000 --log_freq=100 \
  --num_workers=4 --policy.device=cuda \
  --policy.repo_id=local/smolvla_pick3 --policy.push_to_hub=false \
  --wandb.enable=false
```

### 2.3 绝对最小（只有 3 个必填）

```bash
lerobot-train --policy.path=<基座权重目录> --dataset.repo_id=<任意id> --dataset.root=<数据集目录>
```

> `--dataset.repo_id` 即使纯本地也**必填**（`DatasetConfig.repo_id: str` 无默认值）。
> `--policy.repo_id` 在 `push_to_hub=true`（默认！）时不填会直接报错，见 §6.2。

---

## 3. `TrainPipelineConfig` 完整字段（0.4.4）

来源：`site-packages/lerobot/configs/train.py:37-77` + `configs/default.py`

| 字段 | 默认值 | 备注 |
|---|---|---|
| `dataset` | `DatasetConfig` | 必填 |
| `env` | `None` | 仿真评测环境，微调不需要 |
| `policy` | `None` | `--policy.path` 或 `--policy.type` 二选一 |
| `output_dir` | `None` | 默认 `outputs/train/{日期}/{时间}_{job_name}` |
| `job_name` | `None` | 默认取 policy.type |
| `resume` | `False` | 断点续训开关 |
| `seed` | `1000` | |
| `cudnn_deterministic` | `False` | 0.5.0+ 才有 |
| `num_workers` | `4` | |
| `batch_size` | `8` | |
| `steps` | `100000` | |
| `eval_freq` | `20000` | **0.6.2 改名为 `env_eval_freq`** |
| `log_freq` | `200` | |
| `tolerance_s` | `1e-4` | 视频解码时间戳容差 |
| `save_checkpoint` | `True` | |
| `save_freq` | `20000` | |
| `use_policy_training_preset` | `True` | 用 policy 自带的 optimizer/scheduler 预设 |
| `optimizer` / `scheduler` | `None` | 预设组自动填充 |
| `eval` | `EvalConfig` | 仿真评测 |
| `wandb` | `WandBConfig` | |
| `peft` | `None` | 0.5.0+ |
| `use_rabc` / `rabc_*` | `False` | 0.5.0+，0.6.2 已迁移为 `sample_weighting` |
| `rename_map` | `{}` | **0.5.0+ 才有**，用于重映射相机/状态 key |

---

## 4. `SmolVLAConfig` 字段（0.4.4 `policies/smolvla/configuration_smolvla.py`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `n_obs_steps` | 1 | |
| `chunk_size` | 50 | 一次预测的动作长度 |
| `n_action_steps` | 50 | 每次执行几步；须 ≤ chunk_size |
| `max_state_dim` / `max_action_dim` | 32 / 32 | 短向量补零到该维度 |
| `resize_imgs_with_padding` | (512, 512) | **图像统一缩放到 512×512** |
| `empty_cameras` | 0 | 缺相机时用空图占位 |
| `tokenizer_max_length` | 48 | |
| `num_steps` | 10 | 推理去噪步数 |
| `use_cache` | True | |
| `freeze_vision_encoder` | True | |
| `train_expert_only` | True | |
| `train_state_proj` | True | |
| `optimizer_lr` | 1e-4 | |
| `optimizer_betas` | (0.9, 0.95) | |
| `optimizer_weight_decay` | 1e-10 | |
| `optimizer_grad_clip_norm` | 10 | |
| `scheduler_warmup_steps` | 1000 | |
| `scheduler_decay_steps` | 30000 | |
| `scheduler_decay_lr` | 2.5e-6 | |
| `vlm_model_name` | `HuggingFaceTB/SmolVLM2-500M-Video-Instruct` | **独立加载，见 §6.3** |
| `load_vlm_weights` | `False` | ⚠️ 加载 `--policy.path` 时会被覆盖为 True |
| `attention_mode` | `cross_attn` | |
| `num_vlm_layers` | 16 | |
| `num_expert_layers` | -1 | ≤0 表示与 VLM 层数相同 |
| `self_attn_every_n_layers` | 2 | |
| `expert_width_multiplier` | 0.75 | |
| `compile_model` | False | |

### 基础类 `PreTrainedConfig` 字段
`device`(None→自动)、`use_amp`(False)、`push_to_hub`(**True**)、`repo_id`(None)、`private`(None)、`tags`(None)、`license`(None)、`pretrained_path`(None)

> ⚠️ `use_amp` 在 `--help` 里**存在且默认 False**，但 SmolVLA 内部以 bf16 加载 VLM
> （`smolvlm_with_expert.py:78` `torch_dtype="bfloat16"`），实测训练已自动用 autocast。
> 普通用户不要碰 `use_amp`。

---

## 5. 关键问题解答

### 5.1 `dataset.repo_id` vs `dataset.root`

源码（0.4.4 `datasets/lerobot_dataset.py:97`）：
```python
self.root = Path(root) if root is not None else HF_LEROBOT_HOME / repo_id
```

| | `repo_id` | `root` |
|---|---|---|
| 作用 | **数据集标识符**（逻辑 ID） | **实际磁盘目录**（物理路径） |
| 缺省 | **必填**，无默认 | `None` → `$HF_LEROBOT_HOME/repo_id`（默认 `~/.cache/huggingface/lerobot/...`） |
| 本地场景 | 随便起名，如 `local/mydata` | 指向含 `meta/info.json` 的目录 |

- **纯本地**：`root` 必给，`repo_id` 只是个标签。
- **Hub 数据集**：只给 `repo_id`，自动下载到缓存。
- 常见错误：给了 `repo_id=lerobot/pusht` 又给 `root=/my/local/dir`，此时 **`root` 优先**，会读本地目录，若目录不存在则报 `meta/info.json` 缺失。

### 5.2 从本地目录加载基座权重（不用 HF hub）

```bash
--policy.path=/home/jjx/workspace/models/smolvla_base
```

`--policy.path` 若是**存在的目录**，`PreTrainedConfig.from_pretrained()` 直接读 `config.json`，完全不访问网络
（`configs/policies.py:187` `if Path(model_id).is_dir()`）。

**但仅此不够**，还需满足两点（见 §6.3）：
1. `config.json` 内 `vlm_model_name` 要指向本地 VLM 目录；
2. 设置 `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`，确保无任何兜底联网。

服务器实测：`smolvla_base/config.json` 的 `"vlm_model_name": "/home/jjx/workspace/models/SmolVLM2-500M"` — 已被改成绝对路径。

### 5.3 `resume` / 断点续训的正确参数

**正确姿势（0.4.4）**：
```bash
lerobot-train --resume=true --config_path=<output_dir>/checkpoints/last/pretrained_model/train_config.json
```
- 也可指向 `.../checkpoints/last/pretrained_model/` 目录本身。
- `resume=true` 时**配置以 checkpoint 内的 `train_config.json` 为准**，CLI 传的其它参数会被忽略——想改步数必须改 `train_config.json`。
- 续训从 `checkpoints/last`（符号链接 → 最新 step）继续。

**checkpoint 实际结构（服务器实测）**：
```
outputs/smolvla_pick3/checkpoints/
├── 005000/ 010000/ ... 030000/
│   ├── pretrained_model/        # 可直接用于推理
│   │   ├── config.json  model.safetensors
│   │   ├── policy_preprocessor.json  policy_postprocessor.json
│   │   └── train_config.json
│   └── training_state/          # 续训所需
│       ├── optimizer_state.safetensors  optimizer_param_groups.json
│       ├── scheduler_state.json  rng_state.safetensors  training_step.json
└── last -> 030000
```

> ⚠️ **0.6.2 差异**：`--resume` 仍需 `--config_path`，但语义变更为"从 Hub repo id 或本地 checkpoint 载入"；
> 且非 resume 时若 `output_dir` 已存在会**直接抛 `FileExistsError`**（`train.py:299`）。
> 0.4.4 没有这个检查（会覆盖）。

### 5.4 `policy.path` vs `policy.type`

源码 `configs/parser.py` 的 `filter_path_args()`：
```python
if get_path_arg(field, args) and get_type_arg(field, args):
    raise ArgumentError("Cannot specify both --policy.path and --policy.type")
```

| | `--policy.path` | `--policy.type` |
|---|---|---|
| 含义 | 从**已有权重**加载（微调） | 从**零初始化**某架构 |
| 值 | 目录路径或 Hub repo id | `{act,diffusion,pi0,pi05,smolvla,...}` |
| 权重 | 载入 `model.safetensors` | 随机初始化 |
| 用途 | **微调必用** | 从零训练 |

**两者互斥**。微调 SmolVLA 时只能 `--policy.path`；用 `--policy.type=smolvla` 会得到随机权重（且需 `load_vlm_weights=true` 才有 VLM 先验）。

### 5.5 常见坑

| # | 坑 | 现象 | 解法 |
|---|---|---|---|
| 1 | **torchcodec** | `--help` 能跑，一开始训练就崩 | 0.3.3~0.5.0 只用 `find_spec` 检查（**本体能 import 不代表能解码**，常缺 ffmpeg 动态库）→ 显式 `--dataset.video_backend=pyav` |
| 2 | **视频解码后端** | 0.6.2 加了真实 `import` 探测，失败自动回落 pyav（`utils/import_utils.py:74`）；0.3~0.5 **无此保护** | 统一显式指定 `pyav` 最稳 |
| 3 | **warmup** | `scheduler_warmup_steps=1000`，但微调常只跑 5k~20k 步 → 前 1000 步 lr 从 0 爬升，占比过大 | 步数 <10k 时建议降到 100~300 |
| 4 | **`load_vlm_weights`** | 独立跑时默认 False，从零训；但加载 `--policy.path` 后会被覆盖 True | 别手动设 False |
| 5 | **`vlm_model_name` 仍联网** | 设了 `--policy.path` 本地目录，仍去 HF 拉 SmolVLM2 | 改 `config.json` 的 `vlm_model_name` 为本地绝对路径 + `HF_HUB_OFFLINE=1` |
| 6 | **tokenizer 路径硬编码** | 换了机器/目录，`policy_preprocessor.json` 里 `tokenizer_name` 是旧绝对路径 | 迁移权重目录时同步改，或保证目录不变 |
| 7 | **`push_to_hub` 默认 True** | 没给 `--policy.repo_id` 直接 ValueError | `--policy.push_to_hub=false` |
| 8 | **`resume` 吃旧配置** | 改了 `--steps` 不生效 | 改 checkpoint 里的 `train_config.json` |
| 9 | **`output_dir` 已存在** | 0.6.2 直接 `FileExistsError` | 换目录或 `--resume=true` |
| 10 | **v2.1 数据集在 0.4.x 加载失败** | `BackwardCompatibilityError` | 先跑 `convert_dataset_v21_to_v30 --repo-id=...` |
| 11 | **`--rename_map` 0.5.0 才有** | 0.4.4 上无此参数 | 新版本才能重映射相机名 |

---

## 6. 数据集格式差异（v2.0 / v2.1 / v3.0）

### 6.1 目录结构对比

**v2.0 / v2.1**（源码 `lerobot==0.3.3` `datasets/utils.py:58-61`）：
```
meta/info.json
meta/episodes.jsonl          ← JSONL
meta/episodes_stats.jsonl    ← JSONL
meta/tasks.jsonl             ← JSONL
data/chunk-000/episode_000000.parquet          ← 每 episode 一个文件
videos/chunk-000/{video_key}/episode_000000.mp4 ← 每 episode 一个视频
```
```python
DEFAULT_VIDEO_PATH  = "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4"
DEFAULT_PARQUET_PATH= "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
```

**v3.0**（服务器实测 `/home/jjx/1028pick_3.0`，`codebase_version: "v3.0"`）：
```
meta/info.json
meta/stats.json
meta/tasks.parquet                    ← 改为 parquet
meta/episodes/chunk-000/file-000.parquet   ← 改为 parquet + 分片
data/chunk-000/file-000.parquet       ← 多 episode 合并成大文件
videos/{video_key}/chunk-000/file-000.mp4  ← ★ 路径层级翻转
```
```python
DEFAULT_VIDEO_PATH = "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"
DEFAULT_DATA_PATH  = "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
```

### 6.2 ★ v3.0 的 videos 目录结构变化（重点）

**v2.x**：`videos/chunk-XXX/{video_key}/episode_XXXXXX.mp4`
—— chunk 在**外层**，相机 key 在**内层**，且**一个 episode 一个 mp4**。

**v3.0**：`videos/{video_key}/chunk-XXX/file-XXXXXX.mp4`
—— **相机 key 提到最外层**，chunk 在**内层**，且**多个 episode 共用一个 mp4**。

> 影响：任何按 `videos/chunk-*/` 找文件、或假设"一个 mp4 = 一个 episode"的下游代码，在 v3.0 下**全部失效**。
> episode 与视频的对应关系不再靠文件名，而是靠 `meta/episodes/*.parquet` 里的
> `videos/{key}/chunk_index`、`videos/{key}/file_index`、`videos/{key}/from_timestamp`、`to_timestamp` 定位。

### 6.3 parquet 字段（服务器实测 v3.0）

`data/chunk-000/file-000.parquet` 列：
`observation.state`, `action`, `episode_index`, `frame_index`, `timestamp`, `index`, `task_index`

注意：**图像不在 parquet 里**，v3.0 的 parquet 只存低维信号；视频单独存 mp4，靠上面 4 个字段定位帧。
（v2.x 若用 `dtype: "image"` 则是逐帧图片存 `images/` 目录。）

### 6.4 `meta/info.json` 的 `features`（v3.0 实测）

```json
"observation.images.camera1": {
  "dtype": "video",
  "shape": [480, 640, 3],
  "info": {
    "video.height": 480, "video.width": 640, "video.codec": "h264",
    "video.pix_fmt": "yuv420p", "video.is_depth_map": false,
    "video.fps": 30, "video.channels": 3, "has_audio": false
  }
}
```
- v3.0 多出嵌在 feature 里的 **`info` 子字典**（视频编码参数）。
- `observation.state` / `action` 均为 `{"dtype":"float32","shape":[N],"names":[...],"fps":30.0}`；v3.0 的 shape 已是 `(N,)`，v2.0 曾有 `(h,w,channel)` 顺序 bug 需要兼容处理（`dataset_to_policy_features` 里专门兜底）。
- `info.json` 顶层关键字段：`codebase_version`, `robot_type`, `total_episodes`, `total_frames`, `total_tasks`, `chunks_size`, `fps`, `splits`, `data_path`, `video_path`, `features`。

### 6.5 版本兼容性

- 0.4.x 起 `CODEBASE_VERSION = "v3.0"`；加载 v2.1 数据集直接抛 `BackwardCompatibilityError`
  → `python -m lerobot.datasets.v30.convert_dataset_v21_to_v30 --repo-id=<id>`（**注意中划线**）。
- 服务器当前数据集 `1028pick_3.0` 即 v3.0，与 0.4.4 匹配。

---

## 7. RTX 4090 48GB 实测配置

**实测环境**：`172.16.122.37`，RTX 4090 49140 MiB，driver 595.71，torch 2.10.0+cu128，lerobot 0.4.4。
**实测方法**：真实加载 `smolvla_base` + 前向+反向，3 路 480×640 相机。

### 7.1 参数量
- 总参数 **450.0M**；可训练 **99.9M（22.2%）** —— `train_expert_only=true` + `freeze_vision_encoder=true` 生效。
- 权重常驻显存 **0.86 GiB**。

### 7.2 显存实测（前向+反向峰值）

| 输入分辨率 | bs=16 | bs=32 | bs=64 |
|---|---|---|---|
| 480×640 ×3 cam | 5.58 GiB | 10.19 GiB | **19.46 GiB** |
| 256×256 ×3 cam | 5.46 GiB | 9.95 GiB | 18.94 GiB |

### 7.3 训练速度实测（真实日志 `~/workspace/outputs/smolvla_pick3.log`）

```
bs=16, steps=30000 → 总耗时 5h34m，avg 1.5 step/s，updt_s≈0.21s，data_s≈0.46s
step 30K: loss 0.013, epch 7.9
```

### 7.4 结论与建议

| 项 | 建议 |
|---|---|
| **batch_size** | **64 完全放得下**（19.5 GiB / 48 GiB）。推荐 32~64；`data_s≈0.46s > updt_s≈0.21s` 说明**瓶颈在 dataloader 不在 GPU**，加大 batch 反而提高 GPU 利用率 |
| **num_workers** | 8~16（当前 4 偏保守，是 data 慢的主因） |
| **分辨率** | ⚠️ **分辨率对显存几乎无影响**（480p vs 256p 差 <4%），因为 `resize_imgs_with_padding=(512,512)` 会**统一缩放**。真正的分辨率由 `--policy.resize_imgs_with_padding` 决定 |
| **可跑多大分辨率** | 512×512（默认，256 token/相机）显存无忧；降到 384×384（144 tok）或 256×256（64 tok）可显著提速、扩上下文，但**会掉精度**；**不建议超过 512**（SmolVLM2 的 `max_image_size.longest_edge=512`，再大也被截断） |
| **相机数** | 3 路是基座配置；少于 3 路可用 `--policy.empty_cameras=N` 补空图 |

> `resize_imgs_with_padding` 是 `[int int]` 类型，需空格分隔：`--policy.resize_imgs_with_padding 384 384`

---

## 8. 参数表：实测来源标注

| flag | 类型 | 默认 | 源码出处（0.4.4） |
|---|---|---|---|
| `--policy.path` | path | None | `configs/parser.py` PATH_KEY 机制（**不在 `--help` 中，但真实可用**） |
| `--policy.device` | str | None | `configs/policies.py` PreTrainedConfig.device |
| `--dataset.repo_id` | str | **必填** | `configs/default.py:34` |
| `--dataset.root` | path | None | `configs/default.py:41` |
| `--dataset.video_backend` | str | torchcodec/pyav | `datasets/video_utils.py:117` |
| `--dataset.episodes` | list | None | `configs/default.py` |
| `--dataset.streaming` | bool | False | 0.5.0+ |
| `--batch_size` | int | 8 | `configs/train.py:55` |
| `--steps` | int | 100000 | `configs/train.py:56` |
| `--num_workers` | int | 4 | `configs/train.py:54` |
| `--seed` | int | 1000 | `configs/train.py:52` |
| `--log_freq` | int | 200 | `configs/train.py:58` |
| `--save_freq` | int | 20000 | `configs/train.py:63` |
| `--eval_freq` | int | 20000 | `configs/train.py:57` |
| `--save_checkpoint` | bool | True | `configs/train.py:61` |
| `--output_dir` | path | None | `configs/train.py:43` |
| `--job_name` | str | None | `configs/train.py:44` |
| `--resume` | bool | False | `configs/train.py:49` |
| `--config_path` | path | None | `configs/parser.py` |
| `--wandb.enable` | bool | False | `configs/default.py` WandBConfig |
| `--policy.push_to_hub` | bool | **True** | `configs/policies.py:70` |
| `--policy.repo_id` | str | None | `configs/policies.py:71` |
| `--policy.chunk_size` | int | 50 | `policies/smolvla/configuration_smolvla.py` |
| `--policy.n_action_steps` | int | 50 | 同上 |
| `--policy.freeze_vision_encoder` | bool | True | 同上 |
| `--policy.train_expert_only` | bool | True | 同上 |
| `--policy.train_state_proj` | bool | True | 同上 |
| `--policy.vlm_model_name` | str | SmolVLM2-500M-Video-Instruct | 同上 |
| `--policy.load_vlm_weights` | bool | False | 同上 |
| `--policy.optimizer_lr` | float | 1e-4 | 同上 |
| `--policy.scheduler_warmup_steps` | int | 1000 | 同上 |
| `--policy.scheduler_decay_steps` | int | 30000 | 同上 |
| `--policy.resize_imgs_with_padding` | [int int] | (512,512) | 同上 |
| `--policy.empty_cameras` | int | 0 | 同上 |
| `--policy.use_amp` | bool | False | `configs/policies.py:65` |

前 3 个跟 `--policy.path` 冲突（互斥）的：`--policy.type`。

---

## 9. 表单设计建议：哪些暴露、哪些隐藏

### ✅ 必须露出（普通用户必填/高频调）

| 参数 | 控件 | 说明 |
|---|---|---|
| `dataset.root` | 目录选择器 | 本地数据集路径（**最常错**） |
| `dataset.repo_id` | 文本框 | 自动从目录名生成，可编辑 |
| `policy.path` | 目录选择器/下拉 | 基座权重目录，默认 `smolvla_base` |
| `output_dir` | 目录选择器 | 自动生成时间戳子目录 |
| `steps` | 数字 | 默认 20000，旁注"50 episodes ≈ 20k 步" |
| `batch_size` | 数字 | 默认 32（4090 48G 推荐），旁注实测显存 |
| `policy.device` | 下拉 | cuda/cpu |

### ⚙️ 进阶折叠区（默认收起）

`num_workers`(默认 8)、`save_freq`(5000)、`log_freq`(100)、`seed`、`dataset.video_backend`(默认 pyav)、`wandb.enable`、`policy.push_to_hub`(默认 **false**，覆盖上游默认)、`policy.repo_id`（仅 push_to_hub=true 时显示）、`resume` + `config_path`（用开关联动）

### 🚫 建议隐藏（普通用户勿碰）

| 参数 | 理由 |
|---|---|
| `policy.type` | 与 `policy.path` 互斥，选了就变成从零训练 |
| `policy.load_vlm_weights` | 由 `policy.path` 自动覆盖，手动设反会废掉微调 |
| `policy.vlm_model_name` | 需与权重目录强绑定；改错→偷偷联网或维度不匹配 |
| `policy.use_amp` | SmolVLA 内部已 bf16，改了反而可能出 NaN |
| `policy.optimizer_lr` / `scheduler_*` | 官方预设已调好，改错的代价高 |
| `policy.chunk_size` / `n_action_steps` | **必须与推理端一致**，训练时改会导致部署错乱 |
| `policy.freeze_vision_encoder` / `train_expert_only` | 改了就变成全量微调，显存/效果双崩 |
| `policy.resize_imgs_with_padding` | 改错影响很大，且不省显存（见 §7.4） |
| `tolerance_s`、`cudnn_deterministic`、`use_policy_training_preset`、`optimizer`/`scheduler`、`eval`、`env`、`peft`、`rabc*`、`rename_map` | 调试/高级用途 |

> 建议「高级模式」全局开关才放出隐藏项，并保留 `extra_args` 透传原始 flag。

---

## 10. ⚠️ 不确定项 / 未验证项（明确声明）

1. **`eval_freq` 的实际语义未验证**：0.4.4 中它驱动仿真评测（`cfg.env`），**纯数据集微调时设了也不会跑评测**。若想要离线 eval loss，0.6.2 才有 `--eval_steps`（需 `dataset.eval_split>0`）。
2. **batch 64 整链路未跑过**：§7.2 的显存是"模型前向+反向"实测，**不含** dataloader 的解码缓冲与分级缓存；整链路峰值会更高（估计 +2~5 GiB），仍应能在 48G 内。
3. **未实测 `--wandb.enable`**：本环境无 wandb 账号，仅从源码确认字段存在。
4. **`--dataset.streaming=true` 未验证**：0.5.0+ 参数，需配合 Hub，本地目录场景行为未测。
5. **0.6.2 的部分参数**（`checkpoint_format`、`accelerator.*`、`parallelism.*`、`ema.*`、`sample_weighting`）是新增/重构项，**建议本项目不实现**，标注为"0.6.x 专属"。
6. **v2.0 与 v2.1 的具体差异**未逐项核对：源码里 `check_version_compatibility` 显示二者为**兼容**关系（v2.1 仅 minor 升级 + warning），非破坏性变更；关键破坏性变更发生在 **v2.1 → v3.0**。v2.0 特有的 `names[2] in ["channel","channels"]` 顺序 bug 见 `dataset_to_policy_features`。
7. **v3.0 与 `chunks_size`**：实测数据集 `chunks_size=1000` 但所有数据在一个 `file-000` 内，说明 v3 的分片由**文件大小**（`DEFAULT_DATA_FILE_SIZE_IN_MB=100`）驱动而非 episode 数，未深入验证。
8. **多相机缺省行为**：`empty_cameras` 补空图逻辑只读源码，未跑训练验证效果。

---

## 11. 交付物

- 本报告：`docs/smolvla-training-params.md`
- JSON 模板：`docs/smolvla-training-params.json`