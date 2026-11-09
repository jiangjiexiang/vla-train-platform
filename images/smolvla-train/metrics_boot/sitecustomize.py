"""在任何 Python 进程启动时安装训练指标 hook。

为什么需要这一层：
    训练是 train_vla.py 用 subprocess 起的**独立进程**。在父进程里 monkey-patch
    MetricsTracker 不会被子进程继承——日志照常打印「指标将写入 ...」，
    但训练进程里根本没有 hook，曲线永远是空的。这个坑排查了很多轮。

为什么用 sitecustomize：
    Python 启动时会自动 import 名为 sitecustomize 的模块（只要它在 sys.path 上）。
    由 train_vla.py 把这个目录塞进子进程的 PYTHONPATH，训练进程一启动就会执行这里，
    不需要改 lerobot 源码，也不需要额外参数。

失败必须静默但可见：
    装不上只是没有曲线，不能影响训练。但如果完全不出声，就会出现「看起来在工作、
    实际没写任何东西」的情况——这正是之前反复踩的坑，所以失败时打印一行。
"""

import os
import sys


def _install() -> None:
    # 只在平台任务里启用：没有 JOB_NAME/CHECKPOINT_DIR 说明是普通 Python 调用
    # （比如用户自己跑脚本），不该被我们的 hook 干扰。
    if not os.environ.get("JOB_NAME") or not os.environ.get("CHECKPOINT_DIR"):
        return

    try:
        import metrics_hook
    except ImportError:
        # hooks 目录可能不在 sys.path（取决于启动方式），补一次
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            import metrics_hook
        except Exception:  # noqa: BLE001
            return

    try:
        metrics_hook.install()
    except Exception as exc:  # noqa: BLE001 - 绝不能让训练因指标失败
        print(f"[vla-platform] 指标 hook 安装失败（训练继续）：{exc}", file=sys.stderr)


_install()
