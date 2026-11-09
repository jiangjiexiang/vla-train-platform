# 如何部署你自己的训练环境

本文面向「有自己的训练代码、想把训练接进本平台」的同事。照本文从零做一遍，就能得到一个能提交、
能排队、能产出 checkpoint、日志能在网页上看到的训练任务。

> 约定：命令默认在平台节点（有 GPU 的那台 K3s 机器）的仓库根目录执行；`<...>` 表示需要替换的值；
> 用本文示例里的任务名（如 `smolvla-v1`）之前先确认没有被占用（`cli/vla-train list`）。
> 全文只用 buildah + K3s 自带 containerd，**不需要、也不要安装 Docker**（理由见
> [`operations.md` 第 6 节](operations.md)）。

## 1. 原理一句话

**训练环境 = 镜像；数据与输出通过挂载提供，不打进镜像。**

镜像里只放三样东西：Python/框架依赖、你的训练代码、要在离线环境加载的基座权重。数据集和
checkpoint 都不进镜像——数据集由平台以只读方式挂到 `/datasets`，输出写到可写的 `/checkpoints`。
这样换代码只重建镜像，换数据只换挂载目录，两者互不影响。

## 2. 三部分组成

| 组成 | 内容 | 由谁提供 | 容器内路径 | 权限 |
|---|---|---|---|---|
| 镜像 | 依赖（torch、lerobot 等）+ 训练代码 + 基座权重 | 你自己用 buildah 构建并导入 K3s | `/app/...`、`/models/...` | 只读（镜像层） |
| 数据集 | LeRobot 数据集目录 | 宿主机 `/data/vla-platform/datasets/<name>` | `/datasets/<name>` | **只读** |
| 输出 | 训练 checkpoint、日志汇总 | 宿主机 `/data/vla-platform/checkpoints/<任务名>` | `/checkpoints/<任务名>` | 可写 |

```text
  宿主机                                        容器（训练 Pod）
  ┌─────────────────────────────────┐          ┌───────────────────────────────┐
  │ /data/vla-platform/datasets     │── ro ───▶│ /datasets                     │
  │   ├── pick1028/  (LeRobot v3.0) │          │   └── pick1028/               │
  │   └── <其它数据集>/             │          │                               │
  ├─────────────────────────────────┤          │                               │
  │ /data/vla-platform/checkpoints  │── rw ───▶│ /checkpoints                  │
  │   ├── smoke001/                 │          │   └── <任务名>/               │
  │   └── <任务名>/                 │          │        (=$CHECKPOINT_DIR)     │
  └─────────────────────────────────┘          │                               │
                                               │ 镜像层（只读）:               │
  本地镜像 tar ──import──▶ K3s containerd ────▶│   /app/train_vla.py           │
   (buildah 构建)                               │   /models/smolvla_base/ ...   │
                                               └───────────────────────────────┘
```

平台创建 PyTorchJob 时固定做这几件事（见 `backend/app/templates/pytorchjob.py`），你不需要自己写 YAML：

| 平台自动处理的项 | 值 |
|---|---|
| 数据集挂载 | 宿主机 `PLATFORM_DATASET_HOST_PATH`（默认 `/data/vla-platform/datasets`）→ `/datasets`，`readOnly: true` |
| 输出挂载 | 宿主机 `PLATFORM_CHECKPOINT_HOST_PATH`（默认 `/data/vla-platform/checkpoints`）→ `/checkpoints`，可写 |
| 单任务输出目录 | 环境变量 `CHECKPOINT_DIR=/checkpoints/<任务名>`；同名变量即便你在 `env` 里写了也会被覆盖 |
| 容器命令 | `["/bin/bash", "-lc", <你填的 command>]`，所以可以直接写 shell |
| 拉取策略 | `imagePullPolicy: IfNotPresent` |
| 运行时 | `runtimeClassName: nvidia`，申请 `nvidia.com/gpu` |
| 排队与优先级 | 打 Kueue 标签，LocalQueue `default`，优先级 `low`/`normal`/`high` |

## 3. 完整流程

### 3.1 准备数据集

数据集必须是 LeRobot 格式，至少包含 `meta/info.json` 与 `meta/tasks.parquet`。放到宿主机
`/data/vla-platform/datasets/<name>`，容器里就是 `/datasets/<name>`。两种方式任选：

**方式一：复制（占磁盘）**

```bash
sudo cp -r <源目录> /data/vla-platform/datasets/<name>
sudo chmod -R a+rX /data/vla-platform/datasets/<name>      # 容器内用户要能读
ls /data/vla-platform/datasets/<name>/meta/info.json
```

**方式二：bind mount（不占额外磁盘，已验证）**

```bash
sudo mkdir -p /data/vla-platform/datasets/<name>
sudo mount --bind <源目录> /data/vla-platform/datasets/<name>
mount | grep /data/vla-platform/datasets/<name>            # 确认挂上了
ls /data/vla-platform/datasets/<name>/meta/info.json
```

> ⚠️ **bind mount 重启后失效**。`mount --bind` 不写 `/etc/fstab`，机器重启后
> `/data/vla-platform/datasets/<name>` 变回一个空目录，任务会报「不是 LeRobot 数据集」或「数据集为空」。
> 重启后重新执行上面的 `mount --bind`；提交任务前用 `mount | grep vla-platform` 确认。
> 需要长期生效就把挂载写进 `/etc/fstab`（`<源目录> /data/vla-platform/datasets/<name> none bind 0 0`），
> 或干脆用方式一。任务**运行中**挂载失效会导致读数据失败，不要对正在跑的数据集动手。

两种方式的共同注意事项：

- 目录结构必须是 `/data/vla-platform/datasets/<name>/meta/info.json`，**多套一层或少套一层都会找不到数据**。
- 平台以该宿主机目录作为 `hostPath`（`type: Directory`），所以 `<name>` 目录必须存在，哪怕是绑定挂载点。
- 数据集目录要对容器内用户可读（上表 `a+rX`）：容器以构建镜像时定义的用户运行，文件属主是别人且
  权限为 `600` 时容器读不到。

### 3.2 编写训练脚本

平台不规定入口脚本名，但下面这些约定能让你的任务在平台上「活得省心」：

| 约定 | 说明 |
|---|---|
| 数据集路径 | 用环境变量或参数传 `/datasets/<name>`，**不要写死宿主机路径**（容器里不存在） |
| 输出目录 | 优先读 `CHECKPOINT_DIR`，缺省落到 `/checkpoints/<任务名>`；不要写 `/datasets`（只读），也不要写 `/app`（镜像层，容器重启即丢） |
| 基座权重 | 放在镜像里（如 `/models/...`），运行时不再联网下载 |
| 日志 | 每 N 步打印 loss、grad_norm、显存峰值、耗时/ETA，并 `flush=True` |
| 完成/失败信号 | 成功时最后打印固定标记（如 `VLA_TRAIN_COMPLETE`）；失败时打印 `VLA_TRAIN_FAILED: 原因` 并 `exit 1` |
| OOM | 捕获 `torch.cuda.OutOfMemoryError`，打印当时的 batch/显存，便于对照第 5 节处理 |

`CHECKPOINT_DIR` 的取值规则（参考 `~/vla-train-image/context/train_vla.py` 的 `resolve_output_dir`）：

```python
import os
from pathlib import Path

def resolve_output_dir(arg_value=None) -> Path:
    if arg_value:                                  # 1. 显式 --output-dir 优先
        return Path(arg_value)
    if os.environ.get("CHECKPOINT_DIR"):           # 2. 平台注入的 /checkpoints/<任务名>
        return Path(os.environ["CHECKPOINT_DIR"])
    return Path("/checkpoints") / os.environ.get("JOB_NAME", "default")
```

启动阶段**必须打印**这几项（出问题时 90% 的排查靠日志完成，不用再进容器猜）：

| 阶段 | 打印内容 | 用途 |
|---|---|---|
| `START` | `steps`/`batch_size`/`grad_accum`、`dataset_root`、`base_model`、`output_dir` | 确认参数和路径就是你以为的那套 |
| `ENV` | `torch.__version__`、`torch.version.cuda`、`torch.cuda.is_available()`、GPU 名称与显存 | 区分「没拿到卡」和「代码问题」 |
| `ENV` | `HF_HUB_OFFLINE` 等离线开关 | 离线镜像里一旦出现联网下载，一眼就能看出来 |
| `DATA` | 数据集 `codebase_version`、`episodes`、`frames`、features 形状 | 确认读到的是不是最新数据、维度对不对 |
| `MODEL` | 参数量/可训练参数量、加载耗时、加载后显存 | 判断权重是否真的来自镜像 |
| `STEP` | 每 N 步 loss / grad_norm / lr / 显存峰值 / 耗时 / ETA | 判断是否在学、是否变慢 |
| `SAVE` / `DONE` | checkpoint 绝对路径、写入的文件列表 | 到宿主机上能直接找到产物 |

示例输出（`vla-smolvla:latest` 日志的格式，数值以实际为准）：

```text
[VLA_TRAIN] [START] steps=500 batch_size=2 grad_accum=1 effective_batch=2
[VLA_TRAIN] [START] dataset_root=/datasets/pick1028 base_model=/models/smolvla_base vlm=/models/SmolVLM2-500M output_dir=/checkpoints/<任务名>
[VLA_TRAIN] [ENV] torch=2.5.1+cu124 (cuda build 12.4)
[VLA_TRAIN] [ENV] GPU[0]: NVIDIA GeForce RTX 4090 total=48.0GiB capability=8.9 SMs=128
[VLA_TRAIN] [DATA] root=/datasets/pick1028 codebase_version=v3.0 episodes=143 ...
[VLA_TRAIN] [STEP] step 10/500 loss=... grad_norm=... gpu_peak=...GiB elapsed=...s eta=...s
[VLA_TRAIN] [SAVE] writing checkpoint -> /checkpoints/<任务名>/checkpoints/000500/pretrained_model
[VLA_TRAIN] [DONE] output_dir=/checkpoints/<任务名>
VLA_TRAIN_COMPLETE
```

> 注意：`train_vla.py` 在 `output_dir` 已存在且非空时会直接退出（lerobot 不允许覆盖）。
> 同一任务名重复提交（尤其是 `clone` 回同名或复用名字）前，先清空
> `/data/vla-platform/checkpoints/<任务名>` 或换新任务名。

最小骨架（可复制后改成自己的训练）：

```python
#!/usr/bin/env python3
"""平台训练入口最小骨架。"""
import os, sys, time, torch

def main() -> int:
    out = os.environ.get("CHECKPOINT_DIR", "/checkpoints/default")
    data = os.environ.get("DATASET_ROOT", "/datasets/pick1028")
    print(f"[START] dataset={data} output={out}", flush=True)

    print(f"[ENV] torch={torch.__version__} cuda={torch.version.cuda} "
          f"available={torch.cuda.is_available()}", flush=True)
    if not torch.cuda.is_available():
        print("VLA_TRAIN_FAILED: no GPU visible", flush=True)
        return 1
    print(f"[ENV] gpu={torch.cuda.get_device_name(0)} "
          f"vram={torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f}GiB", flush=True)

    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    for step in range(1, 11):
        time.sleep(0.1)                      # 换成真正的训练 step
        if step % 5 == 0:
            print(f"[STEP] {step}/10 loss=0.0 elapsed={time.time() - t0:.1f}s "
                  f"gpu_peak={torch.cuda.max_memory_allocated() / 1024**3:.2f}GiB", flush=True)

    torch.save({"step": 10}, os.path.join(out, "model.pt"))
    print(f"[DONE] checkpoint={out}/model.pt", flush=True)
    print("VLA_TRAIN_COMPLETE", flush=True)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except torch.cuda.OutOfMemoryError as exc:
        print(f"VLA_TRAIN_FAILED: CUDA OOM: {exc}", flush=True)
        sys.exit(1)
```

### 3.3 编写 Dockerfile

构建上下文（下文约定为 `~/vla-train-image/context/`）里放 `Dockerfile`、训练脚本、预下载的 wheel
和基座权重。下面是一个**最小可用**示例：

```dockerfile
# 基础镜像：换成和你 torch/CUDA 版本匹配的镜像；本机缓存里有 python:3.11-slim
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    PYTHONPATH=/app

# apt 走清华源（Debian 官方源在本机很慢）
ARG APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
RUN set -eux; \
    . /etc/os-release; \
    printf 'deb %s %s main\n'         "$APT_MIRROR" "${VERSION_CODENAME}"               >  /etc/apt/sources.list; \
    printf 'deb %s %s-updates main\n' "$APT_MIRROR" "${VERSION_CODENAME}"              >> /etc/apt/sources.list; \
    printf 'deb %s %s-security main\n' "$APT_MIRROR" "${VERSION_CODENAME}-security"    >> /etc/apt/sources.list; \
    rm -f /etc/apt/sources.list.d/*; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates ffmpeg libgl1 libglib2.0-0; \
    rm -rf /var/lib/apt/lists/*

# 大文件依赖预先放在构建上下文里（buildah 不能 COPY 上下文之外的路径）
COPY wheels/ /tmp/wheels/
RUN set -eux; \
    pip install --timeout 120 --retries 5 /tmp/wheels/*.whl; \
    rm -rf /tmp/wheels; \
    python -c "import torch; print(torch.__version__, torch.version.cuda)"

# pypi 走阿里云（pypi.org 不通）
ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --timeout 120 --retries 5 --index-url "$PIP_INDEX" <你的包>==<版本>

# 基座权重 + 训练代码
COPY base_model/ /models/base_model/
COPY train_vla.py /app/train_vla.py

# 构建期自检：路径写错、权重缺失在构建时失败，不要留到任务运行时才发现
RUN set -eux; \
    python -m py_compile /app/train_vla.py; \
    test -f /models/base_model/config.json; \
    mkdir -p /checkpoints /datasets; \
    du -sh /models/base_model

CMD ["python", "/app/train_vla.py"]
```

和本机相关的硬性约束：

| 约束 | 原因 / 做法 |
|---|---|
| `buildah bud --isolation=chroot` | 本机 buildah 1.23.1；普通构建跑不通时用 chroot 隔离，已验证 |
| 上下文里不放数据集 | 数据集走挂载；打进镜像会让镜像动辄几十 GB，换数据还要重建 |
| 大 wheel 先下到构建上下文 | buildah 只认上下文内的文件；torch 等大包预先下载 |
| 尽量不在构建时联网装包 | 网络源不稳定，构建耗时会从分钟级涨到小时级，失败还得重来 |

**大文件依赖怎么预下载到构建上下文**（torch 官方源实测只有 ~35KB/s，不可用）：

```bash
cd ~/vla-train-image/context
mkdir -p wheels

# torch / torchvision：阿里云 pytorch-wheels，实测 1.4-3.5MB/s（+ 在 URL 里写成 %2B）
curl -fL --retry 5 -C - -O \
  https://mirrors.aliyun.com/pytorch-wheels/cu124/torch-2.5.1%2Bcu124-cp311-cp311-linux_x86_64.whl
curl -fL --retry 5 -C - -O \
  https://mirrors.aliyun.com/pytorch-wheels/cu124/torchvision-0.20.1%2Bcu124-cp311-cp311-linux_x86_64.whl
mv torch-2.5.1+cu124-cp311-cp311-linux_x86_64.whl \
   torchvision-0.20.1+cu124-cp311-cp311-linux_x86_64.whl wheels/

# 其余纯 Python 包走阿里云 pypi（备选 https://pypi.tuna.tsinghua.edu.cn/simple）
pip download -d wheels -i https://mirrors.aliyun.com/pypi/simple/ \
  --timeout 120 --retries 5 <你的包>==<版本>
ls -lh wheels/
```

> torch 单个 wheel 约 900MB，`curl -C -` 支持断点续传，失败后重跑同一条命令即可。
> 可下载的版本以 `https://mirrors.aliyun.com/pytorch-wheels/cu124/` 的列表为准。

**离线基座权重**：像 `vla-smolvla:latest` 那样，把 `smolvla_base/`（约 865MB）和 `SmolVLM2-500M/`
（约 1.9GB）整体 `COPY` 进 `/models/`，并保持 `HF_HUB_OFFLINE=1`。权重里的 `config.json` 可能指向
原作者机器上的绝对路径，构建时改写成镜像内路径（写法参考 `~/vla-train-image/context/Dockerfile`）。

### 3.4 构建并导入 K3s

本机没有 Docker，buildah 的存储和 K3s 的 containerd 相互独立，所以必须
「build → tag → push 成 docker-archive → `k3s ctr images import`」。
**每次构建都换一个 tag**（`v1`、`v2`……），原因见第 5 节第二条坑。

```bash
cd ~/vla-train-image/context

# 1. 构建（耗时取决于 wheel 是预下载还是联网拉取）
sudo buildah bud --isolation=chroot -t my-train:v1 .

# 2. 必须补上 docker.io/library/ 全名，否则 K8s 按全名去 containerd 里找不到
sudo buildah tag localhost/my-train:v1 docker.io/library/my-train:v1

# 3. 导出 docker-archive。⚠️ 不能放 /tmp（该机器 /tmp 不可写），放 ~/ 下
sudo buildah push docker.io/library/my-train:v1 docker-archive:/home/jjx/my-train.tar

# 4. 导入 K3s 的 containerd（命名空间是 k8s.io）
sudo k3s ctr -n k8s.io images import /home/jjx/my-train.tar

# 5. 验证：能看到 docker.io/library/my-train:v1；提交时镜像填 my-train:v1
sudo k3s ctr -n k8s.io images ls | grep my-train

# 6. 清理 tar（大小约等于镜像），避免磁盘被多个镜像包塞满
rm -f /home/jjx/my-train.tar
```

| 步骤 | 常见错误 | 后果 |
|---|---|---|
| 省掉 `buildah tag` | 只有 `localhost/my-train:v1` | Pod `ImagePullBackOff` / `ErrImagePull` |
| docker-archive 放 `/tmp` | `/tmp` 不可写，push 失败 | 导入的是旧包或根本没有包 |
| `push` 时没带 `docker.io/library/` 全名 | tar 里的镜像名不对 | import 后名字对不上 |
| 忘了 `-n k8s.io` | 导入到 containerd 的默认命名空间 | `images ls \| grep` 查不到 |
| 不删 tar | 每个镜像包多占一份磁盘 | 磁盘写满，后续构建失败 |

### 3.5 提交任务

三种方式等价，最终都是 `POST /api/v1/jobs`。任务名必须符合 `^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`，
最长 50 字符，不能以连字符开头或结尾。

**方式一：网页** `http://<服务器IP>:30080/submit`，填写：任务名、镜像（填 `my-train:v1`，
不用带 `docker.io/library/`）、GPU 数（4090 只能填 `1`）、优先级、命令、环境变量。

**方式二：CLI**（只调 REST API，不碰集群）

```bash
cd /path/to/vla-train-platform

# 首次使用先配置地址和令牌（当前 4090 profile 为 off 模式，但 CLI 仍要求配置了令牌值；
# 当前鉴权模式以 curl http://127.0.0.1:30080/api/v1/health 的 auth_mode 为准）
cli/vla-train config --server http://127.0.0.1:30080 --token "$PLATFORM_API_TOKEN"
cli/vla-train config --show

# 先 --dry-run：只打印请求，确认 env / command / image 都对
cli/vla-train submit \
  --name dry-run-001 --gpus 1 --image my-train:v1 \
  --command 'python /app/train_vla.py --steps 2' --dry-run

# 正式提交
cli/vla-train submit \
  --name smolvla-v1 \
  --gpus 1 \
  --image my-train:v1 \
  --priority normal \
  --env PYTHONUNBUFFERED=1 \
  --env DATASET_ROOT=/datasets/pick1028 \
  --command 'python /app/train_vla.py --steps 100 --batch-size 2 --grad-accum 1 --log-freq 10 --save-freq 50'
```

| 参数 | 说明 |
|---|---|
| `--name` | 任务名，也是 `/checkpoints/<name>` 目录名 |
| `--gpus` | 4090 profile 只允许 `1`（`PLATFORM_ALLOWED_GPUS=1`） |
| `--image` | 第 3.4 节导入的镜像名，不写 `docker.io/library/` 前缀 |
| `--command` | 容器内由 `/bin/bash -lc` 执行，可以写管道和 `&&` |
| `--env KEY=VALUE` | 可重复；`CHECKPOINT_DIR` 写了也会被平台覆盖 |
| `--priority` | `low` / `normal` / `high` |
| `--max-retries` | `0`/`1`/`2`，默认 `0`（失败不自动重启） |
| `--termination-grace-period` | 取消时留给进程退出的秒数，默认 `30` |

**方式三：REST**

```bash
export BASE=http://127.0.0.1:30080/api/v1
curl -fsS -X POST "$BASE/jobs" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "smolvla-v1",
    "gpus": 1,
    "image": "my-train:v1",
    "priority": "normal",
    "env": {"PYTHONUNBUFFERED": "1", "DATASET_ROOT": "/datasets/pick1028"},
    "command": "python /app/train_vla.py --steps 100 --batch-size 2 --log-freq 10 --save-freq 50"
  }'
```

开启鉴权（`token` / `ldap`）时，所有请求加 `-H "Authorization: Bearer $TOKEN"`。

### 3.5.1 登记训练参数模板（让提交页出现参数表单）

默认情况下，提交页只显示平台内置的一套通用参数（`--steps`、`--batch-size`、`--save-freq` 等）。
如果你的训练脚本参数名不一样（比如 lerobot 用 `--batch_size`、openpi 用 `--num-train-steps`、
Hydra 风格 `--config.name`），可以**给镜像登记一份模板**，提交页会据此动态渲染表单，
用户填完自动拼成正确命令行，不用记参数名。

模板就是一份 JSON，格式与示例见仓库 `examples/params/`：

- `examples/params/lerobot-smolvla.json` —— lerobot 风格（下划线参数）
- `examples/params/openpi-pi05.json` —— openpi 风格（Hydra 点号参数）

**登记命令**（需要 admin 权限）：

```bash
# 注意镜像名里的 : 和 / 要 URL 编码；用 curl --get --data-urlencode 更省事
curl -X PUT \
  "http://127.0.0.1:30080/api/v1/images/my-train:v1/params" \
  -H "Content-Type: application/json" \
  --data-binary @examples/params/lerobot-smolvla.json
```

**查看当前生效的模板**：

```bash
curl -s "http://127.0.0.1:30080/api/v1/images/my-train:v1/params" | python3 -m json.tool
```

返回体里的 `source` 说明来源：

| `source` | 含义 |
|---|---|
| `builtin` | 该镜像没有登记模板，使用平台内置的通用参数 |
| `file` | 已登记的模板 |

**删除登记**（之后回退内置模板）：

```bash
curl -X DELETE "http://127.0.0.1:30080/api/v1/images/my-train:v1/params"
```

**字段速查**

| 字段 | 说明 |
|---|---|
| `key` | 参数标识，`^[a-z][a-z0-9_]*$`，同一模板内唯一 |
| `flag` | 命令行 flag，如 `--batch_size`、`--config.name` |
| `label` | 表单上显示的中文名 |
| `type` | `int` / `float` / `str` / `bool` / `enum` / `path` |
| `default` | 默认值，留空时该参数不出现在命令行里 |
| `min` / `max` | 数值范围（仅 int/float） |
| `options` | 枚举可选项（仅 enum 必填） |
| `required` | 是否必填 |
| `help` / `placeholder` | 表单提示文案 |
| `true_value` / `false_value` | bool 参数为真/假时拼的值；不设则 true 只拼 flag、false 跳过 |

> **一个容易踩的坑**：`bool` 参数如果同时设了 `default: false` 和 `false_value: "false"`，
> 那么**不传值时也会拼出 `--flag false`**（default 参与了渲染）。
> 想让「默认不出现」，就不要给这个 bool 设 `default`。

模板的取值会被拼到命令行末尾，规则与提交页底部的「最终命令预览」完全一致——
前端预览与后端实际执行经过一致性测试，看到的预览就是真正会跑的命令。

### 3.6 查看进度与产物

```bash
# 列表 / 详情（Kueue 状态、PyTorchJob 状态、Pod 状态）
cli/vla-train list
cli/vla-train status smolvla-v1

# 日志（queued 时为空是正常的；RUNNING 后应看到 3.2 节约定的体检行）
cli/vla-train logs smolvla-v1 --tail-lines 200

# 事件：ImagePullBackOff、调度失败、被 Kueue 放行都先看这里
cli/vla-train events smolvla-v1

# 取消（释放 GPU，排队任务随即被放行）
cli/vla-train cancel smolvla-v1
```

网页对应 `/jobs` 与 `/jobs/<任务名>` 页面，任务详情里能看到输出目录。产物在宿主机上：

```bash
ls -lh /data/vla-platform/checkpoints/smolvla-v1/
# vla-smolvla 的最终权重在（以实际日志的 SAVE 行路径为准）：
#   /data/vla-platform/checkpoints/<任务名>/checkpoints/000500/pretrained_model
```

集群侧只读核对（可选）：

```bash
kubectl -n vla-training get pytorchjob,workload,pod -o wide | grep smolvla-v1
nvidia-smi
```

## 4. 完整示例：SmolVLA

以已经在平台上跑通的 `vla-smolvla:latest` 为例，串起全流程。参考实现对：
`~/vla-train-image/context/Dockerfile`、`~/vla-train-image/context/train_vla.py`。

| 项 | 值 |
|---|---|
| 镜像 | `vla-smolvla:latest`（lerobot 0.4.4 + torch 2.5.1+cu124，内嵌 SmolVLA 基座权重与 SmolVLM2-500M 骨干） |
| 数据集 | `pick1028`，LeRobot v3.0，143 episodes，14 维双臂 state/action，3 路相机 |
| 训练入口 | `/app/train_vla.py` |
| 参数 | `--steps --batch-size --grad-accum --dataset-root --output-dir --base-model --log-freq --save-freq --num-workers --device --dtype` |
| GPU | 1× RTX 4090 48G |

**步骤 1：确认数据集**

```bash
mount | grep /data/vla-platform/datasets/pick1028     # bind mount 生效时能看到
python3 - <<'EOF'
import json
info = json.load(open("/data/vla-platform/datasets/pick1028/meta/info.json"))
print(info["codebase_version"], info["total_episodes"], info["total_frames"])
print({k: (v.get("shape"), v.get("dtype")) for k, v in info["features"].items()})
EOF
```

预期能看到 `v3.0 143 ...`，`observation.state` / `action` 末位为 `14`，3 个 `video` 类型的相机键。

**步骤 2：换新 tag 重新构建导入**（代码或权重有改动时）

```bash
cd ~/vla-train-image/context
V=v1
sudo buildah bud --isolation=chroot -t vla-smolvla:$V .
sudo buildah tag localhost/vla-smolvla:$V docker.io/library/vla-smolvla:$V
sudo buildah push docker.io/library/vla-smolvla:$V docker-archive:/home/jjx/vla-smolvla-$V.tar
sudo k3s ctr -n k8s.io images import /home/jjx/vla-smolvla-$V.tar
sudo k3s ctr -n k8s.io images ls | grep vla-smolvla
rm -f /home/jjx/vla-smolvla-$V.tar
```

**步骤 3：提交**

```bash
cli/vla-train submit \
  --name smolvla-v1 \
  --gpus 1 \
  --image vla-smolvla:v1 \
  --priority normal \
  --env PYTHONUNBUFFERED=1 \
  --command 'python /app/train_vla.py \
      --steps 500 --batch-size 2 --grad-accum 1 \
      --dataset-root /datasets/pick1028 \
      --base-model /models/smolvla_base \
      --log-freq 10 --save-freq 250 --num-workers 2 --dtype bfloat16'
```

`--output-dir` 不传即可，脚本会读平台注入的 `CHECKPOINT_DIR=/checkpoints/smolvla-v1`；
`--dataset-root`、`--base-model` 在脚本里有默认值，显式写出是为了日志里一眼可见。

**步骤 4：验收**

```bash
cli/vla-train status smolvla-v1
cli/vla-train logs smolvla-v1 --tail-lines 200
# 期望依次看到 START / ENV（4090、48GiB）/ DATA（episodes=143）/ MODEL / STEP / SAVE / DONE，
# 最后一行是 VLA_TRAIN_COMPLETE
ls -lh /data/vla-platform/checkpoints/smolvla-v1/checkpoints/
```

## 5. 常见坑

| 现象 | 原因 | 解决 |
|---|---|---|
| Pod 报 `ImagePullBackOff` / `ErrImagePull`；事件里 `manifest unknown` 或 `pull access denied` | 镜像 tag 不是 `docker.io/library/xxx:tag` 全名，只 import 了 `localhost/xxx` | `sudo buildah tag localhost/my-train:v1 docker.io/library/my-train:v1` 后重新 push + import；提交时填 `my-train:v1`；用 `sudo k3s ctr -n k8s.io images ls \| grep my-train` 核对 |
| 重建镜像后，运行的还是旧代码 | tag 没变：PyTorchJob 是 `IfNotPresent`，containerd 里同 tag 的镜像一直命中旧的那份。Web 部署同理，`deploy-web.sh` 之后需要 `kubectl -n vla-platform rollout restart deploy/vla-web` | **每次构建换 tag**（`v1`/`v2`…）并提交新 tag；Web 侧 import 后补 `rollout restart` |
| `/datasets` 为空，或任务报「dataset root does not look like a LeRobot dataset」 | 数据集没放到宿主机目录；多套/少套一层目录；bind mount 重启后失效 | `ls /data/vla-platform/datasets/<name>/meta/info.json`；`mount \| grep vla-platform`；失效就重新 `mount --bind`；结构必须是 `<datasets>/<name>/meta/...` |
| 训练 OOM（`CUDA out of memory`） | `--batch-size` 太大；视觉编码器也在训练；相机/分辨率太大 | 降 `--batch-size`，用 `--grad-accum` 补回等效 batch（如 `2×4` 代替 `8`）；`--freeze-vision-encoder true`；`--dtype bfloat16`；先用小 `--steps` 验证 |
| `pip install` 超时、`Connection reset`、构建卡在下载 | 直连 `pypi.org` 不通；torch 官方源只有 ~35KB/s | pip 加 `-i https://mirrors.aliyun.com/pypi/simple/`；torch 从 `https://mirrors.aliyun.com/pytorch-wheels/cu124/` 预下载进 `wheels/`；`--timeout 120 --retries 5` |
| checkpoint 写入失败（`Permission denied` / `Read-only file system`） | 输出指到了 `/datasets`（只读）或镜像层；宿主机 `/data/vla-platform/checkpoints/<任务名>` 属主与容器内用户不一致；旧目录非空被脚本拒绝覆盖 | 只写 `$CHECKPOINT_DIR`；`sudo mkdir -p /data/vla-platform/checkpoints/<任务名> && sudo chmod 777 /data/vla-platform/checkpoints/<任务名>`（或把属主改成容器用户 uid）；换新任务名或清空旧目录 |
| 任务一直 `QUEUED`，没有训练 Pod | 4090 只有 1 卡且 Kueue `nominalQuota: 1`，卡被别的任务占着；或申请卡数超出 `allowed_gpus`；或用户配额已满 | `nvidia-smi` 看谁在跑；`cli/vla-train list` 找占卡任务，必要时 `cancel`；`kubectl get clusterqueue vla-cluster-queue` 看已用/配额；`cli/vla-train status <name>` 看 Workload 与事件 |
| `RUNNING` 但日志里 `torch.cuda.is_available()` 为 False | 镜像没拿到 GPU（运行时/驱动问题，通常不是训练代码） | 先跑 `vla-smoke:latest` 对照；`kubectl -n vla-training describe pod <pod>` 看 `runtimeClassName: nvidia` 和 GPU 请求；`kubectl get nodes` 看 `nvidia.com/gpu` |
| 离线镜像里报连不上 huggingface | 权重没打进镜像，或 `config.json` 指向宿主机绝对路径 | 基座权重 `COPY` 进 `/models/`，保持 `HF_HUB_OFFLINE=1`，构建期 `test -f .../model.safetensors` 自检 |
| 改了 Web 镜像但页面行为没变 | 同第二条：tag 未变 + `IfNotPresent` | `k3s ctr images import` 之后 `kubectl -n vla-platform rollout restart deploy/vla-web`，再 `kubectl -n vla-platform rollout status deploy/vla-web` |

## 6. 检查清单

交付前逐条打勾：

- [ ] 数据集在宿主机 `/data/vla-platform/datasets/<name>/meta/info.json`，容器里对应 `/datasets/<name>`；
      bind mount 方式已确认挂载存在，且重启后有恢复办法
- [ ] 训练脚本从 `CHECKPOINT_DIR` 取输出目录，不写 `/datasets`，运行时不依赖联网
- [ ] 脚本启动时打印 START / ENV / DATA / MODEL 体检信息，每 N 步打印 loss 与显存
- [ ] 镜像以 `docker.io/library/<镜像>:<新 tag>` 导入，`sudo k3s ctr -n k8s.io images ls | grep <镜像>` 能看到
- [ ] 构建期验证过 `python -c "import torch; print(torch.__version__, torch.version.cuda)"`，不是 CPU 版
- [ ] 基座权重已在镜像内（`test -f` 自检通过），`HF_HUB_OFFLINE=1`
- [ ] 用新 tag 提交过一个小任务（`--steps 2` 之类）并看到 `VLA_TRAIN_COMPLETE`
- [ ] 产物出现在 `/data/vla-platform/checkpoints/<任务名>/`，重复提交前会清空或换名
- [ ] 任务名符合 `^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`、≤50 字符
- [ ] 团队成员知道 OOM / 排队 / 镜像拉取失败时该看哪条命令（第 5 节）