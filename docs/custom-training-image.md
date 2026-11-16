# 怎么做自己的训练镜像

本文讲**通用做法**：你的框架不是 SmolVLA（openpi / π0.5、GR00T、自己写的训练代码），
或者你需要特定版本的依赖、想把权重预置进镜像，都可以照这篇走一遍。

和 [`training-env-guide.md`](training-env-guide.md) 的关系：

| 文档 | 回答的问题 |
|---|---|
| **本文** | 平台**要求**镜像满足什么？做的时候**哪些地方一定会踩坑**？（通用，任何框架） |
| [`training-env-guide.md`](training-env-guide.md) | 数据集怎么放、任务怎么提交、怎么验收（平台使用流程） |
| [`smolvla-training-params.md`](smolvla-training-params.md) | SmolVLA 的具体参数取值与实测数据（特定框架） |

`images/smolvla-train/` 是**一个特定框架的完整参考实现**，本文会在对应位置引用它，
但会明确标出「哪些步骤是 SmolVLA 特有的、你的框架不需要照抄」。

> 约定：命令默认在平台节点的仓库根目录执行；`<...>` 表示需要替换的值；
> 全文只用 buildah + K3s 自带 containerd，**不要装 Docker**（理由见 [`operations.md`](operations.md) 第 6 节）。

---

## 1. 什么时候需要自己做镜像

| 情况 | 为什么要自己做 | 平台的现有镜像能用吗 |
|---|---|---|
| 换框架 | openpi（JAX）、GR00T（Isaac 系依赖）、自己写的 PyTorch 训练循环——依赖树和入口完全不同 | 不能。`vla-smolvla` 里是 lerobot 0.4.4，没有这些框架 |
| 需要特定版本的依赖 | 论文复现要钉死 torch / transformers 版本；框架对上界敏感 | 不能。平台镜像是给 SmolVLA 钉过版本的 |
| 需要预置模型权重 | 内网环境、hf-mirror 慢、每次任务重下几 GB | 能，但每个任务首启都要下载（见第 10 节） |
| 需要系统库 | 少见的解码器、编译依赖、私有 SDK | 不能，`python:3.11-slim` 里只有基础库 |
| 只是改训练参数 | 不需要 | **能，什么都不用做**——参数从命令行/配方传，见第 2.4 节 |

**判断标准**：如果只是「数据集换一个、步数换一下、batch 换一下」，不要做镜像，
直接用现有镜像 + 配方（`recipes/` 目录）提交即可。做镜像只为了**换环境**。

---

## 2. 平台对训练镜像的约定（核心）

这一节是全文最重要的一节。镜像做不出来的原因，八成不是框架装不上，而是漏了这里的某一条。

### 2.0 先看平台替你做了什么

平台创建 PyTorchJob 时固定做下面这些事（代码在 `backend/app/templates/pytorchjob.py`），
**你不需要自己写 YAML**：

| 平台自动处理的项 | 值 |
|---|---|
| 启动命令 | `["/bin/bash", "-lc", <配方 entrypoint 或你填的 command> + <参数模板拼出的 flag>]` |
| 数据集挂载 | 宿主机 `PLATFORM_DATASET_HOST_PATH` → `/datasets`，**只读** |
| 输出挂载 | 宿主机 `PLATFORM_CHECKPOINT_HOST_PATH` → `/checkpoints`，可写 |
| 单任务输出目录 | 环境变量 `CHECKPOINT_DIR=/checkpoints/<任务名>`（你在 `env` 里写了也会被覆盖） |
| 任务名 | 环境变量 `JOB_NAME=<任务名>`（指标文件路径要用它） |
| 共享内存 | `/dev/shm` 挂成 `emptyDir{medium: Memory}`，默认 `4Gi`（`PLATFORM_SHM_SIZE` 可改） |
| GPU | `runtimeClassName: nvidia`，`requests=limits=nvidia.com/gpu: <gpus>` |
| 拉取策略 | `imagePullPolicy: IfNotPresent` |
| 权限 | 起训练容器前先跑一个 `initContainer` 做 `chmod -R a+rwX /checkpoints`（见 2.5） |
| 排队 | Kueue 标签，LocalQueue `default`，优先级 `low`/`normal`/`high` |

由此得出一个重要结论：**平台的启动命令是固定的**——它一定是 `/bin/bash -lc "..."`。
所以镜像里**不一定需要 ENTRYPOINT**（见 2.4）。

### 2.1 数据集：`DATASET_ROOT` 环境变量

- 「一键训练」（网页配方 / `POST /api/v1/runs`）时，平台自动注入
  `DATASET_ROOT=/datasets/<数据集名>`（`backend/app/services/runs.py`）。
- 手工提交任务时平台**不会**自动注入，你要自己加 `--env DATASET_ROOT=/datasets/<数据集名>`。

训练脚本的读法：

```python
import os
from pathlib import Path

dataset_root = Path(os.environ.get("DATASET_ROOT") or "/datasets/<数据集名>")
```

两个配套要求：

1. **不要写死宿主机路径**。宿主机路径（`/data/vla-platform/datasets`）
   在容器里不存在，只有 `/datasets`。
2. **要能容忍传了上层目录**。用户传 `/datasets` 而不是 `/datasets/pick1028` 是常见失误，
   建议像 `images/smolvla-train/train_vla.py` 的 `resolve_paths()` 那样：
   没有 `meta/info.json` 时，往下找**唯一一个**含 `meta/info.json` 的子目录，超过一个就明确报错。

> 数据集是**只读**挂载的。往 `/datasets` 里写东西（比如 openpi 的 norm-stats 缓存、
> lerobot 的 `stats.json` 覆盖）一定失败。见第 9 节「只读文件系统」。

### 2.2 产物：写到 `CHECKPOINT_DIR`，也就是 `/checkpoints/<任务名>/`

- `CHECKPOINT_DIR` **本身就是** `/checkpoints/<任务名>`，**不要再拼一次 `JOB_NAME`**。
  拼错的表现是 `metrics.jsonl` 落在 `/checkpoints/<任务名>/<任务名>/`，曲线永远空白
  （`train_vla.py` 与 `metrics_hook.py` 的注释里各记录了一次，排查了三轮）。
- 目录由平台/initContainer 保证存在且可写，你直接往里写；`mkdir(parents=True, exist_ok=True)` 也无害。
- **产物根目录是平台和 Web 共用的**，所以：

| 该写 | 不该写 |
|---|---|
| `/checkpoints/<任务名>/run/`、`/checkpoints/<任务名>/checkpoints/` | 直接往 `/checkpoints/<任务名>/` 丢几十万个临时文件 |
| `/checkpoints/<任务名>/metrics.jsonl`（平台读这个） | 写到 `/app`、`/tmp`（任务结束就没了） |
| `/checkpoints/<任务名>/tensorboard/`（平台读这个） | 写到 `/datasets`（只读，必失败） |

Web 端的「产物列表」（`backend/app/services/checkpoints.py`）会扫描**整个任务目录的所有文件**，
最多列 200 项、按 mtime 倒序，`.pt/.pth/.safetensors/.ckpt/.bin/.onnx` 会被标成权重。
所以**不要**把训练中间缓存（torch 的 `~/.cache`、dataloader 临时文件）放进任务目录，
否则「产物」列表会被无关文件刷满。

### 2.3 指标：`metrics.jsonl`（可选，但强烈建议）

平台画 loss 曲线读的是 `<checkpoints 根>/<任务名>/metrics.jsonl`——
容器里就是 `/checkpoints/<任务名>/metrics.jsonl`（`backend/app/services/metrics.py`）。**每行一个 JSON 对象**：

```json
{"step": 100, "loss": 0.0234, "grad_norm": 1.82, "lr": 0.0001}
{"step": 110, "loss": 0.0207, "grad_norm": 1.51, "lr": 0.0001}
```

读取规则（写的时候照着来，就不会「文件有内容但曲线上什么都没有」）：

| 规则 | 说明 |
|---|---|
| `step` 必填且必须是**数字** | 没有 `step` 的行直接丢弃，不会报错，也不会提示——静默失效最坑 |
| 其他字段只保留**数值** | `bool`、字符串、嵌套对象都被丢弃；字段名建议用 `loss` / `grad_norm` / `lr` |
| 同一个 step 以**最后一行**为准 | 重跑某一步是安全的 |
| 按 step **升序**返回 | 追加写即可，不需要排序 |
| 只读文件**末尾 2 MiB** | 写得太碎（每步一行、几万步）时早期点会读不到；按 `log_freq` 写即可 |
| 超过 `limit`（Web 请求默认 500 点）时**只保留最后 N 个** | 长任务的早期曲线会被截掉，这是平台行为，不是你没写进去 |
| 只保留数值：`{"note": "x"}` 这类字段会被丢掉 | 想记文本，写在日志里 |
| 最后一行可能被截断 | 平台会跳过解析失败的行，所以你**每写完一行就 flush** 最安全 |

写入的最小实现（放训练循环里，别用日志解析）：

```python
import json, os
from pathlib import Path

METRICS = Path(os.environ.get("CHECKPOINT_DIR") or "/checkpoints/local") / "metrics.jsonl"
METRICS.parent.mkdir(parents=True, exist_ok=True)

def log_metrics(step: int, **values: float) -> None:
    record = {"step": int(step)}
    record.update({k: float(v) for k, v in values.items()})
    with open(METRICS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
        fh.flush()          # 显式 flush：训练被 SIGTERM 时最多丢一行
```

> **训练进程是被 `subprocess` 起的，不是你自己**：如果你在自己的入口里
> monkey-patch 训练库来拿指标，注意子进程里不会生效（`train_vla.py` 注释里记了这个坑）。
> 最稳的做法是训练循环里直接调用 `log_metrics()`。

**TensorBoard（可选）**：另外把 events 写到 `/checkpoints/<任务名>/tensorboard/`
（**必须公用 `$CHECKPOINT_DIR`**，用 `%tensorboard` 那个默认的 `runs/` 目录是无效的）：

```python
from torch.utils.tensorboard import SummaryWriter
writer = SummaryWriter(log_dir=str(METRICS.parent / "tensorboard"))
writer.add_scalar("loss", loss, step)     # 请在每步或每 log_freq 调用
```

平台直接解析 events 文件，不需要你起 `tensorboard` 服务。它只认这几个 tag：
`loss` / `grad_norm` / `lr` / `update_s`，以及 `train/` 前缀的同名版本
（`backend/app/services/tensorboard.py` 的 `DEFAULT_TAGS`）。
**注意**：`tensorboard` 包不随 torch 自动安装，要显式 `pip install tensorboard`
（`images/smolvla-train` 也单独装了它并做了构建期 import 自检），否则 `SummaryWriter` 一 import 就失败。

### 2.4 入口与命令行参数：ENTRYPOINT 是可选的

平台的启动方式决定了这件事：

```text
/bin/bash -lc "<基础命令> <参数模板拼出来的 flag...>"
```

- 「基础命令」的优先级是：**用户手填的 command > 配方里的 `entrypoint` > 平台默认 `python /app/train.py`**
  （`backend/app/services/params_render.py` 的 `resolve_base_command`）。
- 也就是说：**提交任务时你手填了 command，镜像里的 ENTRYPOINT 根本不会被执行**，
  参数模板拼出的 flag 会直接追加在你手填的 command 后面。
  同理，K8s 的 `command` 字段会覆盖 Docker 的 ENTRYPOINT——**别指望 ENTRYPOINT 做任何必要的事**。
- 所以 ENTRYPOINT 的用途只有两个：`docker run`/`podman run` 手动调试时省事，
  以及给配方里「不带参数」的场景一个默认行为。

两种都能用的写法：

```dockerfile
# 写法 A：ENTRYPOINT 转发所有参数（推荐，和命令行 flag 天然对齐）
ENTRYPOINT ["/app/entrypoint.sh"]
# entrypoint.sh 内部 exec python /app/train.py "$@"
```

```dockerfile
# 写法 B：不写 ENTRYPOINT，只保证 /app/train.py 存在且能跑
CMD ["python", "/app/train.py"]
```

**配方（`recipes/*.yaml`）里的 `entrypoint` 字段**要和镜像里的入口路径**一字不差**，
否则任务会以 `No such file or directory` 直接失败。例：
`recipes/smolvla-finetune.yaml` 写的是 `python /app/train_vla.py`，镜像里就得是 `/app/train_vla.py`。

**参数命名要跟「参数模板」一致**。参数由模板的 `flag` 字段拼出来（`examples/params/*.json`），
不同框架的 flag 风格不一样：

| 框架 | flag 风格 | 例 | 模板示例 |
|---|---|---|---|
| lerobot | 下划线 | `--batch_size` | `examples/params/lerobot-smolvla.json` |
| openpi / tyro | 中划线 + 点号 | `--num-train-steps`、`--config.name` | `examples/params/openpi-pi05.json` |
| Hydra / argparse | 中划线 | `--batch-size` | 自定义 |

参数模板的登记方法见 [`training-env-guide.md`](training-env-guide.md) 3.5.1 节。
**填错了不会报错**，只会被当成未知参数——交给框架自己拒绝（`train_vla.py` 的 README 里记过：
写中划线被 lerobot 当未知参数拒绝）。

### 2.5 容器以 root 运行：写出的文件属主是 root

**训练镜像默认没有固定用户**（Dockerfile 不写 `USER` 就是以 root 跑），这是有意的：

- 平台 Web 容器以 `uid=10001` 运行（仓库根 `Dockerfile` 的 `USER vla`），它要**读**训练产物、
  提供下载、清理旧 checkpoint；
- 训练容器如果也要求某个 uid，两边一改属主就坏一边。

现在平台的解法是在训练容器启动前跑一个 **initContainer**
（`pytorchjob.py` 里名字叫 `init-permissions`，**用的就是你提交的那个镜像**）：

```sh
mkdir -p /checkpoints && chmod -R a+rwX /checkpoints
```

它只作用于挂进来的 `/checkpoints`，不动宿主机别的东西。所以：

| 你要注意的 | 说明 |
|---|---|
| 不要在 Dockerfile 里 `USER 1000` 这种非 root 用户 | 平台模板不替你补 `securityContext`，非 root 用户在某些宿主机路径上会写不进去；`apt-get`/`pip` 想临时提权也不一定有 `sudo` |
| 不要 `chmod 600` 产物 | initContainer 在下一次任务启动时会放开，但你这一次的训练产物在**任务结束前** Web 就读不到 |
| 文件模式用 `644`/`755`（默认 umask 即可） | 不要显式 `os.chmod(..., 0o600)` |
| 训练进程里的 `HOME` 是 `/root` | `torch.hub`、`HF_HOME` 默认落在 `/root/.cache`，**这是可写的容器层，任务结束就丢**。要缓存就显式 `ENV HF_HOME=/opt/hf`（见第 10 节） |

> initContainer 的**兜底不等于没问题**：它只保证 `/checkpoints` 可读写，不保证
> **先写出来的文件在训练途中**就能被 Web 读到。有「训练中边跑边看产物」需求的，
> 自己在训练脚本里写文件时用 `0644`。

### 2.6 不要依赖 `/datasets` 可写、`/app` 可写、`/tmp` 很大

| 路径 | 实际情况 | 对策 |
|---|---|---|
| `/datasets` | 只读挂载 | 任何缓存/中间产物都挪到 `$CHECKPOINT_DIR` 或容器内可写路径 |
| `/app` | 镜像层，只读（除非开启了训练代码挂载） | 不要往 `/app` 写；代码本身也不在运行时改 |
| `/tmp` | 容器可写，但任务结束就丢，且会占用节点磁盘 | 大中间文件写 `$CHECKPOINT_DIR` |
| `$HOME` (`/root`) | 容器层，任务结束丢 | `HF_HOME` / `TRANSFORMERS_CACHE` 显式指向镜像内路径 |
| `/dev/shm` | 默认 `4Gi` 的内存盘 | 超过就降 `num_workers`，或找管理员调 `PLATFORM_SHM_SIZE` |

---

## 3. 最小可用 Dockerfile

下面这份是**可以整份复制**的最小骨架：一个 `python:3.12-slim` 基础镜像、
国内源、一个训练脚本、一个入口。把它放在你的构建目录里（例：`~/my-train-image/`），
同目录放 `train.py` 和 `entrypoint.sh`。

```dockerfile
# ---------- 基础镜像 ----------
# 本机 buildah 缓存里已有 python:3.11-slim / python:3.12-slim。
# 版本要和你的 torch wheel 的 cpXX ABI 对上（见 9.1 与 9.8）。
FROM python:3.12-slim

# ---------- 国内源（构建期用，不影响运行时行为）----------
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=180 \
    PIP_INDEX_URL=${PIP_INDEX}

# apt 换清华源。python:3.12-slim 基于 Debian，官方源在本网络约 KB/s 级。
# 只在 debian.sources 存在时替换，换基础镜像版本不会静默失效。
RUN set -eux; \
    if [ -n "${APT_MIRROR}" ] && [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libgomp1 ffmpeg libgl1 libglib2.0-0 ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*

# ---------- 依赖 ----------
# torch 千万别这样写：pip install torch
# 不加约束时 pip 会从 PyPI 取 CPU 版，镜像里没有 CUDA，训练必失败（见 7.1）。
# 正确做法：预先把 cuXXX 的 wheel 放进构建上下文，按文件装。
COPY wheels/ /tmp/wheels/
RUN set -eux; \
    pip install /tmp/wheels/*.whl; \
    rm -rf /tmp/wheels; \
    python -c "\
import torch; \
assert torch.version.cuda, f'装成了 CPU 版 torch: {torch.__version__}'; \
print('torch', torch.__version__, 'cuda', torch.version.cuda)"

# 其余纯 Python 包走国内 PyPI
RUN set -eux; \
    pip install "numpy<2.3" pyyaml tqdm tensorboard

# ---------- 训练代码 ----------
WORKDIR /app
COPY train.py /app/train.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# ---------- 构建期自检（不依赖 GPU）----------
# 目的是让「import 失败 / 脚本语法错 / 路径写错」在构建时就暴露，
# 而不是等任务排队 5 分钟、跑到第 1 步才炸。
RUN set -eux; \
    python -m py_compile /app/train.py; \
    python -c "import torch, yaml, numpy; print('imports ok', torch.__version__)"

# 不要在这里判断 torch.cuda.is_available()：构建时没有 GPU，结果一定是 False。
# GPU 自检放在 entrypoint.sh 里，任务真正跑起来时再做（见第 3 节的 entrypoint 自检）。

ENTRYPOINT ["/app/entrypoint.sh"]
```

配套的 `entrypoint.sh`（**只做三件事**：确认约定、等 GPU、转发参数）：

```bash
#!/usr/bin/env bash
# 容器入口。注意平台是 /bin/bash -lc "<command>" 启动的，
# 手填 command 时本脚本不会执行；它主要用于 docker run 调试和配方的不带参场景。
set -euo pipefail

# 1. 平台约定：数据集和产物目录从环境变量来。
#    缺失时明确报错，不要让它空着继续跑到第 1 步才炸。
DATASET_ROOT="${DATASET_ROOT:-}"
CHECKPOINT_DIR="${CHECKPOINT_DIR:-/checkpoints/${JOB_NAME:-local}}"
if [[ -z "$DATASET_ROOT" ]]; then
  printf 'DATASET_ROOT 未设置。手工运行时请指定：\n' >&2
  printf '  DATASET_ROOT=/datasets/<数据集名> docker run ...\n' >&2
  printf '提交任务时用 --env DATASET_ROOT=/datasets/<数据集名>（一键训练会自动注入）。\n' >&2
  exit 2
fi
export DATASET_ROOT CHECKPOINT_DIR

# 2. GPU 自检。放在这里而不是构建期：构建时没有 GPU。
python - <<'PY'
import sys, torch
print(f"[ENV] torch={torch.__version__} cuda_build={torch.version.cuda}", flush=True)
if not torch.cuda.is_available():
    print("VLA_TRAIN_FAILED: torch.cuda.is_available() 为 False（torch 是 CPU 版？没拿到 GPU？）",
          file=sys.stderr, flush=True)
    sys.exit(1)
p = torch.cuda.get_device_properties(0)
print(f"[ENV] gpu={torch.cuda.get_device_name(0)} vram={p.total_memory / 1024**3:.1f}GiB", flush=True)
PY

# 3. 转发参数。exec 让训练进程成为 PID 1 的后继，取消任务时信号能直接收到。
exec python /app/train.py "$@"
```

配套的 `train.py` 骨架（把 `# TODO` 换成你的框架调用即可）：

```python
#!/usr/bin/env python3
"""平台训练入口骨架：只处理平台约定，训练逻辑自己填。"""
import argparse, json, os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=100, help="总步数（参数模板对应 fields）")
    parser.add_argument("--batch_size", type=int, default=8)  # flag 风格要与参数模板一致
    parser.add_argument("--log_freq", type=int, default=10)
    parser.add_argument("--dataset_root", default=os.environ.get("DATASET_ROOT", ""))
    args = parser.parse_args()

    # --- 平台约定 ---
    dataset_root = Path(args.dataset_root)
    if not dataset_root.is_dir():
        raise SystemExit(f"DATASET_ROOT 不存在：{dataset_root}")
    checkpoint_dir = Path(os.environ.get("CHECKPOINT_DIR") or f"/checkpoints/{os.environ.get('JOB_NAME', 'local')}")
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = checkpoint_dir / "metrics.jsonl"
    out_dir = checkpoint_dir / "run"       # 子目录：任务根目录留给平台指标

    print(f"[START] steps={args.steps} batch_size={args.batch_size} "
          f"dataset={dataset_root} output={out_dir}", flush=True)

    # --- 训练逻辑（换成你的框架）---
    for step in range(1, args.steps + 1):
        # TODO: loss = train_one_step(...)
        loss = 1.0 / (step + 1)
        if step % args.log_freq == 0:
            with open(metrics_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"step": step, "loss": loss}) + "\n")
                fh.flush()
            print(f"[STEP] {step}/{args.steps} loss={loss:.4f}", flush=True)

    # --- 产物 ---
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "README.txt").write_text("replace with your checkpoint\n", encoding="utf-8")
    print(f"[DONE] output={out_dir}", flush=True)
    print("VLA_TRAIN_COMPLETE", flush=True)      # 成功标记，验收时好找
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> **为什么入口要自己写、不直接调框架 CLI？** 三个理由（`images/smolvla-train/README.md` 里写过，
> 对任何框架都成立）：① 框架没有 `DATASET_ROOT` / `/checkpoints/<任务名>` 这套约定；
> ② 需要训练前自检（跑了半小时才因为没 CUDA 失败是最糟的）；
> ③ 需要把框架的必填项报错翻译成人话。

---

## 4. 构建和导入（三条命令）

本机没有 Docker，**buildah 的存储和 K3s 的 containerd 是两套独立存储**：
`buildah bud` 成功只代表 buildah 里有镜像，K8s 完全看不到。所以必须
「build → 导出成 docker-archive → `k3s ctr images import`」。

假设构建目录是 `/home/jjx/my-train-image`，镜像名 `my-train:latest`：

```bash
cd /home/jjx/my-train-image

# 1. 构建。--isolation=chroot 是本机 buildah 1.23.1 的既定做法。
#    产物名带 localhost/ 前缀：localhost/my-train:latest
sudo buildah bud --isolation=chroot -t my-train:latest .

# 2. 导出成 docker-archive。
#    冒号后面那一段是「tar 里的镜像名」，必须连 docker.io/library/ 一起写全，
#    否则导入后名字对不上，提交任务时 Pod 会 ImagePullBackOff。
#    tar 不要放 /tmp（该机器 /tmp 不可写），放 ~/ 下；大小约等于镜像大小。
sudo buildah push --format docker localhost/my-train:latest \
  docker-archive:/home/jjx/my-train.tar:docker.io/library/my-train:latest

# 3. 导入 K3s 的 containerd。命名空间必须是 k8s.io。
sudo k3s ctr -n k8s.io images import /home/jjx/my-train.tar

# 4. 清理 tar，避免磁盘被多个镜像包塞满
rm -f /home/jjx/my-train.tar
```

**每次改动都要换 tag**（`my-train:v1`、`my-train:v2`……）。
平台固定 `imagePullPolicy: IfNotPresent`，同名同 tag 的镜像导入后**不会**被用到——
kubelet 认为本地已经有这个 tag 了。见第 9 节 9.9。

更换 tag 的三条命令（把名字和 tag 替换掉即可）：

```bash
V=v2
sudo buildah bud --isolation=chroot -t my-train:$V .
sudo buildah push --format docker localhost/my-train:$V \
  docker-archive:/home/jjx/my-train-$V.tar:docker.io/library/my-train:$V
sudo k3s ctr -n k8s.io images import /home/jjx/my-train-$V.tar && rm -f /home/jjx/my-train-$V.tar
```

> 也可以用 `buildah tag` 两步走（`bud` 之后 `tag localhost/x docker.io/library/x` 再 `push`），
> 两种效果一样。本文用「push 时直接指定 tar 内名字」，少一步，也不依赖 tag 之间的引用关系。
>
> ⚠️ 加 `--format docker`：导出的 manifest 正好是 K8s 能识别的
> `application/vnd.docker.distribution.manifest.v2+json`（不指定格式时是 OCI manifest，
> containerd 也能导入，但混合新旧格式会让「导入成功却看不到名字」更难判断）。

---

## 6. 先在本机快速试跑（可选，但能省掉一半排障）

不需要 GPU、不需要导入 K3s，在**宿主机**上直接跑你的训练脚本，先把平台约定验一遍：

```bash
cd ~/my-train-image

# 用宿主机自带的 python 跑即可。宿主机是 3.10、镜像里是 3.11/3.12，
# 但「读 DATASET_ROOT / 写 CHECKPOINT_DIR / 写 metrics.jsonl」这套逻辑与版本无关。
# 唯一要绕开的是 torch：宿主机上的 torch 未必是 CUDA 版，
# 入口里的 GPU 自检会在这一关把你挡住（这本身也是对的，见下面的「期望」）。
# 输出不要写 /tmp：该机器的 /tmp 不可用，写 ~/ 下。
DATASET_ROOT=/data/vla-platform/datasets/pick1028 \
CHECKPOINT_DIR=~/tryrun-ckpt/job1 JOB_NAME=job1 \
  bash entrypoint.sh --steps 5 --log_freq 1

echo "--- 指标文件（平台会读这个）:"
cat ~/tryrun-ckpt/job1/metrics.jsonl
stat -c '%a %n' ~/tryrun-ckpt/job1/metrics.jsonl   # 期望 644
```

期望：退出码 0，`metrics.jsonl` 每行一个 `{"step": ..., "loss": ...}`，
产物落在 `~/tryrun-ckpt/job1/` 下、文件模式 `644`（不是 `600`）。
如果卡在 `VLA_TRAIN_FAILED: torch.cuda.is_available() 为 False`，说明宿主机 torch 是 CPU 版——
把这行自检临时注释掉再跑一次即可，这一步验证的是**约定**不是**驱动**。

能过这一步，剩下要在平台上验证的就只有「依赖装没装对」和「GPU 认不认得上」两件事了。

---

## 7. 验证：导入之后必须查一遍

**构建成功 ≠ 能跑。** 导入后、提交前必须确认真实存在的名字：

```bash
sudo k3s ctr -n k8s.io images ls -q | grep my-train
```

期望看到**两行**（`buildah push` 会把两个名字都写进 tar，ctr 两个都导入）：

```text
docker.io/library/my-train:latest
localhost/my-train:latest
```

- 只有 `localhost/my-train:latest` 一行：说明 push 时没写 `docker.io/library/` 全名，
  提交时 K8s 找不到，会去公网拉然后失败（第 9 节 9.2）。
- 一行都没有：import 失败或导入了别的命名空间（`-n k8s.io` 漏了）。
- **不要只 `grep my-train` 就算过**：`my-train` 也是 `my-train-v2` 的子串，旧 tag 会一起命中。
  判断标准是「`docker.io/library/<镜像名>:<tag>` 这一行存在」，不是「有输出」。

还要确认**每个要跑训练的节点都导入了**（多节点部署时）：
本机 `buildah`/`containerd` 里可见，不等于别的节点可见（`docs/multi-node.md` 第 3 条）。

镜像内容的自检（不占 GPU、几秒）：

```bash
# 看这个镜像的 User / ENTRYPOINT / CMD / Env。
# 注意 buildah 1.23.1 的输出里 Config 是**一个 JSON 字符串**，要二次解析，
# 且字段在它的 config 子对象下。
sudo buildah inspect localhost/my-train:latest | python3 -c '
import json, sys
cfg = json.loads(json.load(sys.stdin)["Config"])["config"]
print("User      :", cfg.get("User") or "root（默认）")
print("Entrypoint:", cfg.get("Entrypoint"))
print("Cmd       :", cfg.get("Cmd"))
for env in cfg.get("Env") or []:
    print("Env       :", env)
'
```

**最省事的验证方式**：用极小参数提交一个真任务（`--steps 2`），看日志能到
`VLA_TRAIN_COMPLETE`。代价比反复排障小得多。

---

## 8. 提交任务时的镜像名怎么写

| 你在哪里写 | 写什么 | 平台/K8s 怎么理解 |
|---|---|---|
| `buildah push ... docker-archive:<路径>:docker.io/library/my-train:latest` | 全名 | tar 里记录的镜像名 |
| 提交页 / CLI `--image` / REST `"image"` | **`my-train:latest`**（不带 registry 前缀） | K8s 自动补成 `docker.io/library/my-train:latest` |
| 配方 YAML 的 `image:` | **`my-train:latest`** | 同上 |

**规则只有一条：导入时用的名字，提交时就用同一个。**

平台**不做任何名字规范化**（`SubmitSpec.image` 原样保留），补 `docker.io/library/`
是 K8s/kubelet 解析「不带 registry 的镜像名」时的默认行为。所以：
导入时用 `docker.io/library/my-train:latest`，提交时写 `my-train:latest` 或全名都行；
导入时只有 `localhost/my-train:latest`，提交时写什么都不对。

常见对不上的情况：

| 导入成了 | 提交时写了 | 结果 |
|---|---|---|
| `docker.io/library/my-train:latest` | `my-train:latest` | ✅ 正常（K8s 补全后一致） |
| `docker.io/library/my-train:latest` | `my-train` | ❌ 补成 `my-train:latest`，不一致 → 去公网拉 → `ImagePullBackOff` |
| `localhost/my-train:latest` | `my-train:latest` | ❌ 名字不一致 → `ImagePullBackOff` |
| `docker.io/my-train:latest` | `my-train:latest` | ❌ 少了 `library/` |
| 任何名字，但镜像里没有 `/bin/bash` | `my-train:latest` | ❌ 容器起不来：`exec: "/bin/bash": stat /bin/bash: no such file or directory`（见第 9 节 9.7） |

> 配方里的 `image` 是「一键训练」用的；配方登记了但镜像没导入，用户点一下就会得到
> `ImagePullBackOff`。登记配方前先按第 7 节验证一遍。

---

## 9. 常见坑（每条都是真实的）

每条按「症状 → 原因 → 解法」写。前六条来自 `images/smolvla-train/` 和
`images/smoke-train/` 的注释（都标注了实测），后面几条来自平台后端与部署清单。

### 9.1 torch 装成了 CPU 版

- **症状**：任务能跑起来，日志里 `torch.cuda.is_available()` 是 `False`；
  `torch.__version__` 形如 `2.14.1` 且没有 `+cuXXX`。
- **原因**：`pip install torch` / 把 `torch` 交给某个框架去解析依赖时，pip 会选 PyPI 上最新的
  **CPU 版**。框架声明的区间往往很宽（lerobot 是 `torch>=2.2.1,<2.11`），完全不会阻止它选错。
- **解法**：
  1. torch/torchvision 从 **CUDA 专用 wheel** 装：`pip install /tmp/wheels/torch-2.6.0+cu124-*.whl`（见第 3 节）。
     wheel 从阿里云的 pytorch-wheels 文件目录下载（`fetch-wheels.sh` 里有可用的 URL 和并发写法），
     **不要**用 `pip download torch`（它走 PyPI 索引，返回的仍然是 CPU 版）。
  2. 构建期断言，把它挡在构建阶段：

     ```dockerfile
# 接在第 3 节的依赖步骤之后
RUN python -c "import torch; \
    assert torch.version.cuda and torch.version.cuda.startswith('12.4'), \
        f'not a cu124 build: {torch.__version__}'; print('ok')"
     ```

  3. 注意**不要**在构建期判断 `torch.cuda.is_available()`：构建时没有 GPU，结果一定是 False，
     这是个假阳性。
- **相关**：`torch==2.6.0+cu124` 这种带本地版本号的钉法，放进别的包的依赖解析会变成
  `ResolutionImpossible`（PEP 440 下 `+cu124` 不满足 `==2.6.0`），要单独按文件装。

### 9.2 忘了导入 k3s / 名字不对 → `ImagePullBackOff`

- **症状**：任务状态停在「排队中」的 `Pending`，Pod 事件里是
  `ImagePullBackOff` / `ErrImagePull` / `manifest unknown` / `pull access denied` / `403`；
  日志是空的（容器根本没起来）。
- **原因**：两个独立的原因，表现一样：
  1. buildah 构建成功，但从没 `k3s ctr images import`——containerd 里没有，
     kubelet 按 `docker.io/library/<名字>` **去公网仓库拉**，被防火墙挡回来（403）。
  2. 导入了，但名字/tag 与提交时填的不一致（第 8 节的表格）。
- **解法**：
  ```bash
sudo k3s ctr -n k8s.io images ls -q | grep docker.io/library/my-train
cli/vla-train events job-name        # 先看事件，它会直接说拉的是哪个名字
kubectl -n vla-training describe pod PODNAME | tail -30
  ```
  对照第 8 节的表格修正名字，重新导入。**任务不会自己好起来**，改完要重新提交。
- **代码里的对应处理**：`backend/app/services/diagnostics.py` 把
  `ImagePullBackOff|ErrImagePull|Failed to pull image|manifest unknown` 识别成
  `image_pull_backoff` 诊断，提示语就是「构建成功不等于能跑」。

### 9.3 `pynput` → `evdev` 编译失败

- **症状**：构建在 `pip install` 阶段失败，最后几行是 `evdev` 的 `gcc` 报错
  （`Python.h: No such file`、`linux/input.h` 找不到之类）。
- **原因**：某些框架（如 lerobot）默认依赖 `pynput` → `evdev`，后者是 Linux 输入设备库，
  编译需要内核头件，训练**完全用不到**（它是遥操作/键盘监听用的）。
- **解法**：
  1. 装框架本体时用 `--no-deps`，依赖按它 METADATA 的 `Requires-Dist` 显式列出来，
     **排除 `pynput`**（`images/smolvla-train/Dockerfile` 的第二步就是这么干的）；
  2. 顺便把 `libgomp1`、`libgl1`、`libglib2.0-0` 装上——这些是 torch/opencv 的**运行**依赖，
     不装的话镜像能构建成功，但 import 时炸（第 3 节已经装了）；
  3. 非要装输入设备库，就先 `apt-get install -y python3-dev libevdev-dev`。不建议：
     训练任务不需要它。
- **顺序也很重要**：先按官方 constraints 装依赖，**最后**再 `--no-deps` 装框架本体。
  反过来做会让后续每次 `pip install` 重新解析全局版本，把 `huggingface-hub` 升到不兼容的版本
  （`Dockerfile` 注释里记了：升到 2.4.0 后 `transformers` 直接 `ImportError`）。

### 9.4 torchcodec 加载失败（只在你也用 lerobot 时相关）

- **症状**：训练能启动，**跑到采第一个 batch 才**抛
  `RuntimeError: Could not load libtorchcodec`；报错里满是 ffmpeg 相关字样，
  会把人引向「是不是缺 ffmpeg」。
- **原因**：lerobot 判断「视频后端可用吗」只看 `importlib.util.find_spec("torchcodec")`——
  能 import 到就算可用，但真正 `dlopen` 在特定 torch 次版本上会失败
  （实测 torch 2.6.0+cu124 上 0.4/0.6/0.7/0.8/0.9/0.10 全部失败）。
- **解法**（`images/smolvla-train/torchcodec_guard/sitecustomize.py`，二十行）：
  注入一个 `sitecustomize.py` 让 `find_spec("torchcodec")` 返回 `None`，
  lerobot 就会打印一行 `torchcodec is not available, falling back to 'pyav'` 并正常训练：

  ```dockerfile
# 放在训练进程 import lerobot 之前的任意位置
ENV PYTHONPATH=/opt/torchcodec_guard
COPY torchcodec_guard/sitecustomize.py /opt/torchcodec_guard/sitecustomize.py
  ```

  ```python
# sitecustomize.py
import importlib.util as _util
_original = _util.find_spec
def find_spec(name, *args, **kwargs):
    if name == "torchcodec" or name.startswith("torchcodec."):
        return None
    return _original(name, *args, **kwargs)
_util.find_spec = find_spec
  ```

  构建期验证：`python -c "import importlib.util; assert importlib.util.find_spec('torchcodec') is None"`。
- **不用 lerobot 的话这段完全不用抄。** 用 OpenAI 系框架（openpi）等不要加，
  否则会把人家要用的东西屏蔽掉。
- 另外 `av` 包要装（`pip install av`），它是 pyav 后端。

### 9.5 文件权限 / 只读文件系统

- **症状 A**：`Permission denied` 写到 `/datasets/...`，或 `Read-only file system`。
  **原因**：`/datasets` 是只读挂载。
  **解法**：任何「想改数据集目录」的操作（norm-stats 缓存、stats 覆盖、转换后的数据集）
  都要先 `cp` 到 `$CHECKPOINT_DIR` 再处理，或由用户在宿主机侧准备好。
- **症状 B**：写 `/checkpoints/<任务名>/` 报 `Permission denied`。
  **原因**：老版本镜像产物属主与平台读写方不一致；或者你在 Dockerfile 里切了 `USER`。
  **解法**：**训练镜像保持 root 运行**，由平台的 `initContainer`（`chmod -R a+rwX /checkpoints`）
  兜底。不要在镜像里 `USER`，也不要在代码里 `chmod 600`（见 2.5）。
- **症状 C**：文件在 `/checkpoints` 里能写，但 Web「产物」页看不到 / 下载 403。
  **原因**：同上，文件模式是 `600`，Web 以 `uid=10001` 读不了。
  **解法**：训练脚本写文件用默认 umask，或显式 `chmod 644`。
- **症状 D**：数据集读不到（用户报「数据集空」/`info.json` 不存在）。
  **原因**：数据集目录权限对容器用户不可读（`chmod -R a+rX` 没做）。
  **解法**：见 `training-env-guide.md` 3.1 节。

### 9.6 大镜像导入慢 / 磁盘被吃满

- **症状**：`buildah push` 导出 tar 或 `k3s ctr images import` 要等很久（10 GB 级镜像
  按磁盘写速估算，**分钟级**是正常的），或者根分区很快吃满。
  **导出/导入本身并不慢**（实测 332 MB 的镜像导出约 1.2 秒，纯本地文件到本地存储），
  所以「很慢」往往不是 import 的问题，而是磁盘已经在临界点上。
- **数据**：参考镜像 `vla-smolvla:latest` 有 **10.8 GB**（`buildah images` 实测）；
  本机根分区实测**已用 83%（737G/938G，剩 153G）**，`/tmp` 与 `/home/jjx` 在同一个分区。
- **原因与对策**：
  1. **不要往镜像里塞数据集**。数据集走挂载，塞进去镜像动辄几十 GB，换数据还得重建。
  2. **权重按需过滤**。SmolVLM2 整仓 7.37 GB，其中绝大部分是 `onnx/` 下的各种量化导出，
     PyTorch 推理一个都用不到；只保留 `*.json / *.safetensors / *.txt` 后是 1.9 GB
     （`Dockerfile` 的第五步就是这么做的）。
  3. **tar 只留一份、导完立刻删**：`k3s ctr -n k8s.io images import x.tar && rm -f x.tar`。
  4. **同一个 tar 不要反复 import**，也不要同时留多个版本的 tar 包。
  5. 构建前先 `df -h /` 看一眼。磁盘满时 buildah 的报错往往是「设备空间不足」，
     容易被误判成网络问题。

### 9.7 镜像里没有 `/bin/bash` → 容器直接起不来

- **症状**：Pod 是 `CrashLoopBackOff` 或 `CreateContainerError`，事件里是
  `exec: "/bin/bash": stat /bin/bash: no such file or directory`。
- **原因**：平台固定用 `["/bin/bash", "-lc", command]` 启动，基础镜像是 Alpine 或 `distroless`
  这类没有 bash 的镜像时，容器根本到不了你的训练代码。
- **解法**：用带 bash 的基础镜像（`python:3.x-slim` / Debian / Ubuntu / NVIDIA 官方 CUDA 镜像都可以）。
  非要用 Alpine，就得在里面 `apk add bash`——但 Alpine 的 glibc/内核兼容问题更多，不建议。

### 9.8 构建卡在下载

- **症状**：`buildah bud` 在 `pip install` 或 `apt-get` 卡几十分钟，或者 `Connection reset`。
- **原因（实测）**：
  - 官方 `download.pytorch.org` / `download-r2` 在这台机器上只有 **~35 KB/s**，
    908 MB 的 torch wheel 要下 7 小时以上；
  - `deb.debian.org` 约 KB/s 级，trixie 的 Packages 索引单这一条就够耗十几分钟；
  - `pypi.org` 直连不通。
- **解法**：阿里云 pytorch-wheels（实测 18 MB/s）+ 阿里云 PyPI + 清华 apt 源，
  并按第 3 节把 wheel 预下载进构建上下文。参考 `images/smolvla-train/fetch-wheels.sh`。
- **另一个坑**：`pip download` 的产物是**宿主机 Python 的 ABI**（宿主机 3.10 → `cp310`），
  而镜像是 3.11/3.12。要带 `--python-version 311 --implementation cp --abi cp311
  --platform manylinux2014_x86_64`（纯 Python 包不受影响）；下完用 `rm -f wheels/*cp310*.whl` 兜底清理。

### 9.9 改了镜像，跑的还是旧代码

- **症状**：重新构建、重新导入，提交的任务日志还是老行为。
- **原因**：平台固定 `imagePullPolicy: IfNotPresent`，**同 tag** 的镜像 kubelet 认为本地有了，
  不会用新导入的那份。
- **解法**：每次构建换 tag（`v2`、`v3`…），提交新 tag。
  （Web 镜像同理，还要 `kubectl -n vla-platform rollout restart deploy/vla-web`，见 `training-env-guide.md`。）

### 9.10 构建期自检里写了 `torch.cuda.is_available()`

- **症状**：Dockerfile 明明没问题，构建却断言失败。
- **原因**：构建环境没有 GPU，结果恒为 False；另外 NVIDIA 容器运行时要 `runtimeClassName: nvidia`
  才会注入 GPU，构建时不存在。
- **解法**：构建期只做 import / 语法 / 路径检查；GPU 检查放到 entrypoint 或 `train.py` 启动时（第 3 节）。

### 9.11 训练脚本的参数名与参数模板对不上

- **症状**：任务启动就退出，报 `unrecognized arguments: --xxx`；或者日志里参数是默认值，你填的没生效。
- **原因**：参数模板的 `flag` 字段拼出来的 flag 与你脚本里的不一致（下划线 vs 中划线、点号 vs 下划线）。
- **解法**：本地 `python train.py --help` 看真实 flag，逐字抄进模板；改完重新登记参数模板
  （`training-env-guide.md` 3.5.1 节）。
- 顺带注意：`bool` 参数同时设 `default: false` 和 `false_value: "false"` 时，**不传值也会拼出
  `--flag false`**，很多框架的 argparse 会因此报错。

### 9.12 `DataLoader worker ... Bus error`

- **症状**：训练跑到第 0 步就崩，`signal: Bus error`。
- **原因**：容器 `/dev/shm` 默认只有 64MB，DataLoader 用它传 batch。平台已经挂成
  `emptyDir{medium: Memory}`（默认 `4Gi`），正常不会出现。
- **解法**：先确认是不是这个原因（跑 `df -h /dev/shm`）；是的话降 `num_workers` /
  `batch_size`，或找管理员调大 `PLATFORM_SHM_SIZE`。

---

## 10. 进阶：预置模型权重

每次任务启动都从 hf-mirror 下几 GB，是最容易被忽略的耗时来源。
参考 `images/smolvla-train/Dockerfile` 的第五步，原则有三条：

### 10.1 用 HF 缓存布局，不要拷成裸目录

```dockerfile
# 官方源 → hf-mirror；HF_HOME 指向镜像内路径
ENV HF_HOME=/opt/hf \
    HF_ENDPOINT=https://hf-mirror.com
RUN pip install huggingface_hub

RUN python -c "\
from huggingface_hub import snapshot_download as sd; \
sd('lerobot/smolvla_base'); \
sd('HuggingFaceTB/SmolVLM2-500M-Video-Instruct', allow_patterns=['*.json', '*.safetensors', '*.txt'])"

# 构建期强制离线验证：缓存布局对不对，靠这一步暴露，而不是任务跑到一半才知道
RUN HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -c "\
from ... import ...; \
obj = Model.from_pretrained('lerobot/smolvla_base'); print('offline load ok')"
```

**为什么必须保持 HF 缓存布局**：`from_pretrained("org/name")` 走的是
`$HF_HOME/hub/models--org--name/snapshots/<rev>/` 这套结构。
把权重拷成 `/opt/hf/smolvla_base/` 这样的裸目录，`HF_HUB_OFFLINE=1` 下会报
`FileNotFoundError: Could not find 'policy_preprocessor.json'`。

**为什么权重是「两份」**：一个 policy 的 `config.json` 里可能引用另一个骨干仓库
（SmolVLA → SmolVLM2-500M），**两个都要在缓存里**。漏了第二个，任务能过 import、
能加载 policy，然后在第一步报找不到 VLM——排查起来很费劲。

**为什么要 `allow_patterns`**：HuggingFaceTB/SmolVLM2-500M-Video-Instruct 整仓 7.37 GiB，
其中绝大多数是 `onnx/` 下的量化导出，PyTorch 一个都用不到；过滤后 1.9 GiB。
过滤清单必须覆盖 `*.json / *.safetensors / *.txt`（`tokenizer.json`、`merges.txt`、`vocab.json`
都在里面，缺了 tokenizer 会报错）。

### 10.2 不要设 `HF_HUB_OFFLINE=1` 作为默认值

`images/smolvla-train` 明确**不在镜像里设** `HF_HUB_OFFLINE=1`：用户的自定义策略/数据集
仍然要能联网拉取。离线只在**构建期验证**那一步临时加。
如果你的任务完全不该联网（内网/合规要求），再把它设成 `ENV HF_HUB_OFFLINE=1`，
并在 entrypoint 里 `echo $HF_HUB_OFFLINE` 打出来，出问题时一眼可见。

### 10.3 什么该预置、什么不该

| 预置 | 不预置 |
|---|---|
| 基座权重（一个任务反复用，且很大） | 数据集（走挂载） |
| tokenizer / 配置文件 | 用户的可选 LoRA 适配器 |
| 框架需要的固定资源（norm-stats、embodiment 配置） | checkpoint（产物写在 `/checkpoints`） |

### 10.4 其他框架的对应做法

| 框架 | 权重通常从哪来 | 预置建议 |
|---|---|---|
| openpi（π0 / π0.5） | `gs://openpi-assets/checkpoints/<model>/params`，走 `gcsfs` 下载 | 内网/国内先 `gsutil -m cp -r` 拉到本地，`ENV OPENPI_ASSETS=/opt/openpi-assets`，并用 `--weight-loader.params-path` 指向它。详见 [`openpi-training-params.md`](openpi-training-params.md) 第 11 条 |
| GR00T（N1.x） | 由 checkpoint 的 `processor_config` 决定，**强依赖 `embodiment_tag`** | 权重 + processor 配置一起预置；新本体必须注册新 tag。详见 [`vla-dataset-format.md`](vla-dataset-format.md) 的模型对比表 |
| 自己的框架 | 任意 | 统一放 `/opt/weights` 并 `ENV <FRAMEWORK>_WEIGHTS=/opt/weights`，**不要在代码里写死路径**，方便换机器 |

---

## 11. 参考实现

`images/smolvla-train/` 是**一个特定框架（lerobot 0.4.4 + torch 2.6.0+cu124）的完整参考**：

| 文件 | 作用 | 对你的框架 |
|---|---|---|
| `Dockerfile` | 五步构建，每步注释都对应一个真实问题 | 第 1、2、4 步通用；第 3 步（pynput 排除）与第 5 步（SmolVLA 权重）**特化** |
| `fetch-wheels.sh` | 预下载 cu124 wheel + CUDA 运行库，绕开慢源和 ABI 错配 | **通用**，改文件名/版本即可 |
| `entrypoint.sh` | 确认 `DATASET_ROOT`、转发参数 | **通用**，照抄改路径 |
| `train_vla.py` | 平台约定 + 环境自检 + lerobot CLI 拼装 | 平台约定部分通用；CLI 拼装**特化** |
| `metrics_hook.py` | 把 lerobot 的 loss 抓成 `metrics.jsonl` + TensorBoard | **特化**（patch lerobot 的 `MetricsTracker`）。你自己有训练循环的话直接调 `log_metrics()`（2.3 节），不需要这层 |
| `metrics_boot/sitecustomize.py` | 让 hook 在训练子进程里生效 | 只有你的入口也是「父进程起子进程」时才需要 |
| `torchcodec_guard/sitecustomize.py` | 让 lerobot 回落 pyav | 只有用 lerobot 时才需要 |
| `README.md` | 为什么这么写、显存实测、已知限制 | 值得读一遍 |

`images/smoke-train/` 是更小的参考（只有 torch + 一个合成训练脚本），
适合拿来对照「最小可行镜像长什么样」。

---

## 12. 交付前检查清单

- [ ] 基础镜像带 `/bin/bash`（平台用 `/bin/bash -lc` 启动）
- [ ] 构建期自检过：`python -c "import torch; assert torch.version.cuda"` 不是 CPU 版
- [ ] 训练脚本从 `DATASET_ROOT` 读数据集，从 `CHECKPOINT_DIR` 写产物，**没有写死宿主机路径**
- [ ] 产物写在 `$CHECKPOINT_DIR` 下的子目录，不往 `/datasets`、`/app`、`/tmp` 写
- [ ] 每 N 步往 `$CHECKPOINT_DIR/metrics.jsonl` 追加一行、且 `flush()`；`step` 是数字
- [ ] 镜像**没有**切 `USER`（保持 root，交给 initContainer 兜底）；产物文件不设 `0600`
- [ ] 配方 `entrypoint` 路径与镜像内路径一字不差；参数 `flag` 与脚本 `--help` 一致
- [ ] 按第 4 节三条命令构建导入，**换了新 tag**
- [ ] 第 7 节验证：`k3s ctr -n k8s.io images ls -q | grep docker.io/library/<镜像>` 有输出
- [ ] 用 `--steps 2` 提交过一个小任务，看到 `VLA_TRAIN_COMPLETE` 和曲线上的点
- [ ] 权重（如果有）在镜像内，且**构建期离线加载**验证过
- [ ] tar 包已删除，磁盘没有被多个镜像包塞满