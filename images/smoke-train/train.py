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


# torchrun 在单机多卡时注入的 rendezvous 变量。多机模式下这几个变量由
# PyTorchJob/Gang 端口 + MASTER_ADDR/MASTER_PORT/WORLD_SIZE 手工提供，
# 因此不能再用它们判断「是不是 torchrun 环境」。
TORCHELASTIC_VARIABLES = (
    "TORCHELASTIC_RUN_ID",
    "TORCHELASTIC_MAX_RESTARTS",
    "TORCHELASTIC_RESTART_COUNT",
    "TORCHELASTIC_USE_AGENT_STORE",
)


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


def env_int(name, default):
    raw = os.environ.get(name, "")
    if raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"环境变量 {name} 必须是整数，当前值: {raw!r}") from None


def hostname():
    return socket.gethostname()


def node_ip():
    """尽力解析本 Pod 在集群内的 IP，用于跨节点 NCCL 选用正确网卡。"""
    if os.environ.get("POD_IP"):
        return os.environ["POD_IP"]
    try:
        addresses = socket.getaddrinfo(hostname(), None, socket.AF_INET)
    except OSError:
        return ""
    for address in addresses:
        candidate = address[4][0]
        if not candidate.startswith("127."):
            return candidate
    return ""


def nvidia_driver_version():
    if not shutil.which("nvidia-smi"):
        return ""
    try:
        driver = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"nvidia-smi driver query failed: {exc}")
        return ""
    return driver[0].strip() if driver else ""


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
    driver = nvidia_driver_version()
    if driver:
        log(f"NVIDIA driver version: {driver}")


def torch_cuda_build():
    """从 torch.version.cuda 推断镜像内链接的 NCCL/CUDA 大版本，便于跨节点比对。"""
    build = torch.version.cuda
    if not build:
        return "cpu"
    return "cu" + build.split(".")[0]


def nccl_version():
    try:
        if hasattr(torch.cuda, "nccl") and hasattr(torch.cuda.nccl, "version"):
            info = torch.cuda.nccl.version()
            if isinstance(info, tuple):
                return ".".join(str(part) for part in info)
            return str(info)
    except (AttributeError, RuntimeError):
        pass
    library = Path("/usr/local/cuda") / "include" / "nccl.h"
    if library.is_file():
        try:
            for line in library.read_text(encoding="utf-8", errors="replace").splitlines():
                if "NCCL_MAJOR" in line or "NCCL_MINOR" in line:
                    log(f"nccl.h: {line.strip()}")
        except OSError:
            pass
    return ""


def nccl_environment():
    """打印影响跨节点 NCCL 的关键环境变量，排查多机问题时最先看这些。"""
    keys = (
        "NCCL_DEBUG",
        "NCCL_DEBUG_SUBSYS",
        "NCCL_SOCKET_IFNAME",
        "NCCL_IB_DISABLE",
        "NCCL_P2P_DISABLE",
        "NCCL_IB_HCA",
        "GLOO_SOCKET_IFNAME",
        "PET_MASTER_ADDR",
        "NVIDIA_VISIBLE_DEVICES",
    )
    values = {key: os.environ[key] for key in keys if os.environ.get(key)}
    log("NCCL_ENV " + json.dumps(values, ensure_ascii=False, sort_keys=True))
    return values


def describe_nccl_communicator(backend):
    """返回 (nccl 版本, 实际使用的 IB/网络插件)。无法查询时返回空串，不报错。"""
    if backend != "nccl":
        return "", ""
    version = nccl_version()
    plugin = ""
    try:
        import torch.distributed as _dist

        if hasattr(_dist, "get_comm_config") and _dist.is_initialized():
            config = _dist.get_comm_config()
            plugin = json.dumps(config, ensure_ascii=False, sort_keys=True)
    except (AttributeError, RuntimeError, TypeError) as exc:
        log(f"NCCL comm config unavailable: {exc}")
    return version, plugin


def rendezvous_environment(distributed):
    """解析并校验 rendezvous。返回 dict，缺字段时直接失败。"""
    world_size = env_int("WORLD_SIZE", 1)
    rank = env_int("RANK", 0)
    local_rank = env_int("LOCAL_RANK", 0)
    local_world_size = env_int("LOCAL_WORLD_SIZE", world_size)
    master_addr = os.environ.get("MASTER_ADDR", "")
    master_port = os.environ.get("MASTER_PORT", "")
    return {
        "world_size": world_size,
        "rank": rank,
        "local_rank": local_rank,
        "local_world_size": local_world_size,
        "master_addr": master_addr,
        "master_port": master_port,
        "distributed": distributed,
    }


def validate_rendezvous(parser, args, rendezvous):
    world_size = rendezvous["world_size"]
    rank = rendezvous["rank"]
    local_rank = rendezvous["local_rank"]
    local_world_size = rendezvous["local_world_size"]
    master_addr = rendezvous["master_addr"]
    master_port = rendezvous["master_port"]

    if args.distributed and world_size <= 1:
        parser.error("--distributed 需要 WORLD_SIZE 大于 1")
    if world_size > 1 and not args.distributed:
        parser.error("检测到多进程环境（WORLD_SIZE > 1），请添加 --distributed")
    if not 0 <= rank < world_size:
        parser.error(f"RANK={rank} 必须落在 [0, WORLD_SIZE={world_size}) 内")
    if not 0 <= local_rank < local_world_size:
        parser.error(f"LOCAL_RANK={local_rank} 必须落在 [0, LOCAL_WORLD_SIZE={local_world_size}) 内")
    if world_size > local_world_size and not master_addr:
        parser.error("多节点训练必须设置 MASTER_ADDR（通常指向 PyTorchJob 的 master）")
    if world_size > local_world_size and not master_port:
        parser.error("多节点训练必须设置 MASTER_PORT（通常等于 PyTorchJob 的 rdzvPort）")
    if master_port and not master_port.isdigit():
        parser.error(f"MASTER_PORT 必须是数字，当前值: {master_port!r}")
    # 多机模式下 PyTorchJob 会显式传入 MASTER_*，直接按 env:// 初始化即可。
    if master_addr:
        os.environ["MASTER_ADDR"] = master_addr
    if master_port:
        os.environ["MASTER_PORT"] = master_port
    return local_world_size


def detect_launch_mode(rendezvous):
    world_size = rendezvous["world_size"]
    local_world_size = rendezvous["local_world_size"]
    if not rendezvous["distributed"]:
        return "single"
    if world_size > local_world_size:
        return "multi-node"
    if any(os.environ.get(name) for name in TORCHELASTIC_VARIABLES):
        return "single-node-torchrun"
    # 图省事的 torchrun --standalone 也会带 TORCHELASTIC_RUN_ID；没有它又只有
    # 一台机器，说明是手工导出的 env:// rendezvous，按单机多卡描述。
    return "single-node-env"


def log_rank_banner(rendezvous, mode, backend, args):
    """每个 rank 都打印一行，汇总多机排查所需的全部信息。"""
    rank = rendezvous["rank"]
    world_size = rendezvous["world_size"]
    node = hostname()
    payload = {
        "rank": rank,
        "world_size": world_size,
        "local_rank": rendezvous["local_rank"],
        "local_world_size": rendezvous["local_world_size"],
        "node": node,
        "node_ip": node_ip(),
        "master_addr": rendezvous["master_addr"] or "unset",
        "master_port": rendezvous["master_port"] or "unset",
        "backend": backend or "none",
        "torch": torch.__version__,
        "cuda_build": torch_cuda_build(),
        "mode": mode,
        "steps": args.steps,
    }
    if payload["local_rank"] == 0:
        # 每节点只打印一次设备信息，避免 8 卡时刷屏；rank 0 额外打印驱动版本。
        payload["gpu_count"] = torch.cuda.device_count() if torch.cuda.is_available() else 0
        if torch.cuda.is_available():
            properties = torch.cuda.get_device_properties(payload["local_rank"])
            payload["gpu_name"] = properties.name
        if rank == 0:
            payload["driver"] = nvidia_driver_version()
    log("RANK_INFO " + json.dumps(payload, ensure_ascii=False, sort_keys=True))


def nccl_diagnostics(rendezvous, backend, mode):
    """多机初始化后做一次往返通信，把耗时和 NCCL 版本打进日志。"""
    if backend != "nccl":
        log("NCCL_SKIP backend is not nccl (CPU/gloo smoke)")
        return
    world_size = rendezvous["world_size"]
    rank = rendezvous["rank"]
    device = torch.device("cuda", rendezvous["local_rank"])
    tensor = torch.tensor([float(rank)], device=device)
    torch.cuda.synchronize()
    started = time.monotonic()
    dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
    torch.cuda.synchronize()
    elapsed_ms = (time.monotonic() - started) * 1000
    expected = float(world_size * (world_size - 1) / 2)
    if tensor.item() != expected:
        raise RuntimeError(f"NCCL 往返校验失败: expected {expected}, got {tensor.item()}")
    version, plugin = describe_nccl_communicator(backend)
    log(
        "NCCL_ROUNDTRIP_OK "
        + json.dumps(
            {
                "rank": rank,
                "world_size": world_size,
                "mode": mode,
                "nccl_version": version or "unknown",
                "all_reduce_ms": round(elapsed_ms, 3),
                "comm_config": plugin,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def append_metrics(record):
    """把一步指标追加到 metrics.jsonl，供平台在网页上画 loss 曲线。

    平台约定：训练脚本把每步指标写到 $CHECKPOINT_DIR/metrics.jsonl，一行一个 JSON。
    写入用追加模式并立即 flush，这样任务运行中网页就能看到曲线。
    任何写失败都不应中断训练——指标只是观测手段。
    """
    try:
        path = checkpoint_directory() / "metrics.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
    except OSError as exc:
        log(f"WARN: cannot append metrics: {exc}")


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
    parser = argparse.ArgumentParser(description="Synthetic single-GPU/DDP/multi-node platform smoke training")
    parser.add_argument("--steps", type=positive_int, default=100, help="training steps (default: 100)")
    parser.add_argument("--sleep", type=nonnegative_float, default=0.1, help="seconds to sleep per step (default: 0.1)")
    parser.add_argument(
        "--distributed",
        action="store_true",
        help="initialize DDP from the env:// rendezvous (torchrun, PyTorchJob master, or manual RANK/WORLD_SIZE)",
    )
    args = parser.parse_args()

    rendezvous = rendezvous_environment(args.distributed)
    local_world_size = validate_rendezvous(parser, args, rendezvous)
    mode = detect_launch_mode(rendezvous)
    rank = rendezvous["rank"]
    world_size = rendezvous["world_size"]
    local_rank = rendezvous["local_rank"]

    log("VLA platform synthetic smoke training; this is not a real VLA model.")
    log(f"LAUNCH_MODE {mode} world_size={world_size} local_world_size={local_world_size}")
    if rank == 0:
        print_cuda_info()

    backend = None
    distributed = args.distributed
    if distributed:
        backend = "nccl" if torch.cuda.is_available() else "gloo"
        nccl_environment()
        log(f"Initializing distributed backend: {backend} via env://")
        if mode in ("multi-node", "single-node-env") and backend == "nccl":
            # 跨节点 NCCL 最怕网卡选错：未显式指定时给出建议值，而不是替用户改。
            if not os.environ.get("NCCL_SOCKET_IFNAME"):
                log(
                    "NCCL_HINT 未设置 NCCL_SOCKET_IFNAME；多节点 NCCL 常需要指定业务网卡"
                    "（例如 NCCL_SOCKET_IFNAME=eth0），否则可能选到 flannel/cni 网卡造成超时或性能骤降。"
                )
        dist.init_process_group(backend=backend, init_method="env://")
        # 结构化输出：便于 scripts/verify-multi-node.sh 用 grep 校验真正生效的 rendezvous。
        log(
            "RENDEZVOUS "
            + json.dumps(
                {
                    "init_method": "env://",
                    "master_addr": os.environ.get("MASTER_ADDR", ""),
                    "master_port": os.environ.get("MASTER_PORT", ""),
                    "rank": rank,
                    "world_size": world_size,
                    "local_rank": local_rank,
                    "local_world_size": local_world_size,
                    "backend": backend,
                    "mode": mode,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    try:
        if torch.cuda.is_available():
            torch.cuda.set_device(local_rank)
            device = torch.device("cuda", local_rank)
        else:
            device = torch.device("cpu")
        log(
            f"PARTICIPANT rank={rank}/{world_size - 1} local_rank={local_rank} "
            f"host={hostname()} device={device} backend={backend or 'none'}"
        )
        log_rank_banner(rendezvous, mode, backend, args)
        if distributed:
            nccl_diagnostics(rendezvous, backend, mode)

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
                current_loss = float(loss.item())
                log(f"step={step}/{args.steps} loss={current_loss:.6f} world_size={world_size}")
                append_metrics(
                    {
                        "step": step,
                        "loss": current_loss,
                        "lr": float(optimizer.param_groups[0]["lr"]),
                        "world_size": world_size,
                    }
                )
            if args.sleep:
                time.sleep(args.sleep)

        checkpoint = checkpoint_directory() / "smoke.pt"
        if rank == 0:
            save_checkpoint(checkpoint, model, optimizer, args.steps, world_size, backend)
        if distributed:
            dist.barrier()
        # 校验策略：
        #   - rank 0：始终校验自己写的文件；
        #   - 单机非 0 rank（含 torchrun 单机 8 卡）：共享同一个 /checkpoints，
        #     由 rank 0 负责写，因此 worker 只通过 barrier 等待；
        #   - 多节点：checkpoint 目录是各节点自己的 hostPath，worker 根本看不到
        #     rank 0 写的文件。此时只用 rank 0 校验，其余 rank 通过 barrier 保证
        #     「rank 0 写盘的顺序」，再配合 DDP_VERIFIED 里的 nodes 数验证拓扑。
        #     若把 checkpoint 换成 RWX 共享存储（NFS/CephFS），也可以显式导出
        #     VLA_SHARED_CHECKPOINT=1 让所有 rank 参与校验。
        shared_checkpoint = os.environ.get("VLA_SHARED_CHECKPOINT") == "1"
        # 只有两类进程需要校验：本进程负责写盘的 rank 0，或显式声明共享存储
        # 让所有 rank 都能看到该文件。其余 rank 只等 barrier。
        verify_here = (not distributed) or rank == 0 or shared_checkpoint
        if verify_here and (not checkpoint.is_file() or checkpoint.stat().st_size == 0):
            raise RuntimeError(f"checkpoint was not created: {checkpoint}")
        if not verify_here:
            log(f"checkpoint 由 rank 0 写入本节点 hostPath，worker 不做存在性校验：{checkpoint}")

        if distributed:
            participant = {
                "rank": rank,
                "host": hostname(),
                "host_ip": node_ip(),
                "device": str(device),
                "master_addr": rendezvous["master_addr"] or "unset",
                "checkpoint": str(checkpoint),
            }
            participants = [None] * world_size
            dist.all_gather_object(participants, participant)
            if rank == 0:
                ranks = sorted(item["rank"] for item in participants)
                if ranks != list(range(world_size)):
                    raise RuntimeError(f"DDP final rank verification failed: {ranks}")
                hosts = len({item["host"] for item in participants})
                log("DDP_PARTICIPANTS " + json.dumps(participants, ensure_ascii=False, sort_keys=True))
                log(
                    f"DDP_VERIFIED world_size={world_size} ranks={ranks} nodes={hosts} "
                    f"mode={mode} backend={backend}"
                )
                if mode == "multi-node" and hosts < 2:
                    raise RuntimeError(
                        f"多节点模式要求至少 2 个不同主机，实际仅 {hosts} 个；"
                        "PyTorchJob 的副本可能被调度到了同一节点"
                    )
        if rank == 0:
            log(f"SMOKE_COMPLETE steps={args.steps} checkpoint={checkpoint}")
    finally:
        if distributed and dist.is_initialized():
            dist.destroy_process_group()


if __name__ == "__main__":
    main()