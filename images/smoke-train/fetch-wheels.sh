#!/usr/bin/env bash
# 预下载 smoke 镜像需要的 torch wheel。
#
# 为什么需要这一步：官方 pytorch 源在本网络约 35 KB/s，908 MB 要下 7 小时；
# 阿里云 pytorch-wheels 镜像可达 18 MB/s。该镜像是普通文件目录（不是 pip index），
# 所以必须先下载到构建上下文，再由 Dockerfile 从本地文件安装。
#
# 用法（在仓库根目录）：
#   bash images/smoke-train/fetch-wheels.sh
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
WHEEL_DIR="$SCRIPT_DIR/wheels"
MIRROR="${PYTORCH_WHEEL_MIRROR:-https://mirrors.aliyun.com/pytorch-wheels/cu124}"
TORCH_WHEEL="torch-2.5.1+cu124-cp311-cp311-linux_x86_64.whl"

mkdir -p "$WHEEL_DIR"
target="$WHEEL_DIR/$TORCH_WHEEL"

if [[ -f "$target" ]]; then
  printf '已存在，跳过下载：%s (%s)\n' "$target" "$(du -h "$target" | cut -f1)"
  exit 0
fi

printf '下载 %s …\n' "$TORCH_WHEEL"
# -C - 支持断点续传：下载中断后重跑不会从头开始
if ! curl -fL --retry 5 -C - -o "$target" "$MIRROR/$(printf '%s' "$TORCH_WHEEL" | sed 's/+/%2B/g')"; then
  rm -f "$target"
  printf '下载失败。可设置 PYTORCH_WHEEL_MIRROR 指向其他镜像，例如官方源：\n' >&2
  printf '  PYTORCH_WHEEL_MIRROR=https://download.pytorch.org/whl/cu124 bash %s\n' "$0" >&2
  exit 1
fi

printf '完成：%s (%s)\n' "$target" "$(du -h "$target" | cut -f1)"
printf '现在可以构建：buildah bud -t vla-smoke:latest images/smoke-train\n'
