# VLA 训练数据集生态与格式规范调研

> 面向「平台自动识别 / 校验 / 展示 LeRobot 数据集」的工程说明书
> 调研时间：2026-11-09 ｜ 依据：本地 `lerobot 0.6.2` 源码（`/home/robam/lerobot/src/lerobot/datasets/`）、GitHub 上 `huggingface/lerobot` 的 `main` / `v0.3.3` 标签、arXiv 原文，以及服务器上真实数据集 `/home/jjx/1028pick_3.0` 的实测结果。
>
> **环境说明（重要）**：本机 `huggingface.co` 不可达（超时），`web_search` 无 API Key。所有「官方文档」结论均改为从 **GitHub 仓库内的文档源码**（`docs/source/*.mdx`，即 huggingface.co/docs/lerobot 的同一份源文件）与 **源码常量** 获取，可信度不低于网页版。标注 `【实测】` 的条目是在真实数据集上跑出来的。

---

## 0. 一页速览（给扫描器开发者）

1. **识别**：读 `<root>/meta/info.json` 的 `codebase_version`，只有三个有效值：`v2.0` / `v2.1` / `v3.0`。
2. **v2.x 是"一 episode 一文件"**，`meta/episodes.jsonl` + `meta/episodes_stats.jsonl` + `meta/tasks.jsonl`；**v3.0 是"一文件多 episode"**，episode 元数据变成 `meta/episodes/chunk-*/file-*.parquet`。
3. **v3.0 的路径不是固定的**，必须在 `info.json` 的 `data_path` / `video_path` 模板上 `.format(...)`，且模板里的 `chunk_index` / `file_index` **不来自文件名规律，而来自 episodes parquet 的列**。
4. **几十毫秒拿到全量统计**：`info.json`（episodes / frames / fps / 动作维度 / 相机分辨率）+ `meta/stats.json`（归一化用 + 特征级 count 交叉校验）。不需要碰 parquet。
5. **最大的坑**：`info.json` 的 `total_frames` 与 `total_episodes` 经常和实际文件对不上（保存中断、`finalize()` 没调、手工改过），**展示时必须以 episodes parquet 的 `length` 求和为准，并用它给 `info.json` 打 "claimed vs actual" 标签**。

---

# A. 调研报告

## 1. LeRobot 数据集格式

### 1.1 版本总览

| 维度 | v2.0 | v2.1 | v3.0 |
|---|---|---|---|
| 数据文件粒度 | 1 episode = 1 parquet | 同 v2.0 | **多 episode 合并进 1 个 parquet** |
| 视频文件粒度 | 1 episode = 1 mp4 | 同 v2.0 | **多 episode 合并进 1 个 mp4**（按 episode 顺序拼接，用 `from_timestamp`/`to_timestamp` 定位） |
| data 路径 | `data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet` | 同 v2.0 | `data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet`（可被 `info.json` 覆盖） |
| video 路径 | `videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4` | 同 v2.0 | `videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4`（**注意 camera 和 chunk 的先后顺序反了**） |
| episode 元数据 | `meta/episodes.jsonl`（每行一个 episode） | 同 v2.0 | **`meta/episodes/chunk-000/file-000.parquet`**（多列，含 `dataset_from_index` 偏移） |
| 任务表 | `meta/tasks.jsonl` | 同 v2.0 | **`meta/tasks.parquet`** |
| 统计文件 | `meta/stats.json`（全量） | `meta/stats.json` **被删除**；改为 `meta/episodes_stats.jsonl`（逐 episode） | `meta/stats.json` **回来了**（全量，由各 episode 聚合）+ 每 episode 统计**内联进 episodes parquet 的 `stats/<key>/<stat>` 列** |
| `info.json` 额外字段 | `total_videos`、`total_chunks` | 同 v2.0 | **`data_files_size_in_mb`、`video_files_size_in_mb`**；`total_videos`/`total_chunks` 被移除（读取时忽略）；新增可选 `storage_format`、`tools` |
| 官方转换脚本 | — | `python -m lerobot.datasets.v21.convert_dataset_v20_to_v21` | `python -m lerobot.scripts.convert_dataset_v21_to_v30` |
| 官方兼容性 | 0.6.x **直接报错**拒绝加载 v2.x | 同左 | 当前 `CODEBASE_VERSION` |

出处：
- v3.0 布局与设计：<https://github.com/huggingface/lerobot/blob/main/docs/source/lerobot-dataset-v3.mdx>（= <https://huggingface.co/docs/lerobot/lerobot-dataset-v3>）
- v2.1 vs v3.0 结构对比：<https://github.com/huggingface/lerobot/blob/main/docs/source/porting_datasets_v3.mdx>
- v2.1 路径常量（权威）：<https://github.com/huggingface/lerobot/blob/v0.3.3/src/lerobot/datasets/utils.py>（`DEFAULT_VIDEO_PATH` / `DEFAULT_PARQUET_PATH` / `EPISODES_PATH` …）
- v2.0→v2.1 语义（删 stats.json、加 episodes_stats.jsonl）：<https://github.com/huggingface/lerobot/blob/v0.3.3/src/lerobot/datasets/v21/convert_dataset_v20_to_v21.py>
- v2.1→v3.0 语义（文件聚合、列改名）：<https://github.com/huggingface/lerobot/blob/main/src/lerobot/scripts/convert_dataset_v21_to_v30.py>
- 当前版本常量：`CODEBASE_VERSION = "v3.0"`，见 `src/lerobot/datasets/dataset_metadata.py:61`

> ⚠️ **v2.0 → v2.1 的实质差异只有一个**：统计文件的组织方式（全局 → 逐 episode）。目录树、路径模板、parquet 列**完全一样**。所以扫描器里 `v2.0` 和 `v2.1` 应该走**同一条代码路径**，只在 stats 读取上分支。

### 1.2 目录树

**v2.0 / v2.1**

```
<root>/
├── meta/
│   ├── info.json                  # 必须
│   ├── stats.json                 # v2.0 有；v2.1 删除了
│   ├── episodes.jsonl             # 每行: {"episode_index", "tasks": [...], "length"}
│   ├── episodes_stats.jsonl       # v2.1 才有；每行: {"episode_index", "stats": {key: {min,max,mean,std,count}}}
│   └── tasks.jsonl                # 每行: {"task_index", "task"}
├── data/
│   └── chunk-000/
│       ├── episode_000000.parquet
│       └── episode_000001.parquet
├── videos/
│   └── chunk-000/
│       └── observation.images.<cam>/
│           ├── episode_000000.mp4
│           └── episode_000001.mp4
└── images/                        # 仅当相机 dtype == "image"（未编码为视频）时存在
    └── observation.images.<cam>/episode_000000/frame_000000.png
```

**v3.0**（以真实数据集 `/home/jjx/1028pick_3.0` 为模板，【实测】）

```
<root>/
├── meta/
│   ├── info.json                          # 3.5 KB
│   ├── stats.json                         # 6.5 KB，全量聚合统计
│   ├── tasks.parquet                      # 列: task_index(int64) + 名为 __index_level_0__ 的 task 文本列
│   └── episodes/
│       └── chunk-000/
│           └── file-000.parquet           # 143 行（= 143 episodes），66 KB
├── data/
│   └── chunk-000/
│       └── file-000.parquet               # 60 470 行（= 全部 frames），4.1 MB，1 个 row group
└── videos/
    ├── observation.images.camera1/
    │   └── chunk-000/file-000.mp4 …
    ├── observation.images.camera2/
    │   └── chunk-000/file-000.mp4 …
    └── observation.images.camera3/
        └── chunk-000/file-000.mp4 …
```

> 【实测】该数据集根目录下还有一个 `meta.bak/`（完整的旧 meta 副本：`info.json` / `stats.json` / `tasks.parquet` / `episodes/chunk-000/`）。**扫描器必须忽略非白名单目录**，否则会把 `chunk-000` 数两遍。

### 1.3 `meta/info.json`：完整字段规格（v3.0）

真实样例（已确认，节选）【实测】：

```json
{
  "codebase_version": "v3.0",
  "robot_type": "dual_arm_mobile",
  "total_episodes": 143,
  "total_frames": 60470,
  "total_tasks": 1,
  "chunks_size": 1000,
  "fps": 30,
  "splits": { "train": "0:143" },
  "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
  "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
  "features": {
    "observation.state": {
      "dtype": "float32", "shape": [14], "fps": 30.0,
      "names": ["arm1_joint_1.pos", "...", "arm2_gripper.pos"]
    },
    "action": { "dtype": "float32", "shape": [14], "fps": 30.0, "names": ["arm1_joint_1.cmd", "..."] },
    "observation.images.camera1": {
      "dtype": "video",
      "shape": [480, 640, 3],
      "names": ["height", "width", "channels"],
      "info": {
        "video.height": 480, "video.width": 640,
        "video.codec": "h264", "video.pix_fmt": "yuv420p",
        "video.is_depth_map": false, "video.fps": 30,
        "video.channels": 3, "has_audio": false
      }
    },
    "timestamp":    { "dtype": "float32", "shape": [1], "names": null, "fps": 30.0 },
    "frame_index":  { "dtype": "int64",   "shape": [1], "names": null, "fps": 30.0 },
    "episode_index":{ "dtype": "int64",   "shape": [1], "names": null, "fps": 30.0 },
    "index":        { "dtype": "int64",   "shape": [1], "names": null, "fps": 30.0 },
    "task_index":   { "dtype": "int64",   "shape": [1], "names": null, "fps": 30.0 }
  }
}
```

**字段级说明**

| 字段 | 类型 | 必填 | 说明 / 陷阱 |
|---|---|---|---|
| `codebase_version` | str | ✅ | `"v2.0"` / `"v2.1"` / `"v3.0"`。0.6.x 遇到 major=2 **直接抛 `BackwardCompatibilityError`** |
| `robot_type` | str \| null | ⭕ | 自由文本，常为 `so101` / `aloha` / `panda` / 自定义 |
| `total_episodes` | int | ✅ | **声明值**，可能不可信（见 1.6） |
| `total_frames` | int | ✅ | **声明值**，同上。⚠️ 真实数据里 `60470` 与 parquet 实际行数一致，但也有大量数据集不一致 |
| `total_tasks` | int | ✅ | 字典里"唯一任务"数，一般等于 tasks 表行数 |
| `chunks_size` | int | ✅ | **一个 chunk 目录最多放多少个 file**（v3.0 是 "files per chunk"，v2.x 是 "episodes per chunk"），默认 1000 |
| `data_files_size_in_mb` | int | v3 | 单个 data parquet 的软上限，默认 100 |
| `video_files_size_in_mb` | int | v3 | 单个 mp4 的软上限，默认 200 |
| `fps` | int | ✅ | 全局采集帧率。**没有逐 feature 的 fps 是强制的**（`timestamp`/`frame_index` 上带的 `fps` 字段是 v3 新增的可冗余信息） |
| `splits` | dict | ⭕ | 通常 `{"train": "0:<total_episodes>"}`，格式 `"<start>:<end>"`（end 不含） |
| `data_path` | str | ✅ | **模板字符串**，格式参数：`{chunk_index}`、`{file_index}`（v2.x 是 `{episode_chunk}`、`{episode_index}`） |
| `video_path` | str \| null | ⭕ | `null` = 本数据集没有视频；`str` 时格式参数：`{video_key}`、`{chunk_index}`、`{file_index}` |
| `features` | dict | ✅ | 见下 |
| `storage_format` | str \| null | v0.6 新增 | `null`/缺省 = 原生 parquet+mp4；`"lance"` = LanceDB 后端，此时 `data/`、`videos/` 目录**不存在** |

**`features` 的结构**

```
features[<key>] = {
    "dtype":  "float32" | "int64" | "video" | "image" | "string" | ...,
    "shape":  [int, ...],
    "names":  [str, ...] | null,      # 对向量特征是可读维度名；对图像是 ["height","width","channels"]
    "fps":    float,                  # 仅部分数据集有（v3 起）
    "info":   {...}                   # 仅 dtype=="video" 有：video.height/width/codec/pix_fmt/fps/channels/is_depth_map/has_audio
}
```

- 图像类：`dtype` 只能是 `"video"`（mp4 存储）或 `"image"`（逐帧 png 存储）。两者都算相机（`camera_keys =` 所有 `dtype in {"video","image"}`）。
- 图像 `shape` 必须是 `[H, W, C]`；**分辨率以 `shape` 为准**（`info["video.height"]` 是编码结果，二者不一致通常说明"第一集探测出来的视频信息"已过期，属于可疑项）。
- 深度图：`feature["info"]["is_depth_map"] == true`（旧式 `video.is_depth_map` 已被规范化，扫描器应两者都认）。

来源：`src/lerobot/datasets/utils.py`（`DatasetInfo` dataclass）、`src/lerobot/datasets/video_utils.py:1174 get_video_info()`、`src/lerobot/utils/constants.py:90 DEFAULT_FEATURES`。

### 1.4 parquet 列规格（v3.0）

**`data/chunk-XXX/file-XXX.parquet`** —— 每行 = 一帧【实测 schema】

```text
observation.state : list<element: float>   # 长度 = features["observation.state"]["shape"][0]，实测 14
action            : list<element: float>   # 实测 14
episode_index     : int64                  # 该帧属于哪个 episode
frame_index       : int64                  # 在 episode 内的帧号，从 0 开始
timestamp         : float                  # 秒，episode 内从 0.0 开始，步长 1/fps
index             : int64                  # 全局帧号 0..total_frames-1
task_index        : int64                  # 指向 meta/tasks.parquet 的行号（不是 task 文本）
```

注意：
- **帧内任务文本不在 data parquet 里**。`LeRobotDataset.__getitem__` 会做 `tasks.iloc[task_index].name` 来还原文本（`src/lerobot/datasets/dataset_reader.py:459-461`）。**`task_index` 是"按行定位"**，所以 `tasks.parquet` 的 `task_index` 列必须是 `0..N-1` 连续且与行序一致，否则任务文本会错位——这是很容易漏的检查项。
- 相机帧**不在 parquet 里**（v3.0 强制视频化）。只有 `dtype=="image"` 时才有 `images/` 目录。
- 标量列（`timestamp`/`index`/`episode_index`/`frame_index`/`task_index`）在 arrow 里是 `float`/`int64` 标量，但**在 `features` 里 shape 声明为 `[1]`**——这是设计如此，不是错误。

> ⚠️ **arrow 类型差异（实测，影响写校验代码）**：同一个 `observation.state`，
> - **v2.1** 真实数据集里是 `fixed_size_list<element: float>[14]`（定长列表）；
> - **v3.0** 真实数据集里是 `list<element: float>`（变长列表）。
>
> 所以扫描器**不能用 arrow 类型的相等性做 schema 比对**，必须"读出来再判断每行长度 == `features[*].shape[0]`"。也正因为 v3.0 是变长 list，**个别行维度写错是可能的**（检查项 7 的意义所在）。

**`meta/episodes/chunk-XXX/file-XXX.parquet`** —— 每行 = 一个 episode【实测 143 行，33 列】

```text
episode_index               : int64
data/chunk_index            : int64
data/file_index             : int64
dataset_from_index          : int64        # 该 episode 在全局帧序号上的起点（含）
dataset_to_index            : int64        # 全局帧序号终点（不含）
tasks                       : list<string> # 该 episode 的语言指令，可能多条
length                      : int64        # 帧数
meta/episodes/chunk_index   : int64        # 自引用：本行所在的 meta 分片位置
meta/episodes/file_index    : int64
videos/<video_key>/chunk_index    : int64  # 每个相机各一组
videos/<video_key>/file_index     : int64
videos/<video_key>/from_timestamp : double # 在该 mp4 内的起始秒
videos/<video_key>/to_timestamp   : double # 在该 mp4 内的结束秒
stats/<feature>/min  : list<list<...>>     # 逐 episode 统计（形状随特征，图像为 HxWxC）
stats/<feature>/max  : ...
stats/<feature>/mean : ...
stats/<feature>/std  : ...
stats/<feature>/count: list<int64>
```

> **关键设计**：`videos/<key>/chunk_index` 和 `file_index` 是**逐 episode、逐相机**独立的。因为 v3 允许"视频按 `video_files_size_in_mb` 单独切分"，所以**同一 episode 的 camera1 可能在 `file-001.mp4`，camera3 可能还在 `file-000.mp4`**。扫描器严禁用 `data` 的 `file_index` 去推视频路径。

**`meta/tasks.parquet`**【实测】

```text
task_index              : int64     # 0..N-1
__index_level_0__       : string    # task 文本（pandas index 被写成了列）
```

> ⚠️ 列名是 `__index_level_0__` 而不是 `task`！pandas 读取时它会被还原成 **index**（`df.index.name == "task"`）。用 pyarrow 直接读会看到这个别扭的列名。扫描器取值请用"**除 `task_index` 外的第一个 string 列**"，而不是硬编码 `task`。

### 1.4b v2.0 / v2.1 的 parquet 与 JSONL（已在真实数据上验证）

【实测】`output/lerobot_v21_sample_h264/…v2`（`codebase_version: "v2.1"`，`robot_type: "aloha_agilex"`）：

```
data/chunk-000/episode_000000.parquet     # 139 行
  action            : fixed_size_list<element: float>[14]
  observation.state : fixed_size_list<element: float>[14]
  timestamp : float   frame_index : int64   episode_index : int64
  index     : int64   task_index  : int64

meta/episodes.jsonl   →  {"episode_index": 0, "tasks": ["['Pick the bottle with ridges near base head-up using the left arm']"], "length": 139}
meta/tasks.jsonl      →  {"task_index": 0, "task": "Pick the bottle with ridges near base head-up using the left arm"}
meta/info.json        →  total_episodes=1, total_frames=139, total_tasks=47, total_videos=3, total_chunks=1, fps=15,
                          data_path="data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
                          video_path="videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4"
videos/chunk-000/observation.images.cam_high/episode_000000.mp4
videos/chunk-000/observation.images.cam_left_wrist/episode_000000.mp4
videos/chunk-000/observation.images.cam_right_wrist/episode_000000.mp4
```

**这暴露了两个 v2.x 的真实"脏数据"模式，扫描器必须专门处理：**

1. **任务文本被 repr 成了字符串**：`tasks` 里存的是 `"['Pick the bottle …']"`——一个**字符串化的 Python list**，而不是 `["Pick the bottle …"]`。原因是早期脚本用了 `str(list)` 而不是 `list`。直接 `" ".join(tasks)` 会把方括号和引号喂给语言模型。
   → 检测：`tasks` 元素以 `"["` 开头且以 `"]"` 结尾且 `ast.literal_eval` 能成功；给出 `warning` + 自动修复建议。
2. **`total_tasks` 与实际任务数严重不符**：声明 47，但只有 1 个 episode、只有 1 条被引用。47 大概率是"上游池子里的候选指令数"而不是"本数据集实际使用的指令数"。
   → 检测：`total_tasks` 与 `tasks.jsonl` 行数、与 `data.task_index` 的去重数**三方对比**，不一致就给 `info`/`warning`，展时以 `task_index` 去重数为准。

另外该数据集带平台侧附加文件 `labelvla_revision.json`（记录 episode 的裁剪映射 `source/start/end/target`），`1028pick_3.0` 带 `_platform/robot.json`（`{"robot_tag": "自变量"}`）与 `segments/`（标注）。**这些不是 LeRobot 规范的一部分**，是平台私有 sidecar——扫描器应把它们当作"可选扩展"识别，但**不能因为存在就判为非法**（检查项 1 与 19 的白名单要放行 `_platform/`、`segments/`、`labelvla_revision.json`）。

---

## 1.5 路径解析算法（v3.0，务必按这个写）

```
for each episode row ep in meta/episodes/*.parquet:
    data_file  = info.data_path.format(chunk_index=ep["data/chunk_index"],
                                       file_index =ep["data/file_index"])
    for cam in video_keys:
        video_file = info.video_path.format(video_key  = cam,
                                           chunk_index=ep[f"videos/{cam}/chunk_index"],
                                           file_index =ep[f"videos/{cam}/file_index"])
```

- Python `str.format` 语法，支持 `{chunk_index:03d}` 这类格式说明符。**直接 `Path(root) / template.format(...)` 即可**，不要自己猜目录名。
- `chunks_size` **只决定目录能放多少文件**，不参与路径解析（路径全由 episodes 列给出）。它的用途是：写数据时切换目录、以及扫描器**校验 chunk 目录数量是否合理**。
- 全局帧行号 ↔ episode 映射：`ep["dataset_from_index"] <= index < ep["dataset_to_index"]`。注意 `dataset_to_index` 是**不含**的，且应满足 `dataset_to_index - dataset_from_index == length`——这是 v3 最容易出错的强约束（见 B 节第 13 项）。

【实测】`1028pick_3.0`：ep0 `dataset_from_index=0, dataset_to_index=493, length=493, camera1 from=0.0, to=16.4333`；ep1 `493→994, length=501, from=0.0, to=16.7`。**每个新 mp4 里 `from_timestamp` 都从 0 重新开始**（文件聚合后，只有跨文件时才有非零起点）。

### 1.6 `meta/stats.json` 的作用

- **用途**：训练时归一化。`LeRobotDataset` 把它暴露为 `dataset.meta.stats`，`NormalizationMode.MEAN_STD` / `QUANTILES` / `QUANTILE10` / `MIN_MAX` 都从这里取统计量。pi0/π0.5/SmolVLA 的 processor 都依赖它。
- **标准 5 项**：`min` / `max` / `mean` / `std` / `count`。
- **可选 5 项（分位数）**：`q01` / `q10` / `q50` / `q90` / `q99`，**默认不存在**，需显式跑 `augment_dataset_quantile_stats.py` 生成。`NormalizationMode.QUANTILES`（π0.5 的默认）读 `q01`/`q99`，`QUANTILE10` 读 `q10`/`q90`；**缺了直接抛 `ValueError`**（`src/lerobot/processor/normalize_processor.py:386-392`）。
- **结构**【实测 `1028pick_3.0/meta/stats.json`】：顶层 key 是 feature key（`observation.state`、`action`、`timestamp`、`episode_index`、`frame_index`、`index`、`task_index`、三个相机），每项含 `min` / `max` / `mean` / `std` / `count`（**实测无 `q*` 键**）。
  - 向量特征的 `min/max/mean/std` 是长度 = 维度 的 list；`count` 是 `[总帧数]`（**实测 `[60470]` = total_frames**）。
  - 图像特征的统计是 `H×W×C` 的三层嵌套 list（**逐像素**，不是只给 3 个通道均值）——所以图像统计对"通道均值"这种展示要自己展平求均值。
- **`count` 是最好的免费对账工具**：`stats["observation.state"]["count"][0] == total_frames` 应当成立；不成立说明 stats.json 与数据不同步（常见于增量写入后没重算）。
- **跨特征 `count` 不一致 = 有特征被漏统计**（比如后来加的相机）。同时**只有部分 feature 缺 stats 条目** 也是可报的警告。
- v2.1 没有它，改用 `episodes_stats.jsonl` 聚合（`lerobot.datasets.compute_stats.aggregate_stats`）；v3.0 又回到全局 `stats.json`，同时把逐 episode 统计内联进 episodes parquet。
- ⚠️ **`stats.json` 是唯一包含"真实数值分布"的地方**，如果它缺失或与 `count` 对不上，归一化会直接用错，是**高优先级**检查项。

### 1.7 快速统计一个数据集：读哪些文件的哪一部分

| 要统计的东西 | 读什么 | 抽取方式 | 代价 |
|---|---|---|---|
| episode 数 | `meta/info.json` → `total_episodes` | 直接用 | <1 ms |
| frame 数 | `meta/info.json` → `total_frames` | 直接用（**声明值**） | <1 ms |
| 任务数 | `meta/info.json` → `total_tasks` | 直接用（声明值） | <1 ms |
| fps | `meta/info.json` → `fps` | 直接用 | <1 ms |
| 各相机分辨率 | `meta/info.json` → `features[cam].shape` → `[H, W, C]` | 直接取 | <1 ms |
| 相机数量 / 相机名 | `features` 里 `dtype in {"video","image"}` 的 key | 过滤 | <1 ms |
| 动作维度 | `features["action"].shape[0]` | 直接取 | <1 ms |
| 状态维度 | `features["observation.state"].shape[0]` | 直接取 | <1 ms |
| 各关节名 | `features["action"].names` | 直接取（可能为 null） | <1 ms |
| 视频编码参数 | `features[cam].info["video.codec"/"pix_fmt"]` | 直接取 | <1 ms |
| **真实 episode/frame 数** | `meta/episodes/**/*.parquet` 的 `length` 列 | `sum(length)` | ~几十 ms / 1 万 episodes |
| 每 episode 时长 | 同上 | `length / fps` | 同上 |
| **归一化统计** | `meta/stats.json` | 直接取 | <1 ms |
| 任务文本列表 | `meta/tasks.parquet` | 读第 1 个 string 列 | ~ms |
| 文件大小 / 存储占用 | `data/`、`videos/` 目录 `stat()` | `du -sh` 等价 | 与文件数成正比 |
| 视频真实帧数 / 时长 | 不能用 info.json，需要 ffprobe（`nb_read_frames`） | 深度检查 | 每个 mp4 约 10–100 ms |

**推荐的两级统计策略**

```
L1 (info.json, <1ms)      → 先出展示卡片（可能不准，标 "声明值"）
L2 (episodes.parquet, 10-100ms) → 出 "实测值"，与 L1 对比，差异即打标
L3 (data/videos 全扫)     → 异步深度检查，用于"健康度评分"
```

> 【实测】`1028pick_3.0`：`info.total_episodes=143`，`episodes.parquet` 143 行，`sum(length)=60470 == info.total_frames` ✅；`stats.count=[60470]` ✅；`ffprobe file-000.mp4` → `nb_read_frames=493, r_frame_rate=30/1, duration=16.433333`，与 ep0 的 `from/to_timestamp` **完全吻合** ✅。这说明该数据集是规范产出的，可以当作扫描器的"正样本"。

### 1.8 版本兼容与常见"伪数据集"

- 0.6.x 的 `check_version_compatibility()`：`v_check.major < v_current.major` → 抛 `BackwardCompatibilityError`（v2.1 会给转换命令）；`v_check.minor < v_current.minor` → 仅 warning。所以 **v3.0 代码无法直接加载 v2.x**，平台若要同时支持，必须自己写 v2.x 读取器或调用 `convert_dataset_v21_to_v30`。
- 转换脚本会**删除** `data/chunk*/episode_*`、`meta/*.jsonl`、`videos/chunk*`（`delete_patterns`）。如果你在别人的 `meta.bak` 里看到旧布局，属于残留，不要当成主数据。
- `storage_format: "lance"` 的数据集没有 `data/`、`videos/`，扫描器要**单独短路**，提示"LanceDB 后端，需 lancedb 依赖读取"。

---

## 2. 主流 VLA 数据集（生态速览）

| 数据集 | 规模 | 特点 | 出处 |
|---|---|---|---|
| **Open X-Embodiment (OXE)** | 22 台机器人、21 家机构、527 技能 / 160266 任务 | 事实上的"多本体预训练标准"，把各家数据统一成 **RLDS / TFDS** 格式；LeRobot 侧有 `lerobot/oxe` 中转。是 RT-X 的训练数据。 | [arXiv:2310.08864](https://arxiv.org/abs/2310.08864) |
| **RT-1 数据** | 13 台 Everyday Robots，约 13 万条 episode / 17 个月采集 | 原始格式为 **tfrecord**，含 6 路相机 + 自然语言指令；是"开源模型不开数据"的典型（只开放了 RT-1-X 权重与 OXE 中的子集）。 | [arXiv:2212.06817](https://arxiv.org/abs/2212.06817) |
| **RT-2 数据** | 未公开规模（WebLI + 机器人轨迹联合微调） | 核心贡献是把**动作离散化成文本 token** 进行 VLM 联合训练，数据范式影响深远，但数据未开源；RT-2-X 使用的是 OXE。 | [arXiv:2307.15818](https://arxiv.org/abs/2307.15818) |
| **DROID** | **76k** 轨迹 / **350 小时** / 564 场景 / 84 任务 / 50 名采集者 / 3 大洲 | 目前最"in-the-wild"的真实操作数据集；原始 RLDS 格式（1.7 TB），LeRobot 有官方移植流程与 v3.0 版本，是验证 v3 大文件格式的标杆。 | [arXiv:2403.12945](https://arxiv.org/abs/2403.12945)、[porting 文档](https://github.com/huggingface/lerobot/blob/main/docs/source/porting_datasets_v3.mdx) |
| **LIBERO** | 4 个任务套件 / **130 任务**，仿真 | 面向**终身学习**的仿真基准，任务套件划分清晰（Spatial/Object/Goal/Long），是 π0/SmolVLA/GR00T 微调的事实默认 benchmark；LeRobot 有 `libero` 中转（`robot_state/...`、`pixels/...` 键名）。 | [arXiv:2306.03310](https://arxiv.org/abs/2306.03310) |
| **CALVIN** | 仿真，长程语言条件操作 | "ABC→D" 跨环境泛化评测标准，任务链最长、动作空间与语言复杂度都高于同期基准；数据结构为**逐帧 npz + 语言标注**，与 LeRobot 差异较大，需转换。 | [arXiv:2112.03227](https://arxiv.org/abs/2112.03227) |
| **RoboCasa** | 仿真，**100 个**厨房任务、150+ 物体类别、数千 3D 资产 | 用生成式 AI 扩增场景资产的仿真"厨房宇宙"，提供高质量人类示范 + 自动轨迹生成；常作为 GR00T/π0.5 的泛化评测。 | [arXiv:2406.02523](https://arxiv.org/abs/2406.02523) |
| **RoboMimic** | 仿真，5 个任务族（Lift/Can/Square/Transport/ToolHang） | 早期模仿学习标准库，数据量小、结构简单（hdf5），主要做算法基线而非预训练。 | robomimic 官方仓库 |
| **AgiBot World**（中文） | **100 万+** 轨迹 / **217** 任务 / 5 类部署场景 | 智元/上海 AI Lab 的规模化真实数据平台，从夹爪扩展到灵巧手与视触觉；强调"标准化采集流水线 + human-in-the-loop 校验"。配套 GO-1 策略，论文报告预训练收益比 OXE 高约 30%。数据已开放，在 HuggingFace 上以 LeRobot 格式发布（子集命名需在 HF 侧确认）。 | [arXiv:2503.06669](https://arxiv.org/abs/2503.06669) |
| **RoboMIND**（中文） | v1：**107k** 轨迹 / **479** 任务 / 96 类物体 / 4 种本体（Panda、UR5e、AgileX 双臂、双臂灵巧手人形）+ 5k 失败示范。<br>v2.0：**310k+** 双臂轨迹 / 6 本体 / 739 任务 / 12k 触觉 + 20k 移动操作 + 20k 仿真 | 北京人形机器人创新中心等；特点是把**失败示范及其原因**也纳入数据集，并提供 Isaac Sim 数字孪生。对质检很有参考价值——"失败帧"不是坏数据。 | [arXiv:2412.13877](https://arxiv.org/abs/2412.13877)、[arXiv:2512.24653](https://arxiv.org/abs/2512.24653) |

**生态小结（对平台的含义）**

1. **两套"事实格式"并存**：学术侧 RLDS/TFDS，工程侧 **LeRobot v3.0**。平台应把 LeRobot 作为一等公民，对 RLDS 只做"识别 + 引导转换"。
2. **跨本体是常态**：OXE/AgriBot/RoboMIND 都是多机器人混合，任何平台如果假设"一个数据集 = 一种动作维度"，都会在真实数据上崩。
3. **仿真数据占比很大**：LIBERO/CALVIN/RoboCasa 出来的 LeRobot 数据集，其"state" 往往是末端位姿而不是关节角，相机也只有 1–2 路，这正是第 3 节"适配"问题的来源。
4. **中文数据集也基本都提供了 LeRobot 导出**，平台可以统一按 v2.1/v3.0 双分支解析。

---

## 3. 跨模型的数据适配

### 3.1 各模型对数据的要求（以 LeRobot 0.6.2 实现为准）

| 模型 | 状态/动作维度 | 动作 chunk | 视觉分辨率 | 归一化 | 相机数 | 关键约束 |
|---|---|---|---|---|---|---|
| **π0 / π0.5** | `max_state_dim=32`、`max_action_dim=32`，**不足补零、超出不截断（`pad_vector(truncate=False)`）** | `chunk_size=50`、`n_action_steps=50` | `DEFAULT_IMAGE_SIZE=224`（等比缩放+padding） | π0: `MEAN_STD`；**π0.5: `QUANTILES`**（需要分位数统计） | ≥1，可 `empty_cameras=N` 补空相机 | 三路相机在预训练里是"base + 2 wrist"的**固定槽位语义**，换相机会削弱预训练先验 |
| **SmolVLA** | `max_state_dim=32` / `max_action_dim=32` | `chunk_size=50` | **`resize_imgs_with_padding=(512,512)`** | `STATE=MEAN_STD`、`ACTION=MEAN_STD`、`VISUAL=IDENTITY` | ≥1，`empty_cameras` 可补 | 预训练在**社区小数据集**上，对单臂/少相机最友好；`adapt_to_pi_aloha` 可做 ALOHA 坐标变换 |
| **GR00T N1.7** | 依赖 **embodiment tag** 的模态配置 | 原生 **40 步** action horizon（processor 侧上限） | `(256,256)` 目标尺寸 / `(230,230)` crop | 由 checkpoint 的 `processor_config` 决定（含 state/action 的模态映射） | 依赖 tag 定义 | **强依赖 `embodiment_tag`**：新本体要注册新 tag 与模态键映射；支持 `use_relative_action`；N1.5 支持已被移除 |
| **OpenVLA** | 7B，Llama2-7B + DINOv2/SigLIP；动作**离散化成 256 bin token** | 单步动作（无 action chunk） | 224（预训练固定） | bin 边界来自预训练统计 | 固定 1 路第三人称 | 换动作维度需**重新定义 bin 边界并调整输出头**，不能直接复用 |

出处：
- π0/π0.5 配置：`/home/robam/lerobot/src/lerobot/policies/pi05/configuration_pi05.py`（`normalization_mapping`、`DEFAULT_IMAGE_SIZE=224`、`max_state_dim/max_action_dim=32`）
- SmolVLA 配置：`/home/robam/lerobot/src/lerobot/policies/smolvla/configuration_smolvla.py`（`resize_imgs_with_padding=(512,512)`、`empty_cameras`）
- GR00T：`/home/robam/lerobot/src/lerobot/policies/groot/configuration_groot.py`（`N1_7_DEFAULT_IMAGE_TARGET_SIZE=(256,256)`、`N1_7_DEFAULT_IMAGE_CROP_SIZE=(230,230)`、40 步 horizon 见 `processor_groot.py:103`）
- `pad_vector`：`/home/robam/lerobot/src/lerobot/policies/common/vla_utils.py:116`
- OpenVLA：[arXiv:2406.09246](https://arxiv.org/abs/2406.09246)
- GR00T N1：[arXiv:2503.14734](https://arxiv.org/abs/2503.14734)

### 3.2 同一个 LeRobot 数据集能直接喂给不同模型吗？

**能，但"能加载"≠"训得好"。** 在 LeRobot 体系内，只要 `LeRobotDataset` 能读，`lerobot-train` 就能跑；差异全在 **policy 的 preprocessor**：

1. **能直接跑的部分**：读取、按 `delta_timestamps` 取 action chunk、视频解码、按 `meta/stats.json` 做归一化。这些是数据集层统一提供的。
2. **需要适配的部分**（按出错概率排序）：

| 适配项 | 现象 | 做法 |
|---|---|---|
| **动作维度** | 14 维动作喂给 `max_action_dim=32` 的 π0 → 自动补 18 个 0，**能训但精度受损**；喂给 >32 维 → `pad_vector` 不截断，**直接维度报错** | 32 以内补零可接受；超 32 必须改 `max_action_dim` 或用 GR00T 的 embidiment 配置 |
| **状态/动作定义不一致** | 数据集是"关节角 + 夹爪开度"，模型预训练是"末端位姿 + 夹爪二值" | 只能选择：① finetune 时接受分布漂移；② 数据侧重算 state/action（需要 URDF + FK/IK）；③ 用支持相对动作的模型（GR00T `use_relative_action`） |
| **归一化** | **π0.5 直接报错**：`QUANTILES` 需要 `stats[key]["q01"]` 和 `["q99"]`，缺失时抛 `ValueError: QUANTILES normalization mode requires q01 and q99 stats` | **不需要近似——必须补齐**：`python -m lerobot.scripts.augment_dataset_quantile_stats --repo-id=...`。默认分位数为 `[0.01, 0.10, 0.50, 0.90, 0.99]` → 写成 `q01/q10/q50/q90/q99` 键。**实测 `1028pick_3.0/stats.json` 只有 `count/min/max/mean/std`，即该数据集当前无法直接微调 π0.5** |
| **相机数量/槽位** | 3 路相机喂给"固定 2 路"的 checkpoint | π0/π0.5/SmolVLA 支持 `empty_cameras`；但**相机→槽位映射**（哪路是 wrist）需要人工指定，且必须在 preprocessor 里固定 key 顺序 |
| **分辨率** | 1920×1080 的原始数据 | 全部模型都在 preprocessor 里缩放，**不需要预处理视频**；但 h264 压缩在 1080p→224 时高频细节损失，建议采集侧直接 480p/640p |
| **fps** | 15 fps 数据 + 假设 30 fps 的 `delta_timestamps` | `delta_timestamps` 必须按 `meta.fps` 计算（`check_delta_timestamps` 会因非整数倍直接抛错） |

**结论（给平台的产品建议）**：平台应把"数据集 ↔ 模型"的兼容性做成**一张前置校验表**，在训练任务下发前跑一次，输出
`{可训练: bool, 阻塞项: [...], 警告项: [...], 建议的 policy.override 参数: {...}}`。

### 3.3 三类"不一致"的具体处置

**(a) 图像分辨率不一致（跨数据集混合训练）**
- 单数据集内，`info.json` 只给一个分辨率；但**真实数据里不同本体/阶段可能混有不同分辨率**（例如早期 720p、后期 480p）。LeRobot **不校验逐文件分辨率**，`info.json` 里的 `video.height/width` 是从"第一个视频"探测的 → 应当信任 `shape` 但**抽查若干 mp4**。
- 处置：① 训练侧统一交给 preprocessor 缩放（主流做法，零成本）；② 数据侧统一转码成同一分辨率+同 codec（对 object-store 训练更省带宽）。**平台不要私自转码**，转码会改变 `nb_frames` 语义正确性，但需同步更新 `info.json`。

**(b) 相机数量不一致**
- 不同 episode 相机不同：**LeRobot 格式不支持**（features 是全局 schema），只能拆成多个数据集。
- 不同数据集相机数量不同：**列级拼接会导致大量 NaN**。推荐做法：选"公共相机子集"训练，或用 `empty_cameras` 补空并在 preprocessor 里屏蔽对应位置。
- 平台展示时一定要把**相机名列表**作为一等字段（`observation.images.camera1` 这种命名毫无语义，用户无法判断哪路是 wrist）。

**(c) 动作维度不一致**
- 混合训练时：按模型上限 `max_action_dim` 补零，并在**数据集卡片里写明填充语义**（"第 15–32 维恒为 0，请勿作为有效动作"）。
- 单个数据集内维度不一致（同一 parquet 里 list 长度不同）：这是**数据损坏**，直接报错（见 B 节第 8 项）。

---

## 4. 数据集质量检查

### 4.1 业界怎么做

1. **格式层（schema validation）**：LeRobot 自己做的是"读取即校验"——`DatasetInfo.__post_init__` 校验 `fps>0`、`chunks_size>0`；`check_version_compatibility` 做版本门禁；`decode_video_frames` 用 `tolerance_s=1e-4`（默认）做**时间戳对帧号的对齐断言**，超差直接抛错。这正是"时间戳/视频帧对不上"的官方检测点。
2. **统计层（statistical reconciliation）**：用 `stats.json` 的 `count` 与 `total_frames` 对账；跨特征 `count` 一致性；`aggregate_stats` 在写 episode 时增量合并，**不重算**，所以"增量写入中断"会留下统计偏差。
3. **物理层（dynamics sanity）**：动作/状态的**跳变检测**（相邻帧差分超阈值）、速度/加速度上限、夹爪开合越界、关节限位越界。这是学术界做 data curation 的常见手段（RoboMIND 甚至专门保留失败示范作为标注数据）。
4. **人审层（human-in-the-loop）**：AgiBot World 明确强调"human-in-the-loop verification"的标准化流水线；社区侧有 LeRobot Dataset Visualizer 做逐 episode 浏览。
5. **平台层**：本仓库 `backend/bridge_ext/qc.py` 已有**标注层**质检（缺框、框越界、面积跳变、mask 缺失），**缺的正是格式层与数据层质检**——本文 B 节就是补这块。

### 4.2 常见的数据集质量问题（按出现频率）

| 问题 | 症状 | 根因 | 检测点 |
|---|---|---|---|
| **`finalize()` 未调用** | parquet 打不开 / 只有部分 episode / `total_frames` 远大于实际 | v3 增量写入，writer 未 flush 就没 footer | 打不开 parquet = 直接判损坏；官方 [PR #1903](https://github.com/huggingface/lerobot/pull/1903) |
| **丢帧** | `frame_index` 不连续；`length` 与视频帧数不符 | 采集线程卡顿 / 相机掉线 | 检查 `frame_index == 0..length-1`；视频 `nb_read_frames` 对比 |
| **时间戳不连续** | `timestamp` 步长不是 `1/fps` | 用墙钟而非帧计数 | `diff(timestamp)` 应恒为 `1/fps` |
| **视频与 parquet 帧数不匹配** | 解码报 tolerance 错误 / 动作与画面错位 | 编码丢帧、可变帧率、`to_timestamp` 计算错误 | 逐 episode：`to-from` 应等于 `length/fps`；mp4 总帧数应等于该文件内所有 episode 的 `length` 之和 |
| **动作跳变** | 某一帧动作突变 | 遥操作抖动 / 主从通讯丢包 / 量纲错误 | 逐维 `max|Δaction|` 与 `|Δstate|` 的比值异常 |
| **动作全零 / 常数** | 某维 std≈0 | 该关节没被记录（话题名写错） | `stats[key].std` 的某些维度 ≈ 0 |
| **state 与 action 量纲不一致** | state 单位 rad、action 单位 deg | 两路来源不同 | `stats["observation.state"].max` 与 `stats["action"].max` 数量级差 >10x |
| **相机黑屏 / 静止** | 画面全黑、或整段无变化 | USB 带宽不足、曝光失败 | 图像 `stats.min/max/mean` 三通道都极低；深入可看帧间差 |
| **任务文本缺失** | `task_index` 越界或 tasks 为空 | 录制时没给 `single_task` | `tasks` 行数为 0；`task_index ∉ [0,N)` |
| **episode 过短 / 过长** | 0 帧、1 帧、或 >10 分钟 | 采集者误操作 | `length` 分布与中位数对比 |
| **任务文本被字符串化** | tasks 里是 `"['指令']"` 而非 `["指令"]` | 早期脚本用 `str(list)` 导出 | 元素以 `[` 开头且 `ast.literal_eval` 成功【实测存在】 |
| **`total_tasks` 虚高** | 声明 47，实际只用 1 条 | 导出时统计了候选指令池 | tasks 行数 / `task_index` 去重数 三方对账【实测存在】 |
| **重复 episode** | 同一段视频/动作出现多次 | 重录未删旧数据 | 抽样 hash（视频 md5 或 `action` 前 N 帧哈希） |
| **`meta.bak` 等残留目录** | 扫描时 episodes 被数两遍 | 手动备份 | 只认白名单目录 |

---

## 5. 不确定 / 需实测确认的地方

1. **【需实测确认】v2.0→v2.1 之外还有没有 v2.x 细分**：我确认存在 `v2.0`、`v2.1` 两个 tag 值，但历史上 `codebase_version` 还出现过 `v2.0` 之前的 `"v1.0"`/`"v1.6"`（`convert_dataset_v1_to_v2.py` 存在）。扫描器遇到未知版本应**降级为"只读展示"而不是报错**。
2. **【已部分确认】`meta/tasks.parquet` 的文本列名**：v3.0 实测是 `__index_level_0__`（pandas index 落盘）；v2.x 的 `tasks.jsonl` 里叫 `task`。**扫描器统一按"非 `task_index` 的首个 string 列"读取**最稳妥。仍需对 `lerobot/*` 官方发布数据集抽样，确认是否存在写成 `task` 的 v3 导出。
3. **【需实测确认】`video.is_depth_map` 与 `is_depth_map`**：0.6.2 会把旧式标记规范化到 `info["is_depth_map"]`（`feature_utils.canonicalize_depth_marker`），但老数据集仍可能只有 `info["video.is_depth_map"]` 或单独 `feature["video_info"]["video.is_depth_map"]`。三者都要认。
4. **【需实测确认】大 episode 数据集的 `dataset_to_index` 是否严格连续**：理论上应严格首尾相接，但若中途删除过 episode（官方**不支持**删除，`validate_episode_buffer` 会报错），可能出现**空洞**。需要在一个被"手工修过"的数据集上验证。
5. **【需实测确认】`stats.json` 的 `count` 语义**：实测 `[60470]` 是全局帧数；但**逐 episode** 的 `stats/<key>/count` 在 episodes parquet 里是 `list<int64>`，应该是该 episode 的帧数。若混合数据集（分辨率不同）聚合，图像统计的形状可能不一致而**无法聚合**——需实测。
6. **【需实测确认】GR00T 的 embodiment tag 具体键名映射**：本机 `lerobot` 只有 GR00T N1.7 的新接口，embodiment 的具体 modality 键（如 `annotation.human.task_description` 之类）在本仓库中不完整，需以 NVIDIA Isaac-GR00T 官方仓库为准。
7. **【无法访问】** huggingface.co 在本机不可达，`https://huggingface.co/docs/lerobot/datasets/` 这类"网页版"链接未能亲自打开；本文所有官方结论都来自 GitHub 同源文档与源码。若需要截图/原文，需要能访问 HF 的环境复核。

---

# B. 数据集健康检查清单（伪代码规格）

> 定位：**格式 + 数据层**质检，与 `backend/bridge_ext/qc.py` 的**标注层**质检互补。
> 严重度：`error`（必须修，否则训练会出错/结果错）、`warning`（能训但有风险）、`info`（仅展示）。
> 前 5 项为 **L1 快检（目标 < 1 秒，只读 `info.json`）**，其余标 `深度检查`。

```python
# ========== 公共上下文 ==========
info      = json.load(root/"meta/info.json")
version   = info["codebase_version"]          # "v2.0" | "v2.1" | "v3.0"
features  = info["features"]
cams      = [k for k,v in features.items() if v["dtype"] in ("video","image")]
fps       = info["fps"]
```

---

### 【快检 1】数据集是否可识别（版本 + 必需文件）

```
检查项: 数据集格式可识别性与必需文件齐全
读取:   root/meta/info.json 是否存在且可被 json.load；
        info.codebase_version ∈ {"v2.0","v2.1","v3.0"}；
        v3.0 追加: root/meta/episodes/ 存在、root/meta/tasks.parquet 存在、
                   root/data/ 存在、root/videos/ 存在（features 含 video 时）
        v2.x  追加: root/meta/episodes.jsonl、root/meta/tasks.jsonl 存在
通过标准: 全部满足
失败提示: "无法识别为 LeRobot 数据集：{缺失项}。请确认该目录根部应包含 meta/info.json；
          如果是 RLDS/TFDS 或原始采集包（如 AgiBot / RoboMIND 原始版），需先转换为 LeRobot 格式。"
          版本未知时: "数据集版本 {version} 不在支持范围（v2.0/v2.1/v3.0），将按只读方式展示，无法保证可训练。"
失败时: 终止后续检查，返回 INVALID
```

---

### 【快检 2】episode 数量

```
检查项: episodes 数量
读取:   meta/info.json → total_episodes
通过标准: total_episodes > 0
失败提示: "数据集没有任何 episode（total_episodes = 0），请检查采集流程是否正确保存了 episode，
          以及是否在录制结束后调用了 dataset.finalize()。"
```

---

### 【快检 3】W0 必要特征字段

```
检查项: W0 核心字段存在性（W0 = 训练必需特征）
读取:   features 的 key 集合
通过标准: 含 "observation.state"、"action"，且二者 dtype 为数值（float32/float64/int64）
         且至少 1 个以 "observation.images." 开头的相机
         {"timestamp","frame_index","episode_index","index","task_index"} 这 5 个
         由框架自动注入的字段必须全部存在（DEFAULT_FEATURES）
失败提示: "缺少训练必需字段：{缺失列表}。
          - 缺少 observation.state / action：该数据集不是标准 LeRobot 格式，无法用于 VLA 训练。
          - 缺少 observation.images.*：没有视觉输入，只能做状态-动作模仿学习，VLA 模型不可用。
          - 缺少 timestamp/frame_index/episode_index/index/task_index：meta/info.json 的
            features 不完整，请重新生成 info.json 或从原始数据重新导出。"
严重度: error
```

---

### 【快检 4】任务表非空（W0）

```
检查项: 语言指令（任务表）
读取:   meta/info.json → total_tasks；v3.0: meta/tasks.parquet 的行数；
        v2.x: meta/tasks.jsonl 的行数
通过标准: total_tasks > 0 且 实际行数 > 0 且 二者一致
失败提示: "数据集没有任何任务描述（language instruction），VLA 模型依赖语言条件，无法训练。
          请在采集时通过 single_task 或逐 episode 的 task 字段提供指令文本。"
严重度: error（缺失）/ warning（数量不一致）
注意:   total_tasks 可能严重偏大（实测有 v2.1 数据集声明 total_tasks=47，
        但只有 1 个 episode 且实际只用了 1 条指令——47 是候选池大小而非实际使用数）。
        展示时以"task_index 去重数"为准，total_tasks 仅作参考。
```

---

### 【快检 5】关键规模与维度信息可展示（用于生成数据集卡片）

```
检查项: 展示用元信息可解析
读取:   meta/info.json → fps、features["action"].shape[0]、features["observation.state"].shape[0]、
        features[cam].shape 的 [H, W, C]、features[cam].info["video.codec"]
通过标准: fps > 0；action/state 的 shape 为长度 1 的 list 且 > 0；
         每个相机 shape 为长度 3 且 H/W/C 均 > 0
失败提示: "meta/info.json 中的 features 描述不完整或不合法（{具体项}），
          无法计算动作维度/相机分辨率，训练时将无法自动配置模型输入。"
附加输出（不判失败）: 展示卡片 = { episodes, frames, tasks, fps,
          动作维度, 状态维度, 相机数, 各相机分辨率, 机器人类型, 视频编码 }
```

---

### 【深度检查 6】episode 元数据实测值（v3.0）

```
检查项: 声明的 episode 数 / 帧数 与实际元数据一致
读取:   meta/episodes/**/*.parquet 的 episode_index 与 length 列
        （只读列，不读 stats/* 大列，避免把 60 张图统计读进内存）
通过标准: 行数 == info.total_episodes；sum(length) == info.total_frames；
         episode_index 恰为 0..N-1 且无重复
失败提示: "数据集元数据与 info.json 不一致：
          声明 {total_episodes} 个 episode / {total_frames} 帧，
          实际 {real_episodes} 个 / {real_frames} 帧。
          常见原因：录制中途异常退出未调用 finalize()，或手工编辑过 info.json。
          建议以实际值为准，并用 lerobot 重新生成 info.json。"
严重度: error（实际为 0）/ warning（仅数量不等）
```

---

### 【深度检查 7】data parquet 列与类型（v3.0）

```
检查项: data 分片存在且 schema 与 features 一致
读取:   由 episodes 列推出的每个 data_path；对每个文件读 parquet metadata（
        不读数据体，reader.read(columns=[]) 或 ParquetFile.metadata）、抽样 100 行校验
通过标准: 每个 episode 引用的 data 文件都存在；
         列集合 == features 的数值特征列 ∪ 标量系统列；
         observation.state / action 每行的 list 长度 == features[*].shape[0]
失败提示: "数据文件缺失或列不匹配：{文件} {问题}。
          若报 'File not found'，通常是 data_path 模板与实际目录不一致，或 data 文件未上传；
          若报 '列长度不一致'，说明部分帧的动作维度与 info.json 声明不符（实例：{实际长度} vs {声明}）。"
严重度: error
```

---

### 【深度检查 8】全局帧序号连续性

```
检查项: index / episode_index / frame_index 三者自洽
读取:   data/*.parquet 的 index、episode_index、frame_index（只读这三列，可内存映射）
通过标准: index 恰为 0..total_frames-1 且严格递增；
         每个 episode 的 frame_index 恰为 0..length-1；
         同一 (episode_index, frame_index) 不重复
失败提示: "帧序号不连续：在 index={n} 处出现断档/重复。
          这通常意味着采集阶段丢帧或中途拼接。训练时相邻帧的时序关系会错乱，
          建议删除该 episode（episode_index={k}）或重新采集。"
严重度: error
```

---

### 【深度检查 9】时间戳连续性

```
检查项: timestamp 是否严格为 1/fps 的等差序列
读取:   data/*.parquet 的 episode_index、timestamp
通过标准: 每个 episode 内 timestamp 从 0.0 开始，相邻差恒为 1/fps（容差 1e-4 秒）；
         该 episode 内 timestamp_range == (length-1)/fps
失败提示: "时间戳不连续：episode {k} 出现 {n} 处非 1/{fps}s 的时间间隔
          （最大偏差 {d} s，出现在帧 {f}）。
          这会导致视频解码时按时间戳取帧失败（LeRobot 默认容差 1e-4s，会直接抛异常）。
          请检查采集时是否用墙钟时间而非帧计数生成 timestamp。"
严重度: error
```

---

### 【深度检查 10】video / data 路径模板可解析（v3.0）

```
检查项: data_path / video_path 模板能被实际值填充
读取:   info.data_path、info.video_path，用 episodes 里的
        data/chunk_index、data/file_index、videos/<cam>/chunk_index、videos/<cam>/file_index 填充
通过标准: 所有填充后的文件都存在；chunk 目录名符合 chunks_size（任意 file_index < chunks_size）
失败提示: "视频路径无法解析：{模板} 填入 chunk={c}, file={f} 后为 {路径}，但文件不存在。
          请确认 info.json 的 video_path 模板与 videos/ 目录结构一致（不要手工修改目录名）。"
严重度: error
```

---

### 【深度检查 11】视频与 parquet 帧数/时长匹配

```
检查项: 每个 mp4 的帧数 = 其承载的所有 episode 的 length 之和；
        每个 episode 的 (to_timestamp - from_timestamp) == length / fps
读取:   episodes parquet 的 videos/<cam>/from_timestamp、to_timestamp、chunk_index、file_index、length；
        mp4 用 ffprobe -count_frames 得到 nb_read_frames、avg_frame_rate、duration
通过标准: 偏差 < 1 帧；帧率 == fps；视频分辨率 == features[cam].shape[:2]
失败提示: "视频与数据帧数不一致：{mp4} 实际 {real} 帧，元数据期望 {expected} 帧
          （差异 {diff} 帧）。常见原因：编码丢帧、可变帧率源、或 ffmpeg 使用了 -vsync 丢帧。
          该 episode 的动作与画面会错位，建议对该 episode 重新编码或剔除。"
严重度: error
```

---

### 【深度检查 12】任务索引有效性

```
检查项: task_index 可解析到任务文本
读取:   meta/tasks.parquet（task_index 列 + 首个 string 列）；
        data/*.parquet 的 task_index 取值集合；episodes 的 tasks 列
通过标准: data 中出现的所有 task_index ∈ tasks 表行号范围；
         tasks 表的 task_index 恰为 0..N-1 连续；
         任务文本非空、无重复
失败提示: "任务索引越界：帧中出现了 task_index={i}，但任务表只有 {N} 条。
          LeRobot 通过'S行号'定位任务文本，越界会导致取到错误指令或直接崩溃。
          若任务表文本列名不是 'task'（实测常见为 __index_level_0__），也请检查导出脚本。"
严重度: error
```

---

### 【深度检查 12b】任务文本形态（v2.x 高频脏数据）

```
检查项: 任务文本是否被字符串化/含脏格式
读取:   v2.x: meta/tasks.jsonl 的 "task" 字段、meta/episodes.jsonl 的 "tasks" 列表元素；
        v3.0: meta/tasks.parquet 的首个 string 列、episodes 里的 "tasks" list 元素
通过标准: 任务文本为纯人类可读句子；不以 "[" 开头、" ]" 结尾；能通过
         ast.literal_eval 解析成 list 的字符串视为"被字符串化"
失败提示: "任务文本被错误地序列化成了字符串：{示例}。
          这是早期导出脚本用 str(list) 而不是 list 造成的。
          风险：语言指令会带着方括号和引号进入模型，语言条件分布与预训练不一致。
          修复：把元素解析后展开为真正的列表，或重新导出 tasks 表。"
严重度: warning
实测: 真实 v2.1 数据集 lerobot_v21_sample_h264 中 episodes.jsonl 的 tasks 即为
      "['Pick the bottle with ridges near base head-up using the left arm']"（字符串化的 list）。
```

---

### 【深度检查 13】episode 全局偏移自洽（v3.0）

```
检查项: dataset_from_index / dataset_to_index 连续切片
读取:   meta/episodes/**/*.parquet 的 episode_index、dataset_from_index、dataset_to_index、length
通过标准: 第一个 episode 的 from == 0；
         第 i 个 episode 的 from == 第 i-1 个的 to；
         to - from == length；
         最后一个 to == total_frames
失败提示: "episode 全局偏移不连续：episode {k} 的 dataset_from_index={a}，
          但上一个 episode 结束于 {b}（期望 {b}）。
          这会导致同一帧被两个 episode 同时索引，或出现无主帧。请重新生成 episodes 元数据。"
严重度: error
```

---

### 【深度检查 14】动作/状态数值健康度

```
检查项: NaN/Inf、常量维度、量纲一致性、跳变
读取:   meta/stats.json（快）+ data/*.parquet 的 action / observation.state（慢）
通过标准: min/max/mean/std 均为有限值；
         std 中不存在 ≈0 的维度（常量关节）；
         observation.state 与 action 的 |max| 数量级相差 < 10 倍；
         逐维相邻帧 |Δaction| 的 P99.9 < 该维全程范围；
         |Δstate| 与 |Δaction| 的比值中位数在 [0.1, 10]
失败提示: "动作数据异常：
          - 维度 {d}（{name}）标准差≈0，该关节可能未被正确记录（检查话题/端口配置）。
          - state 与 action 量纲疑似不一致（state max={x}，action max={y}，相差 {r} 倍，
            常见于一方用弧度一方用角度）。
          - episode {k} 的帧 {f} 处动作跳变 {d}（{v0}→{v1}），超过该维范围 {p}%，
            疑似遥操作主从通讯丢包。"
严重度: warning（NaN/Inf 为 error）
```

---

### 【深度检查 15】stats.json 与数据对账

```
检查项: 归一化统计与数据同步
读取:   meta/stats.json；info.total_frames
通过标准: stats 覆盖 features 中所有非图像特征；
         stats["<任意>#count"][0] == total_frames；
         各特征的 count 彼此一致
失败提示: "meta/stats.json 与数据不同步：统计帧数 {c} ≠ 实际总帧数 {n}（或部分特征缺失统计）。
          归一化会使用错误的均值/方差，导致训练发散。
          请用 lerobot 重新计算统计：python -m lerobot.datasets.compute_stats 或重新导出数据集。"
严重度: error（数量不符）/ warning（缺个别特征）
```

---

### 【深度检查 16】分位数统计（模型兼容性前置项）

```
检查项: stats.json 是否包含 q01/q99（π0.5 / QUANTILES 归一化必需）
读取:   meta/stats.json → stats["action"].keys()、stats["observation.state"].keys()
通过标准: 若目标模型 ∈ {pi05}（或 policy.normalization_mapping.ACTION == QUANTILES），
         必须同时存在 "q01" 与 "q99"（π0.5 还可用 QUANTILE10 → q10/q90）
失败提示: "meta/stats.json 缺少分位数统计（q01/q99）。
          该数据集目前只能用于 MEAN_STD 类模型（π0、SmolVLA、ACT），无法直接微调 π0.5。
          补齐方法：python -m lerobot.scripts.augment_dataset_quantile_stats --repo-id=<repo_id>
          （该脚本会重算并写回 meta/stats.json，耗时与数据量成正比）。"
严重度: warning（不阻塞训练，但阻塞特定模型）
备注: 本条判定应"按目标模型"触发，不要一律报错——绝大多数 LeRobot 导出数据集都没有 q* 键。
```

---

### 【深度检查 17】图像内容健康度（抽样）

```
检查项: 相机是否黑屏 / 冻结 / 分辨率漂移
读取:   meta/stats.json 中每个相机的 min/max/mean（逐像素统计）；
        抽 3 个 episode 用 decode_video_frames 取首/中/末帧
通过标准: 相机 mean 不是全 0 / 全 255；帧间差异 > 阈值；
         实际解码尺寸 == features[cam].shape
失败提示: "相机 {cam} 疑似异常：整段画面均值 {m}（接近全黑/全白），或相邻帧几乎无变化（冻结）。
          请检查相机连接、曝光参数与视频采集进程。"
严重度: warning
```

---

### 【深度检查 18】episode 时长分布

```
检查项: episode 长度合理性
读取:   meta/episodes/**/*.parquet 的 length
通过标准: 不存在 length < 2×fps（<2 秒）或 > 10 分钟的 episode；
         中位数在 [2 秒, 2 分钟]
失败提示: "{n} 个 episode 时长异常（最短 {min} 帧 / 最长 {max} 帧，中位数 {med} 帧）。
          过短的 episode 通常是误触开始/结束；过长的可能是采集者忘记结束。
          建议剔除或切分，否则会显著拉低训练效率。"
严重度: warning
```

---

### 【深度检查 19】目录整洁度

```
检查项: 是否存在干扰目录/残留文件
读取:   root 下的一级目录白名单：meta/ data/ videos/ images/
通过标准: 不存在 meta.bak/、meta_old/、data.bak/ 等备份目录；
         不存在 *.jsonl（v3.0 中应已清理）
         白名单（平台私有 sidecar，允许存在、不参与 LeRobot 解析）：
         _platform/、segments/、labelvla_revision.json
失败提示: "检测到非标准目录 {dirs}。这些目录不会被 LeRobot 读取，但可能让使用者误以为
          数据被重复统计。建议移出数据集根目录或改名隐藏。"
严重度: info
```

---

### 快检/深检 与 5 秒预算的对应关系

| 层级 | 检查项 | 读什么 | 实测耗时量级 | 建议触发时机 |
|---|---|---|---|---|
| **L1 快检** | 1–5 | 仅 `meta/info.json`（<10 KB） | **< 10 ms** | 数据集列表页、上传完成回调，同步返回 |
| **L2 深检（元数据）** | 6、13、15、16、18 | `meta/episodes/**/*.parquet`（只读 `length`/索引列）+ `meta/stats.json` | **10–200 ms**（1 万 episode 约几十 ms） | 数据集详情页打开时，可同步或 1 次异步 |
| **L3 深检（数据）** | 7、8、9、12、14 | `data/**/*.parquet`（列裁剪 + 内存映射） | **秒级~分钟级**（与帧数成正比） | 后台任务，落库，前端轮询 |
| **L4 深检（媒体）** | 10、11、17 | `videos/**/*.mp4`（ffprobe） | **每文件 10–100 ms**，全量可达分钟~小时级 | 手动触发 / 训练前门禁 |
| **L5 目录** | 19 | `root` 一级目录 | < 5 ms | 并入 L1 |

> 实现要点：L2/L3 一律 **只读需要的列**（`pq.read_table(..., columns=[...])`），绝不整表加载——`1028pick_3.0` 的 episodes parquet 含 60 个 `stats/*` 嵌套大列（图像 H×W×C 逐像素），整表读取会让"几十毫秒"变成"几秒"。ffprobe 建议 `-count_frames` 只用于抽检，全量统计改用容器元数据（`nb_frames`），并标注"容器声明值"。

### 汇总判定

```python
score = weighted(errors, warnings)          # error 权重 10，warning 权重 2
status = "INVALID"  if 快检1 失败
       | "BROKEN"   if errors >= 1
       | "RISKY"    if warnings >= 1
       | "HEALTHY"
输出: { dataset, version, status, score,
        display_card: {...},              # 来自快检 5
        checks: [{id, name, severity, passed, message, evidence}] }
```

---

# C. LeRobot v3.0 最小目录树示例（基于服务器真实数据集）

数据来源：`/home/jjx/1028pick_3.0`（通过 `/home/robam/labelvla-docker/.ssh_run.py` 实际拉取）。

## C.1 真实目录树（`find -maxdepth 3`）

```text
/home/jjx/1028pick_3.0
├── meta.bak/                                  # ⚠️ 残留备份，扫描器应忽略
│   ├── info.json
│   ├── stats.json
│   ├── tasks.parquet
│   └── episodes/chunk-000/
├── meta/
│   ├── info.json                  3.5 KB     # 声明: 143 eps / 60470 frames / 1 task / 30 fps
│   ├── stats.json                 6.5 KB     # 10 个特征的全量统计，count=[60470]
│   ├── tasks.parquet              2.1 KB     # 1 行: task_index=0, "pick up the bowl"
│   └── episodes/
│       └── chunk-000/
│           └── file-000.parquet  66 KB       # 143 行 = 143 个 episode
├── data/
│   └── chunk-000/
│       └── file-000.parquet       4.1 MB     # 60 470 行 = 全部帧，1 个 row group
└── videos/
    ├── observation.images.camera1/chunk-000/file-000.mp4 …  # 640x480, h264, yuv420p, 30fps
    ├── observation.images.camera2/chunk-000/file-000.mp4 …
    └── observation.images.camera3/chunk-000/file-000.mp4 …
```

## C.2 "最小可训练"目录树模板

```text
<dataset_root>/
├── meta/
│   ├── info.json                            # 【必需】
│   ├── stats.json                           # 【必需】归一化
│   ├── tasks.parquet                        # 【必需】task_index + task 文本列
│   └── episodes/
│       └── chunk-000/file-000.parquet       # 【必需】episode 边界/长度/路径索引
├── data/
│   └── chunk-000/file-000.parquet           # 【必需】state/action/timestamp/...
└── videos/
    └── observation.images.<cam>/            # 【每个相机一个】仅当 features 中 dtype=="video"
        └── chunk-000/file-000.mp4
```

**最少 7 个文件**即可成为一个可训练的 v3.0 数据集（1 episode 时：info.json / stats.json / tasks.parquet / episodes file-000.parquet / data file-000.parquet / 每相机 1 个 mp4）。注意 `images/` 目录只有在相机 `dtype=="image"` 时才会出现，v3.0 的常规路径不使用它。

## C.3 实测数据（可直接作为扫描器的回归测试用例）

| 校验点 | 实测结果 | 结论 |
|---|---|---|
| `info.total_episodes` | 143 | ✅ |
| `meta/episodes/chunk-000/file-000.parquet` 行数 | 143 | ✅ 一致 |
| `sum(episodes.length)` | 60470 | ✅ == `info.total_frames` |
| `data/chunk-000/file-000.parquet` 行数 | 60470 | ✅ == `total_frames` |
| `stats["observation.state"].count` | `[60470]` | ✅ |
| episode 0 | `from=0, to=493, len=493, cam1 ts=0.0→16.4333` | ✅ |
| episode 1 | `from=493, to=994, len=501, cam1 ts=0.0→16.7` | ✅ 首尾相接 |
| `ffprobe file-000.mp4` | `nb_read_frames=493`, `r_frame_rate=30/1`, `duration=16.433333`, `640x480` | ✅ 与 ep0 完全吻合 |
| `features.action.shape` | `[14]`，names 为 `arm1_joint_1.cmd … arm2_gripper.cmd` | 双机械臂 7+7 维 |
| `features.observation.state.shape` | `[14]`，names 为 `arm1_joint_1.pos … arm2_gripper.pos` | 与 action 同构 |
| 相机 | 3 路 `camera1/2/3`，均 480×640×3，h264/yuv420p | ✅ |
| episode 长度 | min 229 / max 978 / 中位 **417** / 均值 422.9 帧 | 7.6s ~ 32.6s（中位 13.9s），合理 |
| `meta.bak/` | 存在完整旧 meta 副本 | ⚠️ 需忽略 |

> **注意**：`camera1/2/3` 这种命名没有任何语义（无法判断哪路是 wrist / 高位）。这是跨模型适配时"相机槽位映射"必须人工介入的直接原因，建议平台在展示层支持**相机别名字段**（写入 `info.json` 的自定义 key 或在平台侧维护映射表）。

---

## 附：参考链接汇总

**规范（官方）**
- LeRobot Dataset v3.0 文档源码：<https://github.com/huggingface/lerobot/blob/main/docs/source/lerobot-dataset-v3.mdx>（网页版 <https://huggingface.co/docs/lerobot/lerobot-dataset-v3>）
- v2.1→v3.0 移植指南：<https://github.com/huggingface/lerobot/blob/main/docs/source/porting_datasets_v3.mdx>
- 数据集工具/可视化：<https://github.com/huggingface/lerobot/blob/main/docs/source/using_dataset_tools.mdx>
- v2.1 路径常量（v0.3.3）：<https://github.com/huggingface/lerobot/blob/v0.3.3/src/lerobot/datasets/utils.py>
- v2.0→v2.1 转换脚本：<https://github.com/huggingface/lerobot/blob/v0.3.3/src/lerobot/datasets/v21/convert_dataset_v20_to_v21.py>
- v2.1→v3.0 转换脚本：<https://github.com/huggingface/lerobot/blob/main/src/lerobot/scripts/convert_dataset_v21_to_v30.py>
- `finalize()` 必要性：<https://github.com/huggingface/lerobot/pull/1903>
- 本地实现：`/home/robam/lerobot/src/lerobot/datasets/{dataset_metadata,utils,feature_utils,video_utils,dataset_reader}.py`

**数据集论文**
- OXE <https://arxiv.org/abs/2310.08864> ｜ RT-1 <https://arxiv.org/abs/2212.06817> ｜ RT-2 <https://arxiv.org/abs/2307.15818>
- DROID <https://arxiv.org/abs/2403.12945> ｜ LIBERO <https://arxiv.org/abs/2306.03310> ｜ CALVIN <https://arxiv.org/abs/2112.03227>
- RoboCasa <https://arxiv.org/abs/2406.02523> ｜ AgiBot World <https://arxiv.org/abs/2503.06669>
- RoboMIND v1 <https://arxiv.org/abs/2412.13877> ｜ RoboMIND 2.0 <https://arxiv.org/abs/2512.24653>

**模型**
- OpenVLA <https://arxiv.org/abs/2406.09246> ｜ GR00T N1 <https://arxiv.org/abs/2503.14734> ｜ SmolVLA <https://arxiv.org/abs/2506.01844>
- 本地实现：`/home/robam/lerobot/src/lerobot/policies/{pi05,pi0,smolvla,groot}/configuration_*.py`