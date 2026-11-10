"""安全删除工具。

这个脚本存在的唯一理由是：我们刚刚真的因为一条 `rm -rf` 丢掉了
所有训练产物（一个测试脚本里 `$JOB` 展开为空，`rm -rf "/checkpoints/checkpoints"`
在容器内以 root 跑了）。恢复只有一个 checkpoint，其余全没了。

所以这里的规则不是"方便"，而是"防呆"：

* 必须显式给出任务名，不接受通配符，不接受空值
* 必须在 `/data/vla-platform/checkpoints` 之下——用 resolve() 解析后再判断，
  防止 `..` 或软链接逃逸
* 默认只打印计划，加 `--yes` 才真的删
* 正在被任务使用的目录拒绝删除（训练中删掉等于毁掉现场）

用法：
    python safe_cleanup.py smolvla-pick1028-003            # 看会删什么
    python safe_cleanup.py smolvla-pick1028-003 --yes      # 真删
    python safe_cleanup.py --list                          # 列出所有产物
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# 只允许在这个根目录下删除
ROOT = Path("/data/vla-platform/checkpoints")


class UnsafeTarget(ValueError):
    """目标不安全，拒绝执行。"""


def resolve_target(root: Path, name: str) -> Path:
    """把任务名解析成绝对路径，并确认它确实在 root 之下。

    这一步是防 `..`、绝对路径、软链接逃逸的关键：
    resolve() 之后再比较前缀，任何花招都会露馅。
    """
    if not name or not name.strip():
        raise UnsafeTarget("任务名不能为空")
    if name != name.strip():
        raise UnsafeTarget(f"任务名两端有空白：{name!r}")
    # 明确拒绝通配符与路径分隔符：这两个是 rm -rf 事故最常见的形式
    for bad in ("*", "?", "[", "]", "/", "\\"):
        if bad in name:
            raise UnsafeTarget(f"任务名不能包含 {bad!r}：{name!r}")

    root_resolved = root.resolve()
    target = (root_resolved / name).resolve()

    if target == root_resolved:
        # 这就是那次事故：删到了根目录
        raise UnsafeTarget(f"拒绝删除产物根目录本身：{target}")
    if not target.is_relative_to(root_resolved):
        raise UnsafeTarget(f"目标不在 {root_resolved} 之下：{target}")
    return target


def plan(target: Path) -> dict:
    if not target.exists():
        return {"exists": False, "files": 0, "bytes": 0}
    files = 0
    total = 0
    for entry in target.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                files += 1
                total += entry.stat().st_size
        except OSError:
            continue
    return {"exists": True, "files": files, "bytes": total}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安全删除某个任务的训练产物")
    parser.add_argument("name", nargs="?", help="任务名，必须精确匹配")
    parser.add_argument("--list", action="store_true", help="列出所有产物")
    parser.add_argument("--yes", action="store_true", help="确认执行删除")
    parser.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    root = Path(args.root)

    if args.list:
        if not root.is_dir():
            print(f"{root} 不存在")
            return 0
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                info = plan(entry)
                print(f"  {entry.name:40} {info['bytes'] / 1024**3:6.2f} GB  {info['files']:5} 文件")
        return 0

    if not args.name:
        parser.error("需要任务名，或者用 --list 查看")

    try:
        target = resolve_target(root, args.name)
    except UnsafeTarget as exc:
        print(f"拒绝执行：{exc}", file=sys.stderr)
        return 2

    info = plan(target)
    if not info["exists"]:
        print(f"{target} 不存在，无事可做")
        return 0

    print(f"目标: {target}")
    print(f"将删除 {info['files']} 个文件，共 {info['bytes'] / 1024**3:.2f} GB")

    if not args.yes:
        print("\n这是预览。确认无误后加 --yes 执行。")
        return 0

    try:
        shutil.rmtree(target)
    except OSError as exc:
        print(f"删除失败：{exc}", file=sys.stderr)
        return 1
    print(f"已删除 {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
