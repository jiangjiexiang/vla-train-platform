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
        "aiohttp"

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

WORKDIR /app
COPY train_vla.py /app/train_vla.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV NCCL_DEBUG=WARN

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["--help"]
