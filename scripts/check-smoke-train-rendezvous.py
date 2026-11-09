#!/usr/bin/env python3
"""用假 torch 驱动真实 train.py，验证多节点 rendezvous 逻辑。

不下载也不安装 PyTorch：本脚本在 sys.modules 里放一个最小 torch stub，
然后把 images/smoke-train/train.py 当成普通模块导入并调用 main()。
stub 会记录 init_process_group 收到的是 env:// 还是 file://，以此证明
多机路径确实走的是环境变量 rendezvous。

用法：
  python3 scripts/check-smoke-train-rendezvous.py            # 跑全部场景
  python3 scripts/check-smoke-train-rendezvous.py --verbose  # 同时打印子进程输出
"""

import argparse
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
TRAIN_PY = ROOT / "images" / "smoke-train" / "train.py"


class _NoGrad:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_torch_stub(record_path):
    """装一个只有接口、没有数值计算的 torch stub。"""

    torch = ModuleType("torch")
    nn = ModuleType("torch.nn")
    distributed = ModuleType("torch.distributed")
    parallel = ModuleType("torch.nn.parallel")
    cuda_nccl = ModuleType("torch.cuda.nccl")

    record = []

    def record_event(name, **payload):
        record.append({"event": name, **payload})

    class Tensor:
        def __init__(self, value=0.0):
            self.value = float(value)
            self.dtype = "float32"
            self.device = None

        def item(self):
            return self.value

        def __iadd__(self, other):
            return self

        def __add__(self, other):
            return self

        def __mul__(self, other):
            return self

        def backward(self):
            return None

    class Device:
        def __init__(self, type_, index=None):
            self.type = type_
            self.index = index

        def __str__(self):
            return self.type if self.index is None else f"{self.type}:{self.index}"

    class ReduceOp:
        SUM = "sum"

    class _DistributedModule(ModuleType):
        pass

    def is_initialized():
        return bool(record) and record[-1].get("event") == "init"

    def init_process_group(backend=None, init_method=None, **kwargs):
        record_event("init", backend=backend, init_method=init_method)

    def destroy_process_group():
        record_event("destroy")

    def barrier():
        record_event("barrier")

    def all_reduce(tensor, op=None):
        record_event("all_reduce", op=str(op))
        world_size = int(os.environ.get("WORLD_SIZE", "1"))
        rank = int(os.environ.get("RANK", "0"))
        # 两次 all_reduce 语义不同：NCCL 往返用 [rank]，参与度校验用 [rank+1]。
        if tensor.value == float(rank):
            tensor.value = float(world_size * (world_size - 1) / 2)
        else:
            tensor.value = float(world_size * (world_size + 1) / 2)

    def all_gather_object(output, payload):
        # 模拟真实集合通信：每个 rank 看到的 host 都来自各自节点。
        world_size = int(os.environ.get("WORLD_SIZE", "1"))
        node_key = os.environ.get("FAKE_NODES", "smoke-node-0,smoke-node-1,smoke-node-2,smoke-node-3")
        nodes = node_key.split(",")
        for index in range(world_size):
            item = dict(payload, rank=index)
            if len(nodes) > 1:
                item["host"] = nodes[index % len(nodes)]
            output[index] = item

    distributed.init_process_group = init_process_group
    distributed.destroy_process_group = destroy_process_group
    distributed.is_initialized = is_initialized
    distributed.barrier = barrier
    distributed.all_reduce = all_reduce
    distributed.all_gather_object = all_gather_object
    distributed.ReduceOp = ReduceOp

    cuda_nccl.version = lambda: (2, 21, 5)

    class _Cuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def device_count():
            return 1

        @staticmethod
        def set_device(index):
            record_event("set_device", index=index)

        @staticmethod
        def synchronize():
            return None

        @staticmethod
        def get_device_properties(index):
            class _Properties:
                name = "FAKE RTX 4090"
                major = 8
                minor = 9
                total_memory = 24 * 1024**3

            return _Properties()

        nccl = cuda_nccl

    torch.__version__ = "2.5.1-stub"
    torch.version = type("Version", (), {"cuda": "12.4"})()
    torch.cuda = _Cuda
    torch.device = Device
    torch.distributed = distributed
    torch.manual_seed = lambda seed: None
    torch.tensor = lambda value, device=None: Tensor(value if not isinstance(value, list) else value[0])
    torch.arange = lambda count, dtype=None, device=None: Tensor(0.0)
    torch.full = lambda shape, value, device=None: Tensor(value)
    def _stub_save(payload, path):
        # 只有 rank 0 写盘；多机时 checkpoint 目录是共享卷，其余 rank 应能看到同一文件。
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stub-checkpoint", encoding="utf-8")

    torch.save = _stub_save
    torch.no_grad = lambda: _NoGrad()

    class Module:
        def __init__(self, *args, **kwargs):
            pass

        def to(self, device):
            return self

        def state_dict(self):
            return {}

        def parameters(self):
            return iter(())

        def train(self):
            return self

        def __call__(self, *args, **kwargs):
            return Tensor(0.0)

    class Sequential(Module):
        def forward(self, *args):
            return Tensor(0.0)

        def __call__(self, *args, **kwargs):
            return self.forward(*args, **kwargs)

    class Linear(Module):
        pass

    class Tanh(Module):
        pass

    class MSELoss(Module):
        def __call__(self, prediction, target):
            return Tensor(0.0)

    class Parameters:
        def __iter__(self):
            return iter(())

    class SGD:
        def __init__(self, params, lr=0.01):
            pass

        @staticmethod
        def zero_grad(set_to_none=True):
            return None

        @staticmethod
        def step():
            return None

        def state_dict(self):
            return {}

    class DistributedDataParallel(Module):
        pass

    nn.Sequential = Sequential
    nn.Linear = Linear
    nn.Tanh = Tanh
    nn.MSELoss = MSELoss
    torch.optim = type("Optim", (), {"SGD": SGD})()
    torch.nn = nn
    torch.dtype = str
    parallel.DistributedDataParallel = DistributedDataParallel
    nn.parallel = parallel

    # 真实 Pod 里 socket.gethostname() == $HOSTNAME；stub 保持一致，
    # 这样多机场景才能验证「至少 2 个不同主机」的断言。
    stub_socket = ModuleType("socket_stub")
    stub_socket.gethostname = lambda: os.environ.get("HOSTNAME") or "stub-host"
    stub_socket.getaddrinfo = lambda *args, **kwargs: []
    stub_socket.AF_INET = 2

    module = {
        "socket": stub_socket,
        "torch": torch,
        "torch.nn": nn,
        "torch.distributed": distributed,
        "torch.nn.parallel": parallel,
        "torch.cuda.nccl": cuda_nccl,
    }
    sys.modules.update(module)
    record_path.write_text("", encoding="utf-8")
    record_event("stub_installed")


def purge_bytecode_cache():
    """删掉 train.py 旁边的 __pycache__。

    py_compile 与 importlib 都会写 .pyc；当源文件 mtime 与 .pyc 记录一致时
    (例如同一秒内连续修改)，import 会加载陈旧字节码，导致「改了源码仍在跑旧逻辑」。
    这里在加载前直接清掉，保证一定读源码。
    """
    cache = TRAIN_PY.parent / "__pycache__"
    if cache.is_dir():
        for stale in cache.glob("train.*.pyc"):
            try:
                stale.unlink()
            except OSError:
                pass


def load_train_module():
    """按源码加载 train.py，并禁用字节码缓存。

    禁用缓存的原因：本脚本经常和 `py_compile`/`verify-multi-node.sh` 交替运行，
    __pycache__ 里的 .pyc 与源码 mtime 相同时会被优先加载，导致「改了源码却
    还在跑旧逻辑」这种极难排查的假失败。
    """
    purge_bytecode_cache()
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("smoke_train_under_test", TRAIN_PY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
    return module


def run_case(record_path, argv):
    install_torch_stub(record_path)
    train_module = load_train_module()
    buffer = io.StringIO()
    exit_code = 0
    saved_argv = sys.argv
    sys.argv = ["train.py", *argv]
    try:
        with redirect_stdout(buffer):
            try:
                train_module.main()
            except SystemExit as exc:
                exit_code = int(exc.code or 0) if exc.code is not None else 0
    finally:
        sys.argv = saved_argv
    return exit_code, buffer.getvalue()


def parse_verified(payload):
    """DDP_VERIFIED 是 "world_size=2 ranks=[0, 1] nodes=2 mode=... backend=nccl" 纯文本。"""
    text = payload.get("value", "") if isinstance(payload, dict) else str(payload)
    result = {}
    for token in text.split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        if value.startswith("[") :
            continue
        try:
            result[key] = int(value)
        except ValueError:
            result[key] = value
    return result


def parse_json_line(output, prefix):
    """LAUNCH_MODE 是纯文本行（train.py 保持人读友好），其余是 JSON 行。"""
    for line in output.splitlines():
        if line.startswith(prefix + " "):
            remainder = line[len(prefix) + 1 :].strip()
            if prefix in ("LAUNCH_MODE", "RENDEZVOUS_HINT"):
                # 这一行是 "LAUNCH_MODE <mode> world_size=.. local_world_size=.."
                return {"value": remainder.split()[0]}
            try:
                return json.loads(remainder)
            except json.JSONDecodeError:
                return {"value": remainder}
    return None


def dispatch(record_path):
    """子进程入口：在干净解释器里跑一个场景，避免 stub 互相污染。"""
    exit_code, output = run_case(record_path, sys.argv[3:])
    payload = {
        "exit_code": exit_code,
        "output": output,
        "rendezvous": parse_json_line(output, "RENDEZVOUS"),
        "launch_mode": parse_json_line(output, "LAUNCH_MODE"),
        "rank_info": parse_json_line(output, "RANK_INFO"),
        "nccl": parse_json_line(output, "NCCL_ROUNDTRIP_OK"),
        "verified": parse_json_line(output, "DDP_VERIFIED"),
    }
    print(json.dumps(payload))
    sys.stdout.flush()
    return 0 if exit_code in (0, 2) else exit_code


def clean_environment():
    names = [
        "WORLD_SIZE",
        "RANK",
        "LOCAL_RANK",
        "LOCAL_WORLD_SIZE",
        "MASTER_ADDR",
        "MASTER_PORT",
        "TORCHELASTIC_RUN_ID",
        "NCCL_SOCKET_IFNAME",
        "POD_IP",
        "CHECKPOINT_DIR",
    ]
    return {key: value for key, value in os.environ.items() if key not in names}


def execute(scenario, verbose):
    environment = clean_environment()
    environment.update(scenario["env"])
    with tempfile.TemporaryDirectory() as directory:
        record_path = Path(directory) / "record.jsonl"
        # train.py 默认写 /checkpoints/<job>，这里指到临时目录，避免本机无权限。
        environment["CHECKPOINT_DIR"] = str(Path(directory) / "checkpoints")
        completed = subprocess.run(
            [sys.executable, __file__, "--dispatch", str(record_path), *scenario["args"]],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
    if completed.returncode != 0 or not completed.stdout.strip():
        return {
            "exit_code": completed.returncode,
            "output": "",
            "stderr": completed.stderr[-2000:],
        }
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    if verbose:
        payload["stderr"] = completed.stderr[-2000:]
    return payload


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--dispatch":
        return dispatch(Path(sys.argv[2]))

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", action="store_true")
    options = parser.parse_args()

    scenarios = [
        {
            "name": "multi-node rank0 (PyTorchJob master)",
            "env": {
                "WORLD_SIZE": "2",
                "RANK": "0",
                "LOCAL_RANK": "0",
                "LOCAL_WORLD_SIZE": "1",
                "MASTER_ADDR": "multi-node-smoke-master-0.multi-node-smoke-master",
                "MASTER_PORT": "23456",
                "HOSTNAME": "multi-node-smoke-master-0",
            },
            "args": ["--distributed", "--steps", "2", "--sleep", "0"],
            "expect_exit": 0,
            "expect": {"launch_mode": "multi-node", "init_method": "env://", "rank": 0, "distinct_nodes": 2},
        },
        {
            "name": "multi-node rank1 (PyTorchJob worker)",
            "env": {
                "WORLD_SIZE": "2",
                "RANK": "1",
                "LOCAL_RANK": "0",
                "LOCAL_WORLD_SIZE": "1",
                "MASTER_ADDR": "multi-node-smoke-master-0.multi-node-smoke-master",
                "MASTER_PORT": "23456",
                "HOSTNAME": "multi-node-smoke-worker-0",
            },
            "args": ["--distributed", "--steps", "2", "--sleep", "0"],
            "expect_exit": 0,
            "expect": {"launch_mode": "multi-node", "init_method": "env://", "rank": 1, "distinct_nodes": 2},
        },
        {
            "name": "multi-node missing MASTER_ADDR",
            "env": {
                "WORLD_SIZE": "2",
                "RANK": "0",
                "LOCAL_RANK": "0",
                "LOCAL_WORLD_SIZE": "1",
                "MASTER_PORT": "23456",
            },
            "args": ["--distributed", "--steps", "1"],
            "expect_exit": 2,
            "expect_stderr": "MASTER_ADDR",
        },
        {
            "name": "multi-node missing MASTER_PORT",
            "env": {
                "WORLD_SIZE": "2",
                "RANK": "1",
                "LOCAL_RANK": "0",
                "LOCAL_WORLD_SIZE": "1",
                "MASTER_ADDR": "master",
            },
            "args": ["--distributed", "--steps", "1"],
            "expect_exit": 2,
            "expect_stderr": "MASTER_PORT",
        },
        {
            "name": "torchrun single node 8 GPU (regression)",
            "env": {
                "WORLD_SIZE": "8",
                "RANK": "3",
                "LOCAL_RANK": "3",
                "LOCAL_WORLD_SIZE": "8",
                "TORCHELASTIC_RUN_ID": "standalone",
            },
            "args": ["--distributed", "--steps", "2", "--sleep", "0"],
            "expect_exit": 0,
            "expect": {"launch_mode": "single-node-torchrun", "init_method": "env://", "rank": 3},
        },
        {
            "name": "single GPU without --distributed",
            "env": {},
            "args": ["--steps", "2", "--sleep", "0"],
            "expect_exit": 0,
            "expect": {"launch_mode": "single", "init_method": None, "rank": 0},
        },
        {
            "name": "WORLD_SIZE>1 without --distributed must fail",
            "env": {"WORLD_SIZE": "2", "RANK": "0", "MASTER_ADDR": "m", "MASTER_PORT": "1"},
            "args": ["--steps", "1"],
            "expect_exit": 2,
            "expect_stderr": "WORLD_SIZE",
        },
        {
            "name": "--distributed with WORLD_SIZE=1 must fail",
            "env": {"WORLD_SIZE": "1", "RANK": "0"},
            "args": ["--distributed", "--steps", "1"],
            "expect_exit": 2,
            "expect_stderr": "WORLD_SIZE",
        },
    ]

    failures = []
    for scenario in scenarios:
        result = execute(scenario, options.verbose)
        details = []
        ok = True
        if result.get("exit_code") != scenario["expect_exit"]:
            ok = False
            details.append(f"exit_code={result.get('exit_code')} expected {scenario['expect_exit']}")
        expected = scenario.get("expect", {})
        if ok:
            launch = (result.get("launch_mode") or {}).get("value")
            rendezvous = result.get("rendezvous") or {}
            if "launch_mode" in expected and launch != expected["launch_mode"]:
                ok = False
                details.append(f"launch_mode={launch} expected {expected['launch_mode']}")
            if expected.get("init_method") is not None and rendezvous.get("init_method") != expected["init_method"]:
                ok = False
                details.append(f"init_method={rendezvous.get('init_method')} expected {expected['init_method']}")
            if "rank" in expected and (result.get("rank_info") or {}).get("rank") != expected["rank"]:
                ok = False
                details.append(f"rank_info.rank={(result.get('rank_info') or {}).get('rank')}")
            # DDP_VERIFIED 只有 rank 0 打印；其它 rank 的成功路径不做该项断言。
            is_rank0 = scenario["env"].get("RANK", "0") == "0"
            if expected.get("init_method") == "env://" and result.get("exit_code") == 0 and is_rank0:
                verified = parse_verified(result.get("verified") or {})
                if verified.get("world_size") != int(scenario["env"]["WORLD_SIZE"]):
                    ok = False
                    details.append(f"DDP_VERIFIED world_size={verified.get('world_size')}")
                if "distinct_nodes" in expected and verified.get("nodes") != expected["distinct_nodes"]:
                    ok = False
                    details.append(f"DDP_VERIFIED nodes={verified.get('nodes')} expected {expected['distinct_nodes']}")
                if not result.get("nccl"):
                    ok = False
                    details.append("NCCL_ROUNDTRIP_OK missing")
        if not ok:
            stderr = result.get("stderr") or ""
            if "expect_stderr" in scenario and scenario["expect_stderr"] not in stderr:
                details.append(f"stderr missing {scenario['expect_stderr']!r}")
        status = "OK  " if ok else "FAIL"
        print(f"[{status}] {scenario['name']}")
        for detail in details:
            print(f"       {detail}")
        if not ok:
            failures.append(scenario["name"])
            if options.verbose:
                print(result.get("output") or result.get("stderr", ""))

    if failures:
        print(f"\n{len(failures)} scenario(s) failed: {failures}", file=sys.stderr)
        return 1
    print(f"\nAll {len(scenarios)} rendezvous scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())