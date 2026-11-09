#!/usr/bin/env python3
"""Small synthetic training job for platform validation, not a VLA model."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

import torch
from torch import nn
from torch.nn.parallel import DistributedDataParallel
import torch.distributed as dist


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def nonnegative_float(value):
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("不能小于 0")
    return parsed


def log(message):
    print(message, flush=True)


def checkpoint_directory():
    if os.environ.get("CHECKPOINT_DIR"):
        return Path(os.environ["CHECKPOINT_DIR"])
    job_name = (
        os.environ.get("JOB_NAME")
        or os.environ.get("VLA_JOB_NAME")
        or os.environ.get("PYTORCH_JOB_NAME")
        or "smoke-train"
    )
    return Path("/checkpoints") / job_name


def print_cuda_info():
    log(f"PyTorch version: {torch.__version__}")
    log(f"CUDA runtime available to PyTorch: {torch.version.cuda or 'none (CPU build)'}")
    log(f"CUDA available: {torch.cuda.is_available()}")
    if not torch.cuda.is_available():
        log("NVIDIA GPU not visible; running the same smoke workload on CPU.")
        return
    count = torch.cuda.device_count()
    log(f"NVIDIA GPU count: {count}")
    for index in range(count):
        properties = torch.cuda.get_device_properties(index)
        log(
            f"GPU {index}: {properties.name}; "
            f"compute capability {properties.major}.{properties.minor}; "
            f"memory {properties.total_memory / (1024 ** 3):.2f} GiB"
        )
    if shutil.which("nvidia-smi"):
        try:
            driver = subprocess.run(
                ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.splitlines()
            if driver:
                log(f"NVIDIA driver version: {driver[0]}")
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"nvidia-smi driver query failed: {exc}")


def save_checkpoint(path, model, optimizer, step, world_size, backend):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    torch.save(
        {
            "smoke_test": True,
            "description": "Synthetic platform smoke workload; not a VLA model",
            "step": step,
            "world_size": world_size,
            "distributed_backend": backend,
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        },
        temporary,
    )
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description="Synthetic single-GPU/DDP platform smoke training")
    parser.add_argument("--steps", type=positive_int, default=100, help="training steps (default: 100)")
    parser.add_argument("--sleep", type=nonnegative_float, default=0.1, help="seconds to sleep per step (default: 0.1)")
    parser.add_argument("--distributed", action="store_true", help="initialize DDP from torchrun environment")
    args = parser.parse_args()

    world_size = int(os.environ.get("WORLD_SIZE", "1"))
    rank = int(os.environ.get("RANK", "0"))
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    if args.distributed and world_size <= 1:
        parser.error("--distributed 需要通过 torchrun 启动（WORLD_SIZE 必须大于 1）")
    if world_size > 1 and not args.distributed:
        parser.error("检测到 torchrun 多进程环境，请添加 --distributed")

    log("VLA platform synthetic smoke training; this is not a real VLA model.")
    if rank == 0:
        print_cuda_info()

    backend = None
    distributed = args.distributed
    if distributed:
        backend = "nccl" if torch.cuda.is_available() else "gloo"
        log(f"Initializing distributed backend: {backend}")
        dist.init_process_group(backend=backend, init_method="env://")

    try:
        if torch.cuda.is_available():
            torch.cuda.set_device(local_rank)
            device = torch.device("cuda", local_rank)
        else:
            device = torch.device("cpu")
        log(
            f"PARTICIPANT rank={rank}/{world_size - 1} local_rank={local_rank} "
            f"host={socket.gethostname()} device={device} backend={backend or 'none'}"
        )

        torch.manual_seed(1234)
        model = nn.Sequential(nn.Linear(8, 16), nn.Tanh(), nn.Linear(16, 1)).to(device)
        if distributed:
            model = DistributedDataParallel(
                model,
                device_ids=[local_rank] if device.type == "cuda" else None,
            )
        optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
        loss_function = nn.MSELoss()
        expected_participation = world_size * (world_size + 1) / 2

        for step in range(1, args.steps + 1):
            model.train()
            features = torch.full((16, 8), float(rank + 1), device=device)
            features += torch.arange(8, dtype=features.dtype, device=device) * 0.01
            targets = torch.full((16, 1), float(rank + 1) * 0.1, device=device)
            optimizer.zero_grad(set_to_none=True)
            loss = loss_function(model(features), targets)
            loss.backward()
            optimizer.step()

            if distributed:
                participation = torch.tensor(float(rank + 1), device=device)
                dist.all_reduce(participation, op=dist.ReduceOp.SUM)
                actual = float(participation.item())
                if actual != expected_participation:
                    raise RuntimeError(
                        f"DDP participation mismatch at step {step}: "
                        f"expected {expected_participation}, got {actual}"
                    )
            if rank == 0:
                log(f"step={step}/{args.steps} loss={loss.item():.6f} world_size={world_size}")
            if args.sleep:
                time.sleep(args.sleep)

        checkpoint = checkpoint_directory() / "smoke.pt"
        if rank == 0:
            save_checkpoint(checkpoint, model, optimizer, args.steps, world_size, backend)
        if distributed:
            dist.barrier()
        if not checkpoint.is_file() or checkpoint.stat().st_size == 0:
            raise RuntimeError(f"checkpoint was not created: {checkpoint}")

        if distributed:
            participant = {
                "rank": rank,
                "host": socket.gethostname(),
                "device": str(device),
                "checkpoint": str(checkpoint),
            }
            participants = [None] * world_size
            dist.all_gather_object(participants, participant)
            if rank == 0:
                ranks = sorted(item["rank"] for item in participants)
                if ranks != list(range(world_size)):
                    raise RuntimeError(f"DDP final rank verification failed: {ranks}")
                log("DDP_PARTICIPANTS " + json.dumps(participants, ensure_ascii=False, sort_keys=True))
                log(f"DDP_VERIFIED world_size={world_size} ranks={ranks} backend={backend}")
        if rank == 0:
            log(f"SMOKE_COMPLETE steps={args.steps} checkpoint={checkpoint}")
    finally:
        if distributed and dist.is_initialized():
            dist.destroy_process_group()


if __name__ == "__main__":
    main()
