#!/usr/bin/env bash
# 预下载 SmolVLA 训练镜像需要的全部 wheel。
#
# 三个必须绕开的坑（每一个都实际踩过）：
#
# 1. torch 不能交给 pip 解析：它会选 PyPI 上最新的 CPU 版 torch（现在是 2.14.1），
#    镜像里没有 CUDA，训练必然失败。torch/torchvision 必须从 pytorch-wheels
#    文件目录单独下载（带 +cu124 本地版本号）。
# 2. 也不能把 `torch==2.6.0+cu124` 放进 lerobot 的依赖解析：本地版本号在
#    PEP 440 下不满足 `torch>=2.2.1,<2.11` 区间约束，pip 直接 ResolutionImpossible。
# 3. wheel 必须与容器的 Python ABI 匹配（python:3.11-slim → cp311）。宿主机是
#    3.10，直接 `pip download` 会下成 cp310，构建时装不上、pip 还会假装找不到该包。
#    这里用 --python-version/--abi/--platform 显式指定目标环境。
#    但加了这些参数后 pip 只接受「带平台标签的 wheel」，纯 Python 包（py3-none-any）
#    仍然可选中——不需要额外处理。
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
WHEEL_DIR="$SCRIPT_DIR/wheels"
PYTORCH_MIRROR="${PYTORCH_WHEEL_MIRROR:-https://mirrors.aliyun.com/pytorch-wheels/cu124}"
PYPI_INDEX="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
PIP="${PIP:-pip3}"

# 目标环境（与 Dockerfile 的 FROM python:3.11-slim 一致）
TARGET_PY=311
TARGET_ABI=cp311
TARGET_PLATFORMS=(--platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64)

mkdir -p "$WHEEL_DIR"

# ---------- 1. torch / torchvision（cu124）----------
TORCH_FILES=(
  "torch-2.6.0+cu124-cp311-cp311-linux_x86_64.whl"
  "torchvision-0.21.0+cu124-cp311-cp311-linux_x86_64.whl"
)
for file in "${TORCH_FILES[@]}"; do
  target="$WHEEL_DIR/$file"
  [[ -f "$target" ]] && { printf '已存在 %s\n' "$file"; continue; }
  encoded=$(printf '%s' "$file" | sed 's/+/%2B/g')
  printf '下载 %s\n' "$file"
  curl -fL --retry 5 --retry-delay 3 -C - -o "$target" "$PYTORCH_MIRROR/$encoded" \
    || { rm -f "$target"; printf '下载失败：%s\n' "$PYTORCH_MIRROR/$encoded" >&2; exit 1; }
done

# ---------- 2. torch 的 CUDA 运行库 ----------
# 版本号直接取自 torch wheel 的 METADATA（Requires-Dist）。少一个，torch 就 import 不了。
printf '\n下载 torch 的 CUDA 运行库…\n'
CUDA_DEPS=(
  "nvidia-cuda-nvrtc-cu12==12.4.127"
  "nvidia-cuda-runtime-cu12==12.4.127"
  "nvidia-cuda-cupti-cu12==12.4.127"
  "nvidia-cudnn-cu12==9.1.0.70"
  "nvidia-cublas-cu12==12.4.5.8"
  "nvidia-cufft-cu12==11.2.1.3"
  "nvidia-curand-cu12==10.3.5.147"
  "nvidia-cusolver-cu12==11.6.1.9"
  "nvidia-cusparse-cu12==12.3.1.170"
  "nvidia-cusparselt-cu12==0.6.2"
  "nvidia-nccl-cu12==2.21.5"
  "nvidia-nvtx-cu12==12.4.127"
  "nvidia-nvjitlink-cu12==12.4.127"
  "triton==3.2.0"
)
"$PIP" download --quiet --dest "$WHEEL_DIR" --index-url "$PYPI_INDEX" --no-deps \
  --python-version "$TARGET_PY" --implementation cp --abi "$TARGET_ABI" \
  --only-binary=:all: "${TARGET_PLATFORMS[@]}" "${CUDA_DEPS[@]}"

# ---------- 3. lerobot 本体（不解析依赖）----------
printf '\n下载 lerobot 本体…\n'
"$PIP" download --quiet --dest "$WHEEL_DIR" --index-url "$PYPI_INDEX" --no-deps \
  --python-version "$TARGET_PY" --implementation cp --abi "$TARGET_ABI" \
  --only-binary=:all: "${TARGET_PLATFORMS[@]}" "lerobot==0.4.4"

# ---------- 4. lerobot 的运行依赖 ----------
printf '\n下载运行依赖…\n'
RUNTIME_DEPS=(
  "torchcodec==0.10.0"
  "accelerate>=1.7,<2"
  "transformers>=4.57.1,<5"
  "draccus"
  "num2words"
  "safetensors"
  "datasets"
  "huggingface_hub"
  "av"
  "numpy<2.3"
  "einops"
  "gymnasium"
  "wandb"
  "imageio"
  "pillow"
  "opencv-python-headless"
  "pyyaml"
  "termcolor"
  "tqdm"
  "scipy"
  "matplotlib"
  "pandas"
  "jsonlines"
  "packaging"
  "filelock"
  "typing-extensions"
  "networkx"
  "jinja2"
  "fsspec"
  "sympy==1.13.1"
)

failed=()
for spec in "${RUNTIME_DEPS[@]}"; do
  if ! "$PIP" download --quiet --dest "$WHEEL_DIR" --index-url "$PYPI_INDEX" \
      --find-links "$WHEEL_DIR" \
      --python-version "$TARGET_PY" --implementation cp --abi "$TARGET_ABI" \
      --only-binary=:all: "${TARGET_PLATFORMS[@]}" "$spec"; then
    failed+=("$spec")
    printf '  下载失败：%s\n' "$spec" >&2
  fi
done

# 兜底清理：任何 cp310 的包都不该出现在这里
rm -f "$WHEEL_DIR"/*cp310*.whl 2>/dev/null || true

printf '\n完成。目录：%s (%s)\n' "$WHEEL_DIR" "$(du -sh "$WHEEL_DIR" | cut -f1)"
printf 'wheel 数量：%s\n' "$(find "$WHEEL_DIR" -name '*.whl' | wc -l)"
if (( ${#failed[@]} > 0 )); then
  printf '以下包下载失败：%s\n' "${failed[*]}" >&2
  exit 1
fi
printf '现在可以构建：buildah bud -t vla-smolvla:latest images/smolvla-train\n'
