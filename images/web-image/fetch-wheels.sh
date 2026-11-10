#!/usr/bin/env bash
# 预下载 web 镜像（Dockerfile 的 dependencies 阶段）需要的全部 wheel。
#
# 为什么需要这个：pip 每次 `--prefix=/install -r requirements.txt` 都从零开始
# 拉包，改一个版本（比如这次加 tensorboard）等于把 grpcio、numpy、pyarrow
# 全部重下一遍——国内网络下构建从 1 分钟变成 10 分钟。预下载后 pip 走
# `--no-index --find-links`，改 requirements 只需装上变动的包。
#
# 踩过的坑（与 images/smolvla-train/fetch-wheels.sh 同源）：
#
# 1. wheel 必须与容器的 Python ABI 匹配（Dockerfile 是 python:3.12-slim → cp312）。
#    构建机宿主机可能是 3.10，直接 `pip download` 会下成 cp310，构建时装不上，
#    而且 pip 只会含糊地报「找不到该包」。这里用
#    --python-version/--implementation/--abi/--platform 显式指定目标环境。
#
# 2. 加了这些参数后 pip 只接受「带平台标签的 wheel」，纯 Python 包
#    （py3-none-any）仍然可选中，不需要额外处理。只提供 sdist 的包会直接失败——
#    那种包只能回到在线安装，--only-binary=:all: 让它在下载阶段就报出来，
#    而不是等到构建时才发现。
#
# 3. 不要加 --no-deps：requirements.txt 只钉了顶层包（kubernetes、tensorboard），
#    传递依赖（grpcio、protobuf、numpy、pyarrow…）才是下载耗时的大头，必须一起下。
#
# 4. --find-links "$WHEEL_DIR" 让重跑复用已有 wheel：已下过的包不会重下，
#    requirements 变了只补新增/变更的包。
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
WHEEL_DIR="$SCRIPT_DIR/wheels"
REQUIREMENTS="${REQUIREMENTS:-$REPO_ROOT/backend/requirements.txt}"
PYPI_INDEX="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
PIP="${PIP:-pip3}"

# 目标环境（与 Dockerfile 的 FROM python:3.12-slim 一致，改 Dockerfile 要同步这里）
TARGET_PY=312
TARGET_ABI=cp312
TARGET_PLATFORMS=(--platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64)

if [[ ! -f "$REQUIREMENTS" ]]; then
  printf '找不到 requirements 文件：%s\n' "$REQUIREMENTS" >&2
  exit 1
fi

mkdir -p "$WHEEL_DIR"

printf '使用 pip：%s\n' "$("$PIP" -V)"
printf '目标环境：cp%s / manylinux x86_64（与 Dockerfile 的 python:3.12-slim 对齐）\n' "$TARGET_PY"
printf 'requirements：%s\n' "$REQUIREMENTS"
printf 'wheel 目录：%s\n\n' "$WHEEL_DIR"

"$PIP" download --dest "$WHEEL_DIR" \
  --index-url "$PYPI_INDEX" \
  --find-links "$WHEEL_DIR" \
  --python-version "$TARGET_PY" --implementation cp --abi "$TARGET_ABI" \
  --only-binary=:all: "${TARGET_PLATFORMS[@]}" \
  -r "$REQUIREMENTS"

# 兜底清理：混进来的非 cp312 二进制 wheel 一律删掉（纯 Python 的 py3-none-any 保留）。
find "$WHEEL_DIR" -name '*-cp3*.whl' ! -name '*-cp312-*' -print -delete 2>/dev/null || true

printf '\n完成。目录：%s (%s)\n' "$WHEEL_DIR" "$(du -sh "$WHEEL_DIR" | cut -f1)"
printf 'wheel 数量：%s\n' "$(find "$WHEEL_DIR" -name '*.whl' | wc -l)"
printf '现在可以构建：buildah bud --isolation=chroot -t vla-platform:local .\n'
