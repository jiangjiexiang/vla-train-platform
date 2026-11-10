FROM python:3.11-slim

# SmolVLA 微调镜像（lerobot 0.4.4 + torch 2.6.0+cu124）。
#
# 这份 Dockerfile 的每一步都对应一个实际踩到的问题，改动前请先看说明：
#
# 1. torch 必须从预下载的 cu124 wheel 装，不能交给 pip 解析。
#    lerobot 声明 torch>=2.2.1,<2.11，pip 会选 PyPI 上最新的 CPU 版（2.14.x），
#    镜像里没有 CUDA，训练必然失败。
# 2. lerobot 本体用 --no-deps 单独装，再由它自己的 requirements 补齐依赖——
#    但要把 pynput/evdev 排除掉。evdev 是 Linux 输入设备库，编译需要内核头文件，
#    训练完全用不到（那是遥操作用的），装它会让构建直接失败。
# 3. torchcodec 在这套组合上必定加载失败，注入 sitecustomize 让它回落 pyav。
#    否则训练跑到采第一个 batch 才抛 Could not load libtorchcodec，
#    报错会把人引向「是不是缺 ffmpeg」，排查成本极高。

ARG PYPI_INDEX=https://mirrors.aliyun.com/pypi/simple/
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ENV PIP_DEFAULT_TIMEOUT=180 \
    PIP_INDEX_URL=${PYPI_INDEX} \
    HF_ENDPOINT=https://hf-mirror.com \
    PYTHONUNBUFFERED=1 \
    TOKENIZERS_PARALLELISM=false \
    PYTHONPATH=/opt/torchcodec_guard

# 系统依赖：
#   libgomp1             torch 的 OpenMP 运行时
#   ffmpeg               视频解码（av 自带编解码库，系统 ffmpeg 作为兜底）
#   libgl1/libglib2.0-0  图像库依赖（opencv）
RUN set -eux; \
    if [ -n "${APT_MIRROR}" ] && [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libgomp1 ffmpeg libgl1 libglib2.0-0 curl; \
    rm -rf /var/lib/apt/lists/*

# torchcodec 屏蔽层（见文件头说明 3）
COPY torchcodec_guard/sitecustomize.py /opt/torchcodec_guard/sitecustomize.py

# ---------- 第一步：torch（cu124，离线 wheel）----------
COPY wheels/torch-2.6.0+cu124-cp311-cp311-linux_x86_64.whl /tmp/wheels/
COPY wheels/torchvision-0.21.0+cu124-cp311-cp311-linux_x86_64.whl /tmp/wheels/
RUN set -eux; \
    pip install --no-cache-dir /tmp/wheels/*.whl; \
    rm -rf /tmp/wheels; \
    python -c "import torch; \
assert torch.__version__.startswith('2.6.0'), f'torch 版本不对: {torch.__version__}'; \
assert torch.version.cuda and torch.version.cuda.startswith('12.4'), 'torch 不是 cu124 构建'; \
print('torch', torch.__version__, 'cuda', torch.version.cuda)"

# ---------- 第二步：lerobot 的依赖（先装依赖，再装本体）----------
#
# 顺序很重要：先按 lerobot 官方 constraints 装依赖，让 pip 解析出彼此兼容的版本；
# 最后再用 --no-deps 装上 lerobot 本体。反过来做（先装本体再补依赖）会让
# 后续每次 pip install 都重新解析全局版本，把 transformers/huggingface-hub
# 升到不兼容的版本——实测把 huggingface-hub 升到 2.4.0 后 transformers 直接
# ImportError。
#
# pynput / evdev 被排除：训练不需要，且 evdev 编译需要内核头文件。
# 依赖清单直接照抄 lerobot 0.4.4 的 METADATA（Requires-Dist），只去掉 pynput：
#   pynput → evdev 是 Linux 输入设备库，编译需要内核头文件；训练用不到（那是遥操作）。
#
# 版本区间照抄而不要收紧，是因为 lerobot 自己声明了上界（如
# huggingface-hub<0.36.0、diffusers<0.36.0），收紧了反而容易装到不兼容的组合。
RUN set -eux; \
    pip install --no-cache-dir \
        "datasets>=4.0.0,<5.0.0" \
        "diffusers>=0.27.2,<0.36.0" \
        "huggingface-hub[cli]>=0.34.2,<0.36.0" \
        "accelerate>=1.10.0,<2.0.0" \
        "setuptools>=71.0.0,<81.0.0" \
        "einops>=0.8.0,<0.9.0" \
        "opencv-python-headless>=4.9.0,<4.13.0" \
        "av>=15.0.0,<16.0.0" \
        "jsonlines>=4.0.0,<5.0.0" \
        "packaging>=24.2,<26.0" \
        "pyserial>=3.5,<4.0" \
        "wandb>=0.24.0,<0.25.0" \
        "draccus==0.10.0" \
        "gymnasium>=1.1.1,<2.0.0" \
        "rerun-sdk>=0.24.0,<0.27.0" \
        "deepdiff>=7.0.1,<9.0.0" \
        "imageio[ffmpeg]>=2.34.0,<3.0.0" \
        "termcolor>=2.4.0,<4.0.0" \
        "transformers>=4.57.1,<5.0.0" \
        "torchcodec>=0.2.1,<0.11.0" \
        "numpy" "pillow" "matplotlib" "scipy" "tqdm" "pyyaml" "pandas" \
        "num2words" \
        "sentencepiece" \
        "protobuf" \
        "pyarrow" \
        "requests" \
        "regex" \
        "h5py" \
        "gitpython" \
        "dill" \
        "multiprocess" \
        "xxhash" \
        "aiohttp" \
        "tensorboard"

# tensorboard 单独说明为什么在这里装：
#   metrics_hook.py 会在训练进程里开 SummaryWriter，把 loss/grad_norm/lr/update_s
#   同步写到 <CHECKPOINT_DIR>/tensorboard。torch 2.6 不再自带 tensorboard
#   （只有 torch.utils.tensorboard 这层门面），不显式装的话 hook 会降级成
#   「只写 metrics.jsonl」——训练不受影响，但 TensorBoard 目录是空的。
#   这里用不需要额外参数的 tensorboard 本体，走 Dockerfile 顶部的 PIP_INDEX_URL（国内源）。
#   装完立刻验证，避免又出现「构建成功、运行到第一次写 events 才报 ImportError」。
RUN set -eux; \
    python -c "\
from torch.utils.tensorboard import SummaryWriter; \
import tensorboard; \
print('tensorboard', tensorboard.__version__, 'SummaryWriter 可用')"

# ---------- 第三步：lerobot 本体（不解析依赖）----------
RUN set -eux; \
    pip install --no-cache-dir --no-deps "lerobot==0.4.4"; \
    python -c "import lerobot; print('lerobot ok')"

# ---------- 第四步：构建期自检 ----------
# lerobot 的问题往往表现为「训练跑一半才报 import 错」，在这里就暴露出来。
# 直接 import 训练脚本的真实入口，比只 import lerobot 包更能发现缺失依赖。
RUN set -eux; \
    python -c "from lerobot.scripts.lerobot_train import main; print('lerobot-train 入口可导入')"; \
    python -c "\
import torch, torchvision, transformers, accelerate, importlib.util; \
assert torch.__version__.startswith('2.6.0'), 'torch 被改写'; \
assert torchvision.__version__.startswith('0.21'), 'torchvision 被改写'; \
assert importlib.util.find_spec('torchcodec') is None, 'torchcodec guard 未生效'; \
print('版本检查通过:', torch.__version__, torchvision.__version__, transformers.__version__)"

# ---------- 第五步：预置 SmolVLA 权重（HF 缓存布局）----------
#
# 为什么要预置：训练命令固定用 --policy.path=lerobot/smolvla_base，它会从 Hub 拉
# 两个仓库——lerobot/smolvla_base（约 865 MiB）和它 config 里引用的
# HuggingFaceTB/SmolVLM2-500M-Video-Instruct（整仓 7.37 GiB，实际只需 1.9 GiB）。
# 每个新任务都重下 2.8 GiB，启动时间全耗在这上面。
#
# 两个关键约束：
# 1. 必须是 HF 缓存布局，不能拷成裸目录。裸目录下 HF_HUB_OFFLINE=1 会报
#    FileNotFoundError: Could not find 'policy_preprocessor.json' ... —— 因为
#    from_pretrained 走的是 HF_HOME/hub/models--org--name/snapshots/<rev> 这套结构。
# 2. SmolVLM2 必须用 allow_patterns 过滤。整仓 7.37 GiB 里绝大部分是 onnx/ 下的
#    各种量化导出（int8/q4/fp16...），PyTorch 推理一个都用不到。只保留
#    *.json / *.safetensors / *.txt 后是 1.9 GiB。这份过滤清单已覆盖
#    tokenizer.json、merges.txt、vocab.json，缺了它们 tokenizer 会报错。
#
# 不在镜像里设 HF_HUB_OFFLINE=1：用户自己的自定义策略或数据集仍要能联网拉取。
ENV HF_HOME=/opt/hf
RUN set -eux; \
    python -c "\
from huggingface_hub import snapshot_download as sd; \
sd('lerobot/smolvla_base'); \
sd('HuggingFaceTB/SmolVLM2-500M-Video-Instruct', \
   allow_patterns=['*.json', '*.safetensors', '*.txt']); \
print('weights downloaded')"; \
    du -sh /opt/hf

# 构建期验证：强制离线加载，确保缓存布局正确且权重完整。
# 训练时能不能命中缓存，靠的是这一步，而不是等任务跑到一半才报错。
RUN set -eux; \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -c "\
from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy; \
p = SmolVLAPolicy.from_pretrained('lerobot/smolvla_base'); \
n = sum(x.numel() for x in p.parameters()); \
print('参数总数:', n); \
assert abs(n - 450_000_000) < 10_000_000, f'权重不对: {n}'; \
print('离线加载成功')"

WORKDIR /app
# 指标 hook：把 lerobot 的 loss 转写成平台能读的 metrics.jsonl。
# lerobot 自己不写任何指标文件，不接这一层平台上就没有 loss 曲线。
COPY metrics_hook.py /app/metrics_hook.py
# metrics_boot/sitecustomize.py 由 train_vla.py 塞进训练子进程的 PYTHONPATH，
# 让 hook 在**训练进程本身**生效。主进程里 patch 不会传染给 subprocess。
COPY metrics_boot/ /app/metrics_boot/
COPY train_vla.py /app/train_vla.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV NCCL_DEBUG=WARN

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["--help"]
 
