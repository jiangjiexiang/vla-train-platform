# web 镜像的预下载 wheel

`images/web-image/` 只有一个用途：让 `Dockerfile` 的 **`dependencies` 阶段**不用每次
都从 PyPI 重新下载全部依赖。

```
images/web-image/
├── fetch-wheels.sh   ← 下载器
├── wheels/           ← 由 fetch-wheels.sh 生成，Dockerfile 会 COPY 进去；不进版本库
└── README.md
```

## 为什么需要

`Dockerfile` 的依赖层原文是：

```dockerfile
RUN pip install --no-cache-dir --prefix=/install -r /app/requirements.txt
```

pip 每次都从零开始拉包。改 `backend/requirements.txt` 里的任何一行（比如加
`tensorboard==2.21.0`），Dockerfile 的 `COPY backend/requirements.txt` 层缓存失效，
**整份依赖重新下载**：grpcio 单个就 7MB+，加上 tensorboard 拉入的 protobuf /
markdown / werkzeug 一整套，国内网络下 1 分钟能变 10 分钟。

预下载成本只有一次，存成 wheel 后 pip 走 `--no-index --find-links` 从本地装，
改 requirements 只装变动的包。

> 注：`buildah 1.23.1` **不支持** `RUN --mount=type=cache`，会报
> `invalid mount type "cache"`。所以这里走的是与 `images/smolvla-train/` 相同的
> 预下载 wheel 方案，不要改回去。

## 用法

```bash
cd /path/to/vla-train-platform

# 1. 预下载（首次约 1-2 分钟；重跑会复用已下好的 wheel）
bash images/web-image/fetch-wheels.sh

# 2. 构建（wheels/ 会被 COPY 进去并优先离线安装）
buildah bud --isolation=chroot -t vla-platform:local .
```

Docker 用户把 `buildah bud` 换成 `docker build` 即可，Dockerfile 是同一份。

## 什么时候要重跑

**改了 `backend/requirements.txt` 就跑一次。** 没重跑也能构建成功，只是 pip 会
在离线安装失败后降级为在线下载（Dockerfile 里有兜底），那就失去意义了。

判断缺哪些 wheel：

```bash
# 构建日志里搜这两行之一
grep -E "使用预下载 wheel|降级为在线安装" build.log
```

## 实现要点（改动前先看）

- **目标 ABI 必须是 cp312**：`Dockerfile` 用的是 `python:3.12-slim`，但构建机宿主机
  可能是 Python 3.10。直接 `pip download` 会下成 cp310，构建时装不上，pip 还会
  含糊地报「找不到该包」。脚本用
  `--python-version 312 --implementation cp --abi cp312 --platform manylinux*` 锁定目标环境。
- **不能加 `--no-deps`**：requirements 只钉顶层包，传递依赖（grpcio、protobuf、
  numpy、pyarrow…）才是下载大头，必须让 pip 一起解析下载。
- **`--only-binary=:all:`**：只提供 sdist 的包在这里会直接失败，比等到构建时才发现好。
  确实需要 sdist 的包，去掉这个参数或单独处理。
- **`--find-links "$WHEEL_DIR"`**：重跑复用已有 wheel，不会重复下载。
- `wheels/` 里如果完全没有 wheel（比如新 clone 的仓库），Dockerfile 的 `ls *.whl`
  判断会失败并降级为在线安装，**构建不会因此失败**。
