FROM python:3.11-slim

# 单机 smoke 与多机 smoke 用同一个镜像，保证 NCCL 版本在各节点完全一致
# （跨节点 NCCL 最常见的问题就是两台机器装了不同的 torch/CUDA 轮子）。
# libgomp1 是 torch 的 OpenMP 运行时，原镜像已有。
#
# 源的选择（实测数据，别改回去）：
#   官方 download.pytorch.org / download-r2.pytorch.org 在本网络只有 ~35 KB/s，
#   908 MB 的 torch wheel 要下 7 小时以上，构建等于卡死。
#   阿里云的 pytorch-wheels 是普通的文件目录（不是 pip index），但实测下载
#   可达 18 MB/s。因此改为：构建前把 wheel 预下载到构建上下文，Dockerfile 直接
#   从本地文件安装；其余 Python 包走阿里云 PyPI。
#
# 预下载（在仓库根目录执行，约 1 分钟）：
#   bash images/smoke-train/fetch-wheels.sh
ARG PYPI_INDEX=https://mirrors.aliyun.com/pypi/simple/
ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_INDEX_URL=${PYPI_INDEX}

# apt 也换国内源：python:3.11-slim 基于 Debian，官方 deb.debian.org 在本网络约 1.6 KB/s，
# trixie 的 Packages 索引有 9.6 MB，单这一条就耗掉十几分钟。换成清华源后是 MB/s 级。
# 仅在构建时需要，不影响运行。若目标环境访问不了清华源，用 --build-arg APT_MIRROR= 覆盖。
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
RUN set -eux; \
    if [ -n "${APT_MIRROR}" ] && [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends libgomp1; \
    rm -rf /var/lib/apt/lists/*

# wheel 由 fetch-wheels.sh 预先放到 wheels/ 下；缺失时构建会直接失败并提示，
# 而不是静默回退到慢速源（那会让构建卡几小时，比失败更糟）。
COPY wheels/ /tmp/wheels/
RUN set -eux; \
    pip install --no-cache-dir /tmp/wheels/torch-2.5.1+cu124-cp311-cp311-linux_x86_64.whl; \
    rm -rf /tmp/wheels; \
    python -c "import torch; print('torch', torch.__version__, 'cuda', torch.version.cuda); \
assert torch.version.cuda and torch.version.cuda.startswith('12.4'), 'torch is not a cu124 build'"

WORKDIR /app
COPY train.py /app/train.py

ENV PYTHONUNBUFFERED=1
# 多机默认 NCCL 观测等级。任务模板里可覆盖成 INFO 做排错；
# 注意：「多个节点 NCCL 版本不一致」的检查靠 scripts/preflight.sh 与镜像统一，
# 不依赖这个变量。
ENV NCCL_DEBUG=WARN

CMD ["python", "/app/train.py"]
