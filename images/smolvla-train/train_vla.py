#!/usr/bin/env python3
"""SmolVLA 微调入口。

为什么自己写入口而不直接调 lerobot CLI：
  1. 平台约定用 DATASET_ROOT 传数据集、/checkpoints/<任务名>/ 存产物，
     lerobot 没有这套约定。
  2. 需要在调用前做一次环境自检——训练跑了半小时才因为没有 CUDA 失败，
     比一开始就失败糟糕得多。
  3. 需要把失败原因翻译成人话。lerobot 的 traceback 动辄上百行，
     算法工程师看到「Could not load libtorchcodec」会以为是缺 ffmpeg。

参数名与 recipes/smolvla-finetune.yaml 一一对应，改这里要同步改配方。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# 预训练基座。lerobot-train 会同时加载它和它 config 里引用的 VLM，
# 所以离线运行时两者都必须已在 HF 缓存里。
DEFAULT_POLICY = "lerobot/smolvla_base"
# 本地数据集的占位标识（draccus 要求必填，实际路径由 dataset.root 给出）
DATASET_REPO_ID = "local/dataset"
# 训练产物的 Hub 仓库占位标识（产物实际落在 /checkpoints/<任务名>/）
POLICY_REPO_ID = "local/smolvla-finetuned"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SmolVLA 微调（平台封装版）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # ---- 与配方参数一一对应（下划线风格，与 lerobot 0.4.4 一致）----
    parser.add_argument("--steps", type=int, default=2000, help="总优化步数")
    parser.add_argument("--batch_size", type=int, default=16, help="每卡 batch size")
    parser.add_argument(
        "--gradient_accumulation_steps",
        dest="gradient_accumulation_steps",
        type=int,
        default=1,
        help="梯度累积步数",
    )
    parser.add_argument("--save_freq", type=int, default=500, help="保存 checkpoint 的间隔（步）")
    parser.add_argument("--log_freq", type=int, default=10, help="打印日志的间隔（步）")
    parser.add_argument("--num_workers", type=int, default=8, help="dataloader 进程数")
    parser.add_argument("--policy.optimizer_lr", dest="lr", type=float, default=1e-4, help="学习率")
    parser.add_argument("--policy.device", dest="device", default="cuda", help="训练设备")
    parser.add_argument("--policy.path", dest="policy_path", default=DEFAULT_POLICY, help="基座权重")
    parser.add_argument("--resume", action="store_true", help="从最新 checkpoint 续训")
    # ---- 平台注入 ----
    parser.add_argument("--dataset_root", default=os.environ.get("DATASET_ROOT", ""), help="数据集根目录")
    parser.add_argument("--output_dir", default="", help="产物目录，默认 /checkpoints/<任务名>")
    parser.add_argument("--smoke", action="store_true", help="只做环境自检，不训练")
    parser.add_argument("--dry-run", action="store_true", help="打印将要执行的命令但不运行")
    return parser


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    """确定数据集目录与产物目录，缺失时给出可操作的报错。"""
    if not args.dataset_root:
        raise SystemExit(
            "未指定数据集。平台会通过环境变量 DATASET_ROOT 注入，"
            "手动运行时请加 --dataset_root /path/to/dataset"
        )
    dataset_root = Path(args.dataset_root)
    if not dataset_root.is_dir():
        raise SystemExit(f"数据集目录不存在：{dataset_root}")

    # LeRobot 要的是包含 meta/ 的目录；用户可能传了上层目录
    if not (dataset_root / "meta" / "info.json").is_file():
        candidates = [
            p for p in dataset_root.iterdir()
            if p.is_dir() and (p / "meta" / "info.json").is_file()
        ]
        if len(candidates) == 1:
            dataset_root = candidates[0]
        else:
            raise SystemExit(
                f"{dataset_root} 下没有 meta/info.json，不是合法的 LeRobot 数据集。"
                "请确认 meta/ 与 data/ 在同一层。"
            )

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        # 平台注入的 CHECKPOINT_DIR **已经是** /checkpoints/<任务名>。
        # 早先这里又拼了一次 JOB_NAME，产物落进 /checkpoints/<任务名>/<任务名>/，
        # 而后端读的是 checkpoint_root/<任务名>/metrics.jsonl——多出的那一层
        # 让 loss 曲线永远是空的。只有 CHECKPOINT_DIR 缺失时才回退到 JOB_NAME。
        checkpoint_dir = os.environ.get("CHECKPOINT_DIR")
        if checkpoint_dir:
            # 产物根是 CHECKPOINT_DIR。但 lerobot 要求它的 output_dir **完全不存在**
            # （空目录也算已存在，会直接 FileExistsError），而平台预创建了这个根目录，
            # 所以训练产物写进它的子目录 run/，根目录留给平台的 metrics.jsonl。
            output_dir = Path(checkpoint_dir) / "run"
        else:
            job = os.environ.get("JOB_NAME") or "local"
            output_dir = Path("/checkpoints") / job / "run"
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    return dataset_root, output_dir


def check_environment(dataset_root: Path, output_dir: Path) -> dict:
    """环境自检：torch/CUDA、lerobot、数据集可读、产物目录可写。"""
    report: dict = {}

    import torch

    report["torch"] = torch.__version__
    report["cuda_available"] = torch.cuda.is_available()
    report["cuda_version"] = torch.version.cuda
    if torch.cuda.is_available():
        report["gpu"] = torch.cuda.get_device_name(0)
        report["vram_gb"] = round(torch.cuda.get_device_properties(0).total_memory / 1024**3, 1)

    try:
        import lerobot

        report["lerobot"] = getattr(lerobot, "__version__", "unknown")
    except Exception as exc:  # noqa: BLE001 - 自检要报告而不是抛
        report["lerobot"] = f"import 失败：{exc}"

    info = json.loads((dataset_root / "meta" / "info.json").read_text(encoding="utf-8"))
    report["dataset"] = str(dataset_root)
    report["episodes"] = info.get("total_episodes")
    report["frames"] = info.get("total_frames")
    report["fps"] = info.get("fps")
    report["codebase_version"] = info.get("codebase_version")

    # 产物目录可写性：不可写时训练跑完才发现，白跑一次。
    # 探针写在父目录里——不能写进 output_dir 本身，那会让 lerobot 认为
    # 目录已被占用（它要求输出目录不存在，除非 resume）。
    probe = output_dir.parent / ".write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report["output_writable"] = True
    except OSError as exc:
        report["output_writable"] = f"不可写：{exc}"
    return report


def build_train_command(
    args: argparse.Namespace,
    dataset_root: Path,
    output_dir: Path,
) -> list[str]:
    """拼装 lerobot-train 命令。

    两个必须显式指定的参数：
      * --dataset.video_backend=pyav：torchcodec 在 torch 2.6 上加载失败（镜像里已
        通过 sitecustomize 屏蔽），显式指定更保险，也避免每次启动打警告。
      * --output_dir：平台约定产物落在 /checkpoints/<任务名>/，否则会写进镜像里的
        临时目录，任务结束就丢了。
    """
    # dataset.repo_id 是 draccus 的必填项。用本地数据集时它只是个标识，
    # 不参与下载——真正的数据由 dataset.root 指定。但漏了它会直接
    # DecodingError: Missing required field(s) repo_id，且报错完全看不出该填什么。
    command = [
        "lerobot-train",
        f"--policy.path={args.policy_path}",
        # policy.repo_id / dataset.repo_id 都是 draccus 的必填项：
        # 前者是「训练完把模型推到 Hub 的哪个仓库」，后者是数据集标识。
        # 我们产物落在本地 checkpoint 目录，不需要推 Hub，但要填一个占位值，
        # 否则 lerobot 在 validate() 阶段直接 ValueError，且报错只说缺参数，
        # 不告诉你该填什么。
        f"--policy.repo_id={POLICY_REPO_ID}",
        # 关掉训练结束时自动推送到 Hub。产物已经在 /checkpoints/<任务名>/ 下，
        # 内网环境通常没有 HF token，不关会在训练**成功之后**抛 401，
        # 让一个跑完的任务看起来像失败——这是最糟糕的失败形态。
        "--policy.push_to_hub=false",
        f"--dataset.repo_id={DATASET_REPO_ID}",
        f"--dataset.root={dataset_root}",
        "--dataset.video_backend=pyav",
        f"--output_dir={output_dir}",
        f"--batch_size={args.batch_size}",
        f"--steps={args.steps}",
        f"--save_freq={args.save_freq}",
        f"--log_freq={args.log_freq}",
        f"--num_workers={args.num_workers}",
        f"--policy.optimizer_lr={args.lr}",
        f"--policy.device={args.device}",
    ]
    # 梯度累积是可选参数：lerobot 不同小版本的 flag 名不同，
    # 传了不支持的会被当未知参数报错，所以只在大于 1 时尝试。
    if args.gradient_accumulation_steps and args.gradient_accumulation_steps > 1:
        command.append(f"--policy.gradient_accumulation_steps={args.gradient_accumulation_steps}")
    if not args.resume and output_dir.exists():
        # lerobot 要求 output_dir 不存在或为空（除非 resume）。
        # 平台预创建了这个目录作为任务产物根，里面可能已经有 metrics.jsonl
        # （指标 hook 会往里写），所以不能简单删掉——把已有内容挪到 .attempt-N，
        # 既不丢指标文件也不阻塞重跑。
        index = 1
        while (output_dir.parent / f".attempt-{index}").exists():
            index += 1
        legacy = output_dir.parent / f".attempt-{index}"
        output_dir.rename(legacy)
        print(f"输出目录已存在，历史内容移动到 {legacy.name}", flush=True)

    command.append(f"--resume={'true' if args.resume else 'false'}")
    return command


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset_root, output_dir = resolve_paths(args)
    report = check_environment(dataset_root, output_dir)
    print("环境自检：", json.dumps(report, ensure_ascii=False, indent=2), flush=True)

    if args.smoke:
        print("自检完成（--smoke 模式，不进入训练）")
        return 0 if report.get("cuda_available") else 1

    if not report.get("cuda_available"):
        print("错误：CUDA 不可用，无法训练。请确认 GPU 已挂载到容器。", file=sys.stderr)
        return 1

    # 注意：这里**不能**安装指标 hook。训练是 subprocess 起的独立 Python 进程，
    # 在父进程里 monkey-patch 不会被子进程继承——之前就是在父进程装、日志也打印了，
    # 但训练进程里根本没有 hook，曲线始终为空，排查了很多轮。
    # 真正的安装点在 metrics_boot/sitecustomize.py，它由 PYTHONPATH 注入，
    # 任何 Python 进程启动时都会执行。

    command = build_train_command(args, dataset_root, output_dir)
    print("即将执行：", " ".join(command), flush=True)
    if args.dry_run:
        return 0

    # 用子进程而不是 exec：指标 hook 需要在训练进程内生效，
    # exec 会换掉当前进程，patch 就白做了。
    # 取消任务时平台会 SIGTERM 整个 Pod，子进程会一并收到。
    import subprocess

    # 同目录下的 metrics_boot/sitecustomize.py 会被 Python 自动导入，
    # 在那里安装 hook 才能作用于训练进程本身。
    env = dict(os.environ)
    boot_dir = Path(__file__).resolve().parent / "metrics_boot"
    if boot_dir.is_dir():
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{boot_dir}{os.pathsep}{existing}" if existing else str(boot_dir)

    child = subprocess.Popen(command, env=env)
    child.wait()
    return child.returncode


if __name__ == "__main__":
    sys.exit(main())
