# 测试运行环境：Python 3.12 + Node 20 + 两侧依赖全部预装，
# 宿主机不需要装 Python/Node/pytest/vitest。
#
#   docker build -f docker/test.Dockerfile -t vla-platform-test:local .
#   docker run --rm vla-platform-test:local bash scripts/test.sh
#
# 冒烟镜像与浏览器 E2E 不在这个镜像范围内（前者要 GPU 主机，后者要真实部署）。

FROM node:20-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    VENV_DIR=/opt/venv

# deb.debian.org 在当前网络下会卡在 Packages 下载，构建时切到清华镜像。
RUN sed -i \
        -e 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        -e 's|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /repo
COPY backend/requirements-dev.txt backend/requirements.txt /tmp/
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements-dev.txt

COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci --no-audit --no-fund

COPY . .

CMD ["bash", "scripts/test.sh"]
