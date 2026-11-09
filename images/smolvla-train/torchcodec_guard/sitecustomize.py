"""让 lerobot 的视频后端自动回落到 pyav。

lerobot 0.4.4 钉死 torchcodec>=0.2.1,<0.11，而这个区间里每个版本都编死在
特定的 torch 次版本上；在 torch 2.6.0 + cu124 上实测 0.4/0.6/0.7/0.8/0.9/0.10
全部 dlopen 失败。更麻烦的是 lerobot 判断「后端是否可用」只看
importlib.util.find_spec("torchcodec")——能 import 到就算可用，于是训练跑到
采第一个 batch 才抛 RuntimeError: Could not load libtorchcodec。

这种报错会把人引向「是不是缺 ffmpeg」，排查成本极高。这里直接让 find_spec
对 torchcodec 返回 None，lerobot 就会打印一行明确的
"torchcodec is not available, falling back to 'pyav'" 并正常训练。

通过 PYTHONPATH 注入（而不是改 lerobot 源码），升级 lerobot 时不会冲突。
"""

import importlib.util as _util

_original_find_spec = _util.find_spec


def find_spec(name, *args, **kwargs):  # noqa: ANN001, ANN201 - 需与标准库签名一致
    if name == "torchcodec" or name.startswith("torchcodec."):
        return None
    return _original_find_spec(name, *args, **kwargs)


_util.find_spec = find_spec
