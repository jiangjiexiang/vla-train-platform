# SmolVLA 训练镜像

lerobot 0.4.4 + torch 2.6.0+cu124，单卡 4090 微调 SmolVLA。

## 构建

```bash
# 1. 预下载 torch wheel（约 1 分钟，阿里云 pytorch-wheels）
bash fetch-wheels.sh

# 2. 构建
buildah bud --isolation=chroot -t vla-smolvla:latest .

# 3. 导入 K3s
buildah push localhost/vla-smolvla:latest \
  docker-archive:/tmp/vla-smolvla.tar:docker.io/library/vla-smolvla:latest
k3s ctr -n k8s.io images import /tmp/vla-smolvla.tar
```

## 为什么这么写（都是踩过的坑）

**torch 必须用预下载的 cu124 wheel。** 交给 pip 解析会选 PyPI 上最新的 CPU 版
（2.14.x），镜像里没有 CUDA，训练必然失败。而 lerobot 声明 `torch>=2.2.1,<2.11`，
版本区间很宽，pip 完全可能选错。

**lerobot 用 `--no-deps` 装，依赖按它 METADATA 的 `Requires-Dist` 显式列出。**
它依赖 `pynput` → `evdev`，后者是 Linux 输入设备库，编译需要内核头文件；
训练用不到（那是遥操作用的），装它会让构建直接失败。

**注入 `torchcodec_guard/sitecustomize.py`。** lerobot 0.4.4 钉死的 torchcodec
版本在 torch 2.6 上全部加载失败，而它的默认后端判定只看 `find_spec`，
于是训练跑到采第一个 batch 才抛 `Could not load libtorchcodec`——
报错会把人引向「是不是缺 ffmpeg」。guard 让 find_spec 返回 None，
lerobot 就会明确打印「回落 pyav」并正常训练。

**训练入口自己写（`train_vla.py`），不直接调 lerobot CLI。** 三个原因：
平台用 `DATASET_ROOT` 传数据集、`/checkpoints/<任务名>/` 存产物，
lerobot 没有这套约定；需要训练前做环境自检（跑了半小时才因为没有 CUDA 失败
是最糟的）；需要把 lerobot 的必填项错误翻译成人话。

## 参数与配方对应

`train_vla.py` 的参数名与 `recipes/smolvla-finetune.yaml` 一一对应，
改一边要同步改另一边。参数用下划线风格（`--batch_size`），
写中划线会被 lerobot 当未知参数拒绝。

## 显存实测（RTX 4090 48G，3 路相机）

| batch size | 峰值占用 |
|---|---|
| 16 | 7.48 GiB |
| 64 | 24.79 GiB |
| 128 | 46.36 GiB（上限） |
| 144+ | OOM |

近似 `bs × 0.35 + 1.4 GiB`，生产建议 bs=32~64。

## 已知限制

- 模型权重（约 2.8 GB）未预置进镜像，每个新任务首次运行会从 hf-mirror 下载。
- lerobot 的 loss 写在自己的日志里，尚未转成平台的 `metrics.jsonl`。
