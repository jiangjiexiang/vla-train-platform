"""TensorBoard events 读取（训练曲线）。

约定：训练脚本把 TensorBoard events 写到 ``/checkpoints/<job>/tensorboard/``，
平台**直接解析 events 文件**，而不是另起一个常驻的 ``tensorboard --logdir`` 服务：

- 常驻服务要多一个进程、一个端口和一套生命周期管理，单机部署上不划算；
- 训练结束后 events 文件还躺在宿主机磁盘上，按需解析就能看到全部历史，
  不受容器 / TTL 回收影响；
- 前端已有折线图组件，拿到 JSON 直接画，不必嵌 iframe。

实现要点（返回结构与 ``metrics.py`` 对齐，前端可以复用同一套渲染逻辑）：
- events 目录或文件不存在时返回 ``path_exists: false``，不报错——
  老任务、刚提交的任务、没装 hook 的脚本都没有这个目录；
- 训练中文件正在被写：遇到不完整或被截断的尾部记录就停下，保留之前的完整记录，
  不抛异常，所以读到的总是最后一次 flush 之前的数据；每次请求都重新读取、不缓存；
- 只解析前端关心的几个 tag，并且**每个 events 文件只读一遍**：性能瓶颈在解析字节，
  不在 tag 个数；
- 点数超过 ``limit`` 时**均匀抽稀并保留首尾**，而不是只留最后 N 个——
  训练早期的 loss 对判断收敛同样重要。
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

# 训练脚本写 events 的子目录（相对 /checkpoints/<job>）
TENSORBOARD_SUBDIR = "tensorboard"
EVENTS_PREFIX = "events.out.tfevents."

# 默认读取的 tag。与训练侧 images/smolvla-train/metrics_hook.py 的写法逐字对应：
# 它写的是 SummaryWriter.add_scalar(key, ...) 的裸键（不加 ``train/`` 前缀），
# 与 metrics.jsonl 的字段名相同。``train/*`` 这几项是老版本/第三方脚本的常见写法，
# 一并保留，有就返回、没有就跳过。
DEFAULT_TAGS: tuple[str, ...] = (
    "loss",
    "grad_norm",
    "lr",
    "update_s",
    "train/loss",
    "train/grad_norm",
    "train/lr",
    "train/update_s",
)

MAX_POINTS = 100_000
DEFAULT_LIMIT = 2000
# 同一 run 被重启会另起一个 events 文件；只读最近若干个，
# 防止反复重启把目录刷爆之后单次请求越来越慢。
MAX_EVENTS_FILES = 20


class TensorboardUnavailable(RuntimeError):
    """镜像里没装 tensorboard（或版本 ABI 不兼容）时抛出，由路由层转成 503。"""


def tensorboard_dir(checkpoint_root: str | Path, job_name: str) -> Path:
    return Path(checkpoint_root) / job_name / TENSORBOARD_SUBDIR


def events_files(root: Path) -> list[Path]:
    """列出 root 下的 events 文件，按修改时间升序。

    文件名里虽然有 timestamp，但 EventFileWriter 在同一秒内重启会重名，
    直接用 mtime 更稳；mtime 相同时再按文件名排，保证顺序确定。
    """
    if not root.is_dir():
        return []
    try:
        found = [path for path in root.iterdir() if path.name.startswith(EVENTS_PREFIX) and path.is_file()]
    except OSError:
        return []

    def sort_key(path: Path) -> tuple[float, str]:
        try:
            return (path.stat().st_mtime, path.name)
        except OSError:
            return (0.0, path.name)

    return sorted(found, key=sort_key)


def read_scalars(
    root: str | Path,
    job: str,
    limit: int = DEFAULT_LIMIT,
    tags: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """读取某个任务的 TensorBoard 标量曲线。

    返回 ``{"name", "tags", "series", "path_exists", "files", "total_points", "truncated", "dir"}``：

    - ``tags``：实际有数据的 tag（前端据此隐藏空标签页）；
    - ``series``：``{tag: [{"step", "value", "wall_time"}, ...]}``，按 step 升序，
      同一 step 以最后写入的为准（训练可能重跑某步）；
    - ``path_exists``：events 目录存在且至少有一个 events 文件；
    - ``dir``：实际扫描的目录，排查「为什么没数据」时有用。

    文件不存在不是错误：老任务、刚开始跑的任务本来就没有 events。
    """
    events_root = tensorboard_dir(root, job)
    limit = max(1, min(int(limit), MAX_POINTS))
    wanted = [tag for tag in (tags if tags else DEFAULT_TAGS) if tag]
    files = events_files(events_root)
    result: dict[str, Any] = {
        "name": job,
        "tags": [],
        "series": {},
        "path_exists": bool(files),
        "files": [path.name for path in files[-MAX_EVENTS_FILES:]],
        "total_points": 0,
        "truncated": False,
        "dir": str(events_root),
    }
    if not files:
        return result

    _event_accumulator()  # 依赖缺失时尽早抛 TensorboardUnavailable
    # 每个 events 文件只读一遍：性能瓶颈在解析字节，不在 tag 个数
    per_file = [_read_file(path, wanted) for path in files[-MAX_EVENTS_FILES:]]
    for tag in wanted:
        by_step: dict[int, dict[str, Any]] = {}
        for points in per_file:
            for point in points.get(tag, ()):  # 同一 step 以后写的文件为准
                by_step[point["step"]] = point
        items = [by_step[step] for step in sorted(by_step)]
        if not items:
            continue
        result["total_points"] += len(items)
        if len(items) > limit:
            items = _downsample(items, limit)
            result["truncated"] = True
        result["tags"].append(tag)
        result["series"][tag] = items
    return result


def _read_file(path: Path, tags: list[str]) -> dict[str, list[dict[str, Any]]]:
    """读一个 events 文件里指定 tag 的标量。坏文件返回空 dict，绝不让整个接口 500。"""
    try:
        return _read_file_tfrecord(path, tags)
    except Exception:  # noqa: BLE001 - 格式异常/IO 错误都按「该文件暂无数据」处理
        pass
    try:
        return _read_file_accumulator(path, tags)
    except Exception:  # noqa: BLE001
        return {}


def _read_file_tfrecord(path: Path, tags: list[str]) -> dict[str, list[dict[str, Any]]]:
    """手工按 TFRecord 帧读取（默认路径）。

    为什么不用 ``EventAccumulator``：它内部会对**每个事件里的每个标量**做
    ``data_compat.migrate_value``，把 simple_value 重新打包成 TensorProto。
    实测 500 个 tag × 5000 步的 events（52 MB）要 64 秒，换个进程再解析一遍
    也无济于事。这里只做两件事：
    - 用 tag 名对应的字节预筛，不含目标 tag 的帧直接跳过（不解码 protobuf）；
    - 命中的帧 ``Event.FromString`` 后直接取 ``simple_value``，跳过迁移。
    同样的文件约 0.3 秒。

    容错：帧头/帧体不完整（训练正在写）时停下，已读到的记录照常返回。
    """
    from tensorboard.compat.proto.event_pb2 import Event
    from tensorboard.util import tensor_util

    tokens = [tag.encode("utf-8") for tag in tags]
    points: dict[str, list[dict[str, Any]]] = {tag: [] for tag in tags}
    data = path.read_bytes()
    total = len(data)
    position = 0
    while position + 12 <= total:
        (size,) = struct.unpack("<Q", data[position : position + 8])
        next_position = position + 12 + size + 4
        if next_position > total:
            break  # 尾部半条记录：最后一次 flush 之后写入的，丢掉即可
        payload = data[position + 12 : position + 12 + size]
        position = next_position
        if not any(token in payload for token in tokens):
            continue
        event = Event.FromString(payload)
        if not event.HasField("summary"):
            continue
        for value in event.summary.value:
            if value.tag not in points:
                continue
            number = _scalar_value(value, tensor_util)
            if number is None:
                continue
            points[value.tag].append(
                {"step": int(event.step), "value": number, "wall_time": float(event.wall_time)}
            )
    return points


def _scalar_value(value: Any, tensor_util: Any) -> float | None:
    """取标量数值。simple_value 是 TF1/SummaryWriter 的写法，tensor 是新版 SummaryWriter 的写法。"""
    simple = value.simple_value
    if simple:
        return float(simple)
    tensor = value.tensor
    if tensor is None or not tensor.ByteSize():
        return None
    try:
        return float(tensor_util.make_ndarray(tensor).reshape(()))
    except Exception:  # noqa: BLE001 - 非标量张量（图像/直方图）忽略
        return None


def _read_file_accumulator(path: Path, tags: list[str]) -> dict[str, list[dict[str, Any]]]:
    """走 ``EventAccumulator``，手工读法抛异常时的兜底（例如加密或压缩格式）。"""
    accumulator_cls = _event_accumulator()
    accumulator = accumulator_cls(str(path), size_guidance=_size_guidance())
    accumulator.Reload()
    out: dict[str, list[dict[str, Any]]] = {}
    for tag in tags:
        try:
            scalars = accumulator.Scalars(tag)
        except KeyError:
            continue
        out[tag] = [
            {"step": int(event.step), "value": float(event.value), "wall_time": float(event.wall_time)}
            for event in scalars
        ]
    return out


def _downsample(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """等间隔抽稀并保留首尾。比 ``items[-limit:]`` 更能看出整条曲线的形状。"""
    if len(items) <= limit:
        return items
    if limit == 1:
        return [items[-1]]
    stride = (len(items) - 1) / (limit - 1)
    picked = [items[round(index * stride)] for index in range(limit)]
    picked[-1] = items[-1]
    return picked


def _size_guidance() -> dict[str, int]:
    # 0 = 内存里只留最新一份。events 里可能混着图像/直方图，不限的话
    # 读一个大 run 会直接把 Web Pod 的内存撑爆。
    return {
        "scalars": 0,
        "tensors": 0,
        "histograms": 0,
        "images": 0,
        "audio": 0,
        "distributions": 0,
        "graph": 0,
        "meta_graph": 0,
    }


def _event_accumulator() -> type:
    try:
        from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
    except Exception as exc:  # noqa: BLE001 - 依赖缺失与 ABI 不匹配归为一类
        raise TensorboardUnavailable(f"tensorboard 不可用：{exc}") from exc
    return EventAccumulator


def available() -> bool:
    """当前进程能否解析 events。缺依赖时路由层返回 503，而不是 500。"""
    try:
        _event_accumulator()
    except TensorboardUnavailable:
        return False
    return True