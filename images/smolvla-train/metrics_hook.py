"""把 lerobot 的训练指标转写成平台的 metrics.jsonl，并同步一份 TensorBoard events。

lerobot 的 loss 只进 wandb 或终端进度条，不写任何文件。平台要画 loss 曲线就得
自己接。这里不 patch lerobot 源码，而是在进程内替换 MetricsTracker 的属性写入
——升级 lerobot 时不会冲突，找不到目标也只是少一条曲线，不影响训练。

为什么不用 logging hook：lerobot 只在 log_freq 命中的步才打日志，而且格式是
进度条，解析脆弱。MetricsTracker 每个训练步都会更新，是更稳定的接入点。

为什么 TensorBoard 也挂在这里、而不是让 lerobot 自己输出：
    1. lerobot 确实有 ``--wandb.enable``，但那是 wandb，不是 TensorBoard；
       要让它吐 events 得改 lerobot 的 wandb 集成或自己写 callback，前者升级即冲突，
       后者要动训练主循环。
    2. 这个 hook 已经被 sitecustomize 注入到**训练进程本身**，拿到的正是每个
       训练步的原始指标——再开一个 SummaryWriter 只是"多写一份"，零额外接线成本，
       也不改 lerobot。
    3. 写 events 和写 metrics.jsonl 用的是同一份数值、同一个 step 语义
       （含续训偏移），两边的曲线天然对得上，不会出现"两张图步数不一致"。
"""

from __future__ import annotations

import atexit
import importlib
import json
import os
import sys
from pathlib import Path

# 平台约定的指标文件位置（与 backend/app/services/metrics.py 一致）
METRICS_FILENAME = "metrics.jsonl"
# TensorBoard 日志目录名。events 落在这里而不是直接落在 CHECKPOINT_DIR，
# 是为了不和 lerobot 的产物（run/、checkpoints/）混在一起被当成本任务产物。
TENSORBOARD_DIRNAME = "tensorboard"
# 要写进 TensorBoard 的指标，以及 JSONL 记录里与之一一对应的键。
_TB_KEYS = ("loss", "grad_norm", "lr", "update_s")


def _metrics_path() -> Path | None:
    """指标文件位置，必须与后端读取约定一致。

    后端（services/metrics.py）读的是 ``<PLATFORM_CHECKPOINT_HOST_PATH>/<任务名>/metrics.jsonl``，
    而平台注入的 ``CHECKPOINT_DIR`` **本身就是** ``/checkpoints/<任务名>``。
    这里再拼一次 JOB_NAME 就会变成 ``/checkpoints/<任务名>/<任务名>/metrics.jsonl``，
    后端自然读不到——这是 loss 曲线一直为空的真正原因，排查了三轮。
    """
    checkpoint_dir = os.environ.get("CHECKPOINT_DIR")
    if checkpoint_dir:
        return Path(checkpoint_dir) / METRICS_FILENAME
    # 没注入 CHECKPOINT_DIR 时（本地手动运行）才用 JOB_NAME 兜底
    job = os.environ.get("JOB_NAME")
    if not job:
        return None
    return Path("/checkpoints") / job / METRICS_FILENAME


def _tensorboard_dir(metrics_target: Path) -> Path:
    """events 目录：与 metrics.jsonl **同级**（``<CHECKPOINT_DIR>/tensorboard``）。

    故意不写成 ``metrics_target.parent / "run" / "tensorboard"``：那个位置是
    lerobot 的 output_dir，平台清理任务时会整个删掉，曲线会跟着丢。
    与 JSONL 放在同一层，两者同生共死，后端读 JSONL 时按同一前缀找 events 最省事。
    """
    return metrics_target.parent / TENSORBOARD_DIRNAME


def _open_summary_writer(directory: Path):
    """创建 SummaryWriter，失败返回 None。

    这个函数只在 install 里调用**一次**：SummaryWriter 自带后台线程、每个 writer
    会各自开一个 events 文件，每步新建的话既泄线程又把目录刷满小文件
    （几千个 events 文件，TensorBoard 打开要卡很久）。
    """
    try:
        from torch.utils.tensorboard import SummaryWriter
    except Exception as exc:  # noqa: BLE001 - 缺包/没装都算降级
        # 用 torch.utils.tensorboard 而不是直接 import tensorboard：前者是
        # torch 官方门面，内部会做版本兼容；它不可用时再试裸包。
        try:
            from tensorboardX import SummaryWriter  # type: ignore[no-redef]
        except Exception:  # noqa: BLE001
            print(
                f"[vla-platform] TensorBoard 不可用，只写 metrics.jsonl（训练不受影响）：{exc}",
                file=sys.stderr,
                flush=True,
            )
            return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # max_queue=1 + flush_secs 小：训练进程随时可能被平台 kill，
        # 攒在内存里没落盘的曲线就永久丢了。
        return SummaryWriter(log_dir=str(directory), max_queue=1, flush_secs=5)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[vla-platform] TensorBoard 初始化失败，只写 metrics.jsonl（训练不受影响）：{exc}",
            file=sys.stderr,
            flush=True,
        )
        return None


def install(output_dir: Path | None = None) -> bool:
    """安装 hook。返回是否成功——失败时训练照常进行，只是没有曲线。"""
    target = output_dir / METRICS_FILENAME if output_dir else _metrics_path()
    if target is None:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    # MetricsTracker 的实际位置在不同版本里变过：0.4.4 在
    # lerobot.utils.logging_utils，早期版本在 train_utils。
    # 逐个尝试比写死一个路径可靠——写死的结果就是 hook 静默失效、
    # 平台上一条曲线都没有，而且没有任何报错提示（踩过）。
    MetricsTracker = None
    for module_path in (
        "lerobot.utils.logging_utils",
        "lerobot.utils.train_utils",
        "lerobot.utils.metrics",
    ):
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            continue
        candidate = getattr(module, "MetricsTracker", None)
        if candidate is not None:
            MetricsTracker = candidate
            break
    if MetricsTracker is None:
        print(
            "[vla-platform] 未找到 MetricsTracker，训练指标不会写入文件"
            "（训练本身不受影响）",
            file=sys.stderr,
            flush=True,
        )
        return False

    # 同一个进程里重复安装会套娃，标记一下
    if getattr(MetricsTracker, "_vla_platform_hooked", False):
        return True

    original_init = MetricsTracker.__init__
    original_setattr = MetricsTracker.__setattr__

    # 只建一次。放在 install 里而不是 patched_init 里：MetricsTracker 在某些
    # lerobot 路径下会被构造多次（eval 一套、train 一套），每次 init 都新建 writer
    # 就会出现多个 events 文件，TensorBoard 里同一张图冒出好几条断线。
    # 失败时 writer 是 None，后面所有写入都跳过——不影响 metrics.jsonl，也不影响训练。
    writer = _open_summary_writer(_tensorboard_dir(target))

    def patched_init(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        original_init(self, *args, **kwargs)
        object.__setattr__(self, "_vla_path", target)
        object.__setattr__(self, "_vla_buffer", {})

    def patched_setattr(self, name, value):  # noqa: ANN001, ANN002
        # 注意：MetricsTracker 自己的 __setattr__ 对 metrics 字典里的键走的是
        # ``self.metrics[name].update(value)``，**不会**调用父类的 __setattr__。
        # 所以这里不能依赖「先调 original 再读属性」，必须在调用前自己记下 value。
        # 之前就是这么写错的：hook 装好了、日志也打印了，但一条记录都没写出来。
        if name in ("loss", "grad_norm", "lr", "update_s"):
            buffer = self.__dict__.get("_vla_buffer")
            if buffer is not None:
                buffer[name] = float(value) if isinstance(value, (int, float)) else value
        original_setattr(self, name, value)
        if name not in ("loss", "grad_norm", "lr", "update_s"):
            return
        buffer = self.__dict__.get("_vla_buffer")
        if buffer is None:
            return
        # loss 是每个训练步都会更新的最后一项，用它触发落盘
        if name != "loss":
            return

        # step 偏移：续训时训练是从 checkpoint 的步数往下走的，
        # 但 hook 是从 1 开始自增。不加偏移的话，续训任务在曲线图上
        # 会从 0 重新起一条线，看起来像"又从第一步开始了"。
        offset = int(os.environ.get("VLA_STEP_OFFSET") or 0)
        # _vla_step 必须存**相对计数**，不能存带偏移的值。
        # 之前存了带偏移的，下一步又加一次偏移，于是步数指数增长
        # （实测出现 step 150050），曲线彻底成了乱的。
        local_step = int(getattr(self, "_vla_step", 0)) + 1
        step = offset + local_step
        record = {"step": step}
        record.update(buffer)
        object.__setattr__(self, "_vla_step", local_step)

        # TensorBoard 先写：它内部是队列 + 后台线程，失败也只影响自己。
        # 放在 JSONL 之前是为了让两者的 flush 顺序与 wall_time 一致，
        # 不会出现 TensorBoard 比 JSONL 少最后几个点的情况。
        _write_tensorboard(writer, step, buffer)

        path = self.__dict__.get("_vla_path")
        if path is None:
            return
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
                handle.flush()
        except OSError as exc:
            # 以前这里是静默 pass：路径错、权限不足、磁盘满都会让曲线悄无声息地
            # 永远是空的，排查了三轮才找到。宁可打日志打扰，也不要静默失败。
            print(f"[vla-platform] 写指标失败: {exc}", file=sys.stderr, flush=True)

    MetricsTracker.__init__ = patched_init
    MetricsTracker.__setattr__ = patched_setattr
    MetricsTracker._vla_platform_hooked = True
    # 挂到模块级，进程退出时由 atexit 统一关闭；只留局部变量的话，
    # 平台 SIGTERM 训练进程时最后几个点会丢在 writer 的缓冲里。
    if writer is not None:
        _WRITERS.append(writer)
    print(
        f"[vla-platform] 训练指标将写入 {target}"
        + (f" 与 {_tensorboard_dir(target)}" if writer is not None else "（TensorBoard 未启用）"),
        file=sys.stderr,
        flush=True,
    )
    return True


def _write_tensorboard(writer, step: int, buffer: dict) -> None:  # noqa: ANN001
    """把一步的指标写进 TensorBoard。**任何异常都吞掉**。

    和 JSONL 那边的处理刻意不同：JSONL 是平台画曲线的唯一数据源，写失败必须
    报出来；TensorBoard 只是额外产物，没装、写不了都不该让训练停，
    更不该因为它把 JSONL 的写入带崩——所以这里单独 try，且不重抛。
    """
    if writer is None:
        return
    try:
        for key in _TB_KEYS:
            value = buffer.get(key)
            if isinstance(value, (int, float)):
                writer.add_scalar(key, float(value), step)
    except Exception as exc:  # noqa: BLE001 - 绝不能影响训练
        # 失败只提示一次，避免每步刷屏把训练日志淹掉
        if not getattr(writer, "_vla_warned", False):
            try:
                writer._vla_warned = True
            except Exception:  # noqa: BLE001
                pass
            print(
                f"[vla-platform] 写 TensorBoard 失败（已忽略，训练继续）：{exc}",
                file=sys.stderr,
                flush=True,
            )


def _close_writers() -> None:
    """进程退出时关闭 writer。SummaryWriter.close 会 flush 并关后台线程。"""
    while _WRITERS:
        writer = _WRITERS.pop()
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


# 已创建的 writer（正常只有一个）。用 list 存是为了重复 install 时旧 writer 也能被关闭。
_WRITERS: list = []
atexit.register(_close_writers)
 
