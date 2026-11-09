# 单容器运行环境：前端构建 + 后端依赖 + 运行时全部在镜像内完成，
# 宿主机不需要安装 Node、npm 或预先生成 frontend/dist。
#
#   docker build -t vla-platform:local .
#   docker run --rm -p 30080:8000 -v $HOME/.kube/config:/kube/config \
#     -e PLATFORM_KUBECONFIG=/kube/config -e PLATFORM_AUTH_REQUIRED=false \
#     -v vla-history:/data/history vla-platform:local
#
# 无 Docker 时可用 buildah 构建同一份 Dockerfile。

# ---------- 1. 构建前端静态资源 ----------
FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# package.json 的 build 脚本是 tsc --noEmit && vite build
RUN npm run build

# ---------- 2. 安装后端依赖 ----------
FROM python:3.12-slim AS dependencies
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /app/requirements.txt

# ---------- 3. 运行时镜像 ----------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLATFORM_DB_PATH=/data/history/platform.db \
    PLATFORM_STATIC_DIR=/app/static

WORKDIR /app
COPY --from=dependencies /install /usr/local
COPY backend/app /app/app
# 训练配方随镜像发布（backend/app/recipes/loader.py 默认读 /app/recipes）。
# 漏了这一行 recipes 接口会返回空列表、一键训练直接 404，已踩过。
COPY recipes /app/recipes
COPY --from=frontend /build/dist /app/static

# 历史库目录；compose 或 -v 挂载后持久化。
# 以非 root 运行：hostPath 形式的 PVC 不会被自动 chown，部署前必须把
# /data/vla-platform/history 的属主改成 10001:10001（见 docs/operations.md）。
RUN mkdir -p /data/history \
    && useradd --create-home --uid 10001 --user-group vla \
    && chown -R vla:vla /data

USER vla

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
