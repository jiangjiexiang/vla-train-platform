# LeRobot v3.0 数据集格式：真正跑起来才知道的四个坑

这份文档记的是**把一份自以为合规的 v3.0 数据集喂给 lerobot 训练时，连续撞上的四个 KeyError**。
每个都是 lerobot 源码里写死的取值方式决定的，不是文档里显眼写的。

## 背景

`pick1028`（171 集 / 73,434 帧 / 3 相机 / 30fps）转换完成后：

```python
LeRobotDataset("pick1028", root="/datasets/pick1028")
# episodes: 171  frames: 73434  fps: 30     ← 看起来完全正常
```

但 `train()` 里一取数据就崩。

## 坑 1：episodes 元数据的列名要用斜杠，不是下划线

lerobot 的取法（`lerobot_dataset.py:200-218`）：

```python
chunk_idx = ep["data/chunk_index"]
file_idx  = ep["data/file_index"]
chunk_idx = ep[f"videos/{vid_key}/chunk_index"]
file_idx  = ep[f"videos/{vid_key}/file_index"]
```

转换脚本生成的是 `data_chunk_index` / `video_chunk_index`——下划线风格、
而且视频没有按相机区分。lerobot 一读就：

```
KeyError: 'videos/observation.images.camera1/chunk_index'
```

**修法**：`meta/episodes/chunk-000/file-000.parquet` 里改列名，每台相机一组：

```
data/chunk_index
data/file_index
videos/observation.images.camera1/chunk_index
videos/observation.images.camera1/file_index
videos/observation.images.camera2/...
videos/observation.images.camera3/...
```

## 坑 2：缺 from_timestamp / to_timestamp

列名改对后，`__getitem__` 又崩：

```
KeyError: 'videos/observation.images.camera1/from_timestamp'
```

lerobot 读视频时（`_query_videos`）：

```python
from_timestamp = ep[f"videos/{vid_key}/from_timestamp"]
shifted_query_ts = [from_timestamp + ts for ts in query_ts]
frames = decode_video_frames(video_path, shifted_query_ts, ...)
```

含义是**「这一集的第 0 帧，落在该 mp4 的第几秒」**。

官方写入器把多集拼进一个 mp4，所以这个值要累加。
我们是**每集一个 mp4**（`file-{ep:03d}.mp4`），所以：

```python
from_timestamp = 0.0
to_timestamp   = length / fps     # 该集时长（秒）
```

## 坑 3：磁盘上的 mp4 与 info.json 的 video_path 模板必须能对上

`info.json` 里：

```json
"video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"
```

`chunk_index` / `file_index` 来自 episodes 元数据。我们的文件是
`videos/<相机>/file-000.mp4` 这种平铺结构，所以 `chunk_index=0`、`file_index=集号`，
**并且路径里不能出现 `chunk-000/` 这一层**——要么改磁盘结构，要么把 `video_path`
模板改成不含 chunk 的形式。

## 坑 4：torchcodec 缺失会降级，不影响功能

```
WARNING: 'torchcodec' is not available in your platform, falling back to 'pyav'
```

只是警告，视频仍能解码。不用处理。

## 验证方法

改完元数据后，**必须验证到 DataLoader 出 batch**，只看 `num_episodes` 是不够的：

```python
ds = LeRobotDataset("pick1028", root="/datasets/pick1028")
assert ds.num_episodes == 171

from torch.utils.data import DataLoader
batch = next(iter(DataLoader(ds, batch_size=2, num_workers=0)))
# observation.images.camera1: (2, 3, 480, 640) float32
# observation.state:          (2, 14)          float32
# action:                     (2, 14)          float32
```

前面三个坑分别会在 `__init__` 和 `__getitem__` 阶段抛 KeyError，
只有走到这一步才算真的能用。

## 修复脚本

一次性转换完成后，可用 `/home/jjx/fix_episodes.py` 和 `/home/jjx/fix_timestamps.py`
对已有数据集做原地修补（两个脚本都通过 buildah 在 lerobot 镜像里跑，
因为宿主机没有 pyarrow / pandas）。

下次**重新转换**数据集时，应该直接把这一步做进转换脚本，
而不是转换完再拿这两个补丁去救。
