FROM python:3.11-slim

# 单机 smoke 与多机 smoke 用同一个镜像，保证 NCCL 版本在各节点完全一致
# （跨节点 NCCL 最常见的问题就是两台机器装了不同的 torch/CUDA 轮子）。
# libgomp1 是 torch 的 OpenMP 运行时，原镜像已有。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir torch==2.5.1+cu124 \
        --index-url https://download.pytorch.org/whl/cu124

WORKDIR /app
COPY train.py /app/train.py

ENV PYTHONUNBUFFERED=1
# 多机默认 NCCL 观测等级。任务模板里可覆盖成 INFO 做排错；
# 注意：「多个节点 NCCL 版本不一致」的检查靠 scripts/preflight.sh 与镜像统一，
# 不依赖这个变量。
ENV NCCL_DEBUG=WARN

CMD ["python", "/app/train.py"]
