"""checkpoint 保留策略。

实测每个 SmolVLA checkpoint 约 1.3 GB（模型 865MB + optimizer 412MB）。
按 save_freq=1000 推算，12 小时训练会写出约 40 个，合计 52 GB；
加上历史任务，十几小时的训练就能把磁盘打满。

但直接删旧的是错的：断点续训需要最近的那几个，最终产物需要最好的那个。
所以策略是固定的三种，都能说清楚为什么：

* ``keep_last_n``：只留最近 N 个。简单可预测，适合"只要最新模型"的场景。
* ``keep_best``：额外保住 loss 最低的那个。训练到最后 loss 未必最低，
  用户真正想要微调效果最好的那个权重。
* 不删 ``last`` 软链接指向的目录——那是续训的入口。

删除顺序刻意从旧到新，且**永远不动最新的一个**：
即使 N 填错成 0，也不会把刚训练出来的结果删掉。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

# lerobot 的 checkpoint 目录名是纯步数，例如 001000
STEP_DIR = re.compile(r"^\d+$")


def list_checkpoints(root: Path) -> list[tuple[int, Path]]:
    """列出训练产出的 checkpoint，按步数升序。

    只认纯数字目录：lerobot 在 checkpoints/ 下还有 ``last`` 之类的软链接，
    把它们当 checkpoint 计数会让保留策略算错。
    """
    if not root.is_dir():
        return []

    found: list[tuple[int, Path]] = []
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        if not STEP_DIR.match(entry.name):
            continue
        found.append((int(entry.name), entry))
    found.sort(key=lambda pair: pair[0])
    return found


def plan_retention(
    root: Path,
    *,
    keep_last_n: int,
    keep_best: bool = False,
    best_step: int | None = None,
) -> dict[str, Any]:
    """算出该删哪些 checkpoint。

    ``best_step`` 是 loss 最低的步数。为 None 而 keep_best 为 True 时，
    退化成"保住最新的那个"——没有依据就选最保险的。
    """
    checkpoints = list_checkpoints(root)
    if not checkpoints:
        return {"remove": [], "keep": [], "saved_bytes": 0}

    keep_steps: set[int] = set()

    # 最新的 N 个永远保留。N 至少为 1，防止配置写 0 把刚训出的结果删掉。
    n = max(1, int(keep_last_n))
    for step, _ in checkpoints[-n:]:
        keep_steps.add(step)

    if keep_best:
        if best_step is not None:
            keep_steps.add(best_step)
        else:
            keep_steps.add(checkpoints[-1][0])

    remove = [(step, path) for step, path in checkpoints if step not in keep_steps]
    keep = [(step, path) for step, path in checkpoints if step in keep_steps]

    return {
        "remove": [{"step": step, "path": str(path)} for step, path in remove],
        "keep": [{"step": step, "path": str(path)} for step, path in keep],
        "saved_bytes": sum(_dir_size(path) for _, path in remove),
    }


def apply_retention(root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """执行删除。单个失败不影响其余，最后汇总。"""
    removed: list[int] = []
    failed: list[dict[str, str]] = []

    for item in plan.get("remove", []):
        path = Path(item["path"])
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
                removed.append(int(item["step"]))
        except OSError as exc:
            failed.append({"step": str(item["step"]), "error": str(exc)})

    return {"removed": removed, "failed": failed}


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


class UnsafePath(ValueError):
    """目标路径不安全，拒绝删除。"""


def safe_job_dir(root: Path, name: str) -> Path:
    """把任务名解析成 root 下的绝对路径，越界就拒绝。

    我们真的因为一条 `rm -rf` 丢过全部训练产物：一个脚本里 `$JOB` 展开为空，
    `rm -rf "/checkpoints/checkpoints"` 在容器内以 root 跑掉了。
    所以这里不做"尽力而为"，而是任何可疑输入都直接拒绝：

    * 空值、带空白、含通配符或路径分隔符 → 拒绝
    * resolve() 之后必须在 root 之下 → 防 `..` 与软链接逃逸
    * 解析结果等于 root 本身 → 拒绝（那正是上次的事故）
    """
    if not name or not name.strip():
        raise UnsafePath("任务名不能为空")
    if name != name.strip():
        raise UnsafePath(f"任务名两端有空白：{name!r}")
    for bad in ("*", "?", "[", "]", "/", "\\"):
        if bad in name:
            raise UnsafePath(f"任务名不能包含 {bad!r}：{name!r}")

    root_resolved = root.resolve()
    target = (root_resolved / name).resolve()
    if target == root_resolved:
        raise UnsafePath(f"拒绝操作产物根目录本身：{target}")
    if not target.is_relative_to(root_resolved):
        raise UnsafePath(f"目标不在 {root_resolved} 之下：{target}")
    return target
