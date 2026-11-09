"""把 lerobot 的训练指标转写成平台的 metrics.jsonl。

lerobot 的 loss 只进 wandb 或终端进度条，不写任何文件。平台要画 loss 曲线就得
自己接。这里不 patch lerobot 源码，而是在进程内替换 MetricsTracker 的属性写入
——升级 lerobot 时不会冲突，找不到目标也只是少一条曲线，不影响训练。

为什么不用 logging hook：lerobot 只在 log_freq 命中的步才打日志，而且格式是
进度条，解析脆弱。MetricsTracker 每个训练步都会更新，是更稳定的接入点。
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

# 平台约定的指标文件位置（与 backend/app/services/metrics.py 一致）
METRICS_FILENAME = "metrics.jsonl"


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

        record = {"step": int(getattr(self, "_vla_step", 0)) + 1}
        record.update(buffer)
        object.__setattr__(self, "_vla_step", record["step"])
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
    print(f"[vla-platform] 训练指标将写入 {target}", file=sys.stderr, flush=True)
    return True
