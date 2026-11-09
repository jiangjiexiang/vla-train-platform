# 企业级训练平台交互调研：如何让用户「选数据集 → 点一下 → 进队列」

> 调研日期：2026-11-09　｜　方法：以官方文档与官方示例代码为主，二手材料仅作补充并已标注
> 目标场景：公司内部算法团队，单机 RTX 4090、LeRobot 格式数据集、Kueue 排队、PyTorchJob 执行

---

## 0. 先说三个改变结论的实地发现（务必先读）

调查中翻到几条「事实与常识不符」的信息，直接影响方案选型：

1. **Hugging Face AutoTrain 已停止维护。** 官方文档页现在直接挂出："This project is no longer maintained. No new features will be added and bugs will not be fixed. We recommend using Axolotl, TRL, or transformers.Trainer."
   来源：<https://huggingface.co/docs/autotrain/index>（经镜像 <https://hf-mirror.com/docs/autotrain> 访问）
   → **不要再把 AutoTrain 当作参考对象**。它留下的有价值遗产是它的**交互骨架**（见 §1.2），可以借鉴交互，但不要抄它的技术栈，也不要在方案里写"对标 AutoTrain"。

2. **Kubeflow Katib 已弃用。** 官方文档顶部横幅："The Katib project is deprecated. Users should migrate to the OptimizationJob APIs, which are released as part of the Kubeflow Trainer project under alpha feature gate."
   来源：<https://www.kubeflow.org/docs/components/katib/user-guides/katib-ui/>
   → Katib UI 的价值是**反例**（8 个 tab 的表单流程），不是正例。正向参考应看 **Kubeflow Trainer 的 `TrainJob` + `Runtime` 预置**。

3. **SageMaker 的 "Experiment" 层级本身也在迁移。** 官方现已推荐用 MLflow 集成替代 Experiments Classic（"Experiment tracking using the SageMaker Experiments Python SDK is only available in Studio Classic… We recommend… creating experiments using the latest SageMaker AI integrations with MLflow"）。
   来源：<https://docs.aws.amazon.com/sagemaker/latest/dg/experiments.html>
   → 做实验管理时，**别自己发明一套 experiment/run 数据模型**，优先用 MLflow（或至少兼容 MLflow 的 run 语义）。

另外两条影响落地的事实：

4. **国内产品里最贴近我们需求的机制是阿里云 PAI-DLC 的「任务模板 + 只读锁定」**：可以把验证过的训练配置沉淀成模板，**锁定**镜像/资源/挂载路径，只开放超参和启动命令给使用者。这正是「选数据集点一下开跑」需要的抽象（见 §1.6）。
5. **Kueue 原生不提供「排队位置」和「预计等待时间」。** 文档里只有排序规则（优先级 → 创建时间，或 BestEffortFIFO）、准入状态、驱逐/重排状态，没有 position/ETA 字段。这两项必须我们**自己在产品层计算**（做法见 §5.5）。

---

## 1. 各产品关键交互拆解

### 1.1 Hugging Face Jobs —— 用「代码即配置」替代表单

**核心抽象：`hf jobs` 命令行 + `--flavor` 硬件档 + `hf://` 数据集挂载。**

| 维度 | 做法 | 出处 |
|---|---|---|
| 启动方式 | `hf jobs uv run --with trl --flavor a10g-small -- script.py --dataset_name ...`，选项与脚本参数用 `--` 分隔 | [HF Jobs Configuration](https://huggingface.co/docs/hub/jobs-configuration) |
| 配置写在脚本里 | 脚本头部 PEP 723 的 `[tool.hf-jobs]` 表可声明 `image / flavor / python / timeout / name / namespace / env / secrets / labels / volumes`；**显式 CLI 参数永远覆盖脚本默认值**，`env/secrets/labels/volumes` 逐条合并 | 同上 |
| 数据集引用 | `-v hf://datasets/<user>/<ds>:/data:ro`，或 Python `Volume(type="dataset", source="user/ds", mount_path="/data")` | 同上 |
| 硬件选择 | `hf jobs hardware` 直接打印一张表：name / pretty name / cpu / ram / storage / accelerator / **cost-per-hour**。例如 `a10g-small`=1×A10G(24G)/$1.00，`t4-small`=1×T4(16G)/$0.40 | 同上 |
| 产物回收 | 挂载可写卷，任务结束后 CLI 打印 `hf buckets sync` 命令把产物同步回来 | 同上 |
| 命名空间 | 通过 `namespace` 字段隔离到个人 / 组织 | 同上 |

**可借鉴点**：
- `--flavor` 是**把"机型/显存/CPU/价格"打包成一个人类可读的档位名**，用户只需选 `a10g-small` 这种词，不需要填 CPU 核数、内存、GPU 型号三处。这对我们"只有 4090 一种卡"的场景同样适用——把它包装成 `rtx4090-1x`。
- `[tool.hf-jobs]` 的 "脚本自带默认值 + 命令行覆盖 + 合并语义" 是**"训练配方 recipe" 的经典实现**：配方属于代码仓库，平台负责渲染成表单。

**局限**：Jobs 是 CLI-first，Web 端交互很薄；它**没有**数据集可用性预检，失败靠日志。

---

### 1.2 Hugging Face AutoTrain —— 已停维，但「任务类型 → 数据集 → 参数」三段式值得记

AutoTrain 的定位是「不用代码的人也能训练」，其交互骨架是：

1. **选任务类型**（NLP / CV / 语音 / 表格 → 再细分，如Σ分类、抽取式问答、目标检测、PaliGemma 微调…）
2. **选数据集**（Hub 上的数据集，按任务类型过滤）
3. **选基础模型**
4. **填少数训练参数**（epoch、batch、lr）
5. **选硬件 → 启动**

出处：<https://huggingface.co/docs/autotrain/index>（镜像可访问版本）

**可借鉴点**：第 1 步「任务类型」承担了**过滤与默认值注入**的职责——选了任务类型，数据集候选、模型候选、默认超参就都定了。这正是"用户什么都不用配"的前提。**但它的缺点是任务类型是硬编码枚举**，加一个新算法要发版。

> 状态提醒：该项目已停止维护，官方推荐 Axolotl / TRL / transformers.Trainer。我们只借交互骨架，不借技术。

---

### 1.3 Weights & Biases Launch —— 目前最完整的「任务模板 + 参数治理」范式

这是我调研到的**信息密度最高**的产品，直接对应"配方/模板"与"高级参数收纳"两件事。

**核心概念链**：`Job`（一个带版本的任务定义）→ `Queue`（FIFO 有序列表，绑一个 target resource）→ `Agent`（拉取并执行）→ `Run`（执行记录）。

| 环节 | 具体交互 | 出处 |
|---|---|---|
| Job 的三种形态 | artifact-based（代码打包）、git-based（按 commit/branch/tag 克隆）、image-based（烧进镜像） | [Launch terms](https://docs.wandb.ai/guides/launch/launch-terminology) |
| Job 有版本 | "Launch jobs are versioned like any other W&B artifact"，改了依赖或源码就产生新版本，提交时用 **Job version 下拉框**选 | [Add job to queue](https://docs.wandb.ai/guides/launch/add-job-to-queue) |
| 提交弹窗字段链 | Job version → **Overrides**（entrypoint / args / run_config）→ **Paste from…**（从上一次 run 粘贴参数）→ Queue → **Job Priority** 下拉（队列不支持优先级时自动置 Medium）→ **Queue Configurations**（管理员定义的模板字段，如只能从 `ml.m4.xlarge` / `ml.p3.xlarge` 里选）→ **Destination project** → Launch now | 同上 |
| **只暴露该改的参数** | SDK：`launch.manage_wandb_config(include=["trainer"], exclude=["trainer.private"])`，或用 `launch.manage_config_file("config.yaml")` 把一个 YAML 当输入。结果：UI 里**只出现 include 命中的 4 个参数** | [Manage job inputs](https://docs.wandb.ai/guides/launch/job-inputs) |
| **管理员护栏** | 队列配置里写 `{{variable}}` 占位符 → Parse configuration → 为每个占位符生成一个 tile，指定 type（string/int/float）与**默认值、最小值、最大值**；成员只能在范围内改 | [Configure launch queue](https://docs.wandb.ai/guides/launch/setup-queue-advanced) |
| 动态注入 | 队列配置支持 `${project_name} ${entity_name} ${run_id} ${run_name} ${image_uri}` 宏，以及任意环境变量 | 同上 |
| 队列监控 | 交互式 dashboard 用来发现低效作业与资源使用 | [Monitor launch queue](https://docs.wandb.ai/guides/launch/launch-queue-observability) |

**可借鉴点（重要性从高到低）**：
1. **`include/exclude` 白名单**：参数的"可见性"由代码声明，平台只渲染白名单 → 天然实现了"高级参数默认隐藏"。
2. **`{{variable}}` + min/max 护栏**：管理员在队列层定义"哪些字段可改、可改到什么范围"，而不是把所有字段丢给用户。
3. **`Paste from…`**：一键从历史 run 复制参数，是"复现实验"的最低成本交互。
4. **Job 版本号**：训练配方是有版本的，不是每次现填。

**局限**：队列是 FIFO，没有位置/ETA；Overrides 是一坨 key-value，不像表单；W&B 文档现已迁到 CoreWeave 域名（`docs.coreweave.com/products/wandb/...`），`docs.wandb.ai` 会 307 跳转。

---

### 1.4 AWS SageMaker（Training Jobs / HyperPod）—— 「表单分层」与「配方文件」两条路

#### (a) 普通 Training Job 的表单分层

SageMaker 控制台的 Create Training Job 按左栏分节，典型分节为：

`Job name/描述` → `算法来源（算法/自带代码 + 镜像）` → `超参` → `输入数据通道（S3 / File·Pipe·FastFile 模式）` → `输出与 Checkpoint` → `资源（instance type × instance count）` → `网络与存储（VPC、KMS、加密）` → `停止条件（MaxRuntimeInSeconds）` → `标签`

其中真正**必填**的只有：Job 名、镜像/入口、训练实例规格与数量、输入数据位置、输出位置。其余全是"不填就用默认"。

出处：[Model training 概览](https://docs.aws.amazon.com/sagemaker/latest/dg/train-model.html)（Before/During/After 三段式、输入模式 File/Pipe/FastFile、配额不足时报 `ResourceLimitExceeded`）

**可借鉴点**：左栏**分节锚点**比长滚动好；每节都有"默认已填"的状态，用户只需改 1–2 节。

#### (b) HyperPod Recipes —— 「配方文件 + 少量外层参数」

HyperPod 的 recipe 是一个 YAML，外层只需改 4 个东西：

```yaml
defaults:
  - _self_
  - cluster: slurm
  - recipes: training/llama/hf_llama3_8b_seq8192_gpu
instance_type: p5.48xlarge
git: { repo_url_or_path, branch, commit, entry_script, token }
env_vars: { NCCL_DEBUG: WARN }
```

recipe 内部定义：是否模型并行、**数据集来源**、混合精度、checkpoint 策略；`run:` 段有 `name / results_dir / time_limit / model_type`；`exp_manager:` 有 `exp_dir / name / create_tensorboard_logger` 与 `auto_checkpoint / checkpoint_callback_params / resume_from_checkpoint / export_full_model`。

出处：[HyperPod recipes](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-recipes)、[General configuration](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-recipes-general-configuration)

**可借鉴点（关键）**：
- **"外层 4 参数 / 内层 N 参数"的两层结构**：用户只碰外层，内层由配方作者维护。这比"一个 80 字段的表单折叠起来"更干净。
- `time_limit`、`resume_from_checkpoint`、`auto_checkpoint` 是**一等公民字段**，不是埋在 JSON 里。我们的场景（4090 单卡长训被 Kueue 抢占）**必须**把它们放到一等位置。
- `max_retries`/重试语义在 Anyscale 也有：作业失败**从头重跑**，不是断点续训——所以 `resume_from_checkpoint` 必须显式建模。

#### (c) 任务级诊断（SageMaker Debugger）

SageMaker Debugger 内置规则：`all_zero`、`class_imbalance`、`loss_not_decreasing`、`overfit`（默认 ratio_threshold=0.1、patience=1）、`overtraining`、`similar_across_runs`、`stalled_training`（默认 1800s 无 tensor 输出即判定）、`tensor_variance`、`unchanged_tensor`。
出处：<https://docs.aws.amazon.com/sagemaker/latest/dg/debugger-built-in-rules>

**可借鉴点**：这是"**结构化错误分类**"最具体的范本——把"训练看起来不对"拆成 9 个有阈值、可解释的规则，而不是让用户读日志。

---

### 1.5 Google Vertex AI Training —— 参数面最小的自定义容器

官方 notebook 里的一次提交，参数就这些（`CustomPythonPackageTrainingJob` / `CustomJob`，`worker_pool_specs`）：

| 字段 | 说明 |
|---|---|
| `display_name` | 作业显示名 |
| `container_spec.image_uri` | 自定义容器镜像 |
| `container_spec.command` / `args` | 入口与命令行（超参通常走 args） |
| `replica_count` | 副本（机器）数 |
| `machine_type` | 机型，如 `n1-standard-4` |
| `accelerator_type` / `accelerator_count` | GPU/TPU 型号与数量 |
| `base_output_dir` | GCS 输出目录 |

`worker_pool_specs` 支持多池：主池（CPU）+ 训练池（GPU）+ 参数服务器池，每池各自 `replica_count/machine_spec/container_spec`。

出处（官方样例 notebook）：<https://raw.githubusercontent.com/GoogleCloudPlatform/vertex-ai-samples/main/notebooks/official/training/get_started_with_vertex_distributed_training.ipynb>（raw 已验证可访问；github.com 的 blob 页在本机被阻断）

> ⚠️ 访问说明：`cloud.google.com` 与 `huggingface.co` 在本机网络被阻断，Vertex AI 的官方文档站与示例 HiperparameterTuningJob 的 `parameter_spec`（`scale_type: UNIT_LINEAR_SCALE / categorical / integer` + `max_trial_count / parallel_trial_count / goal`）**未能直接抓取原文**，此节结论基于官方 GitHub 样例 notebook（可访问）与公开 API 形态的常识，引用粒度弱于其他产品，落地时请以最终文档为准。

**可借鉴点**：**把"超参搜索"当成 job 的一个可选属性**（trial 数、并行度、目标指标、搜索空间），而不是另起一个"调参"产品。Katib 也是这个方向（`objective / algorithm / parallelTrialCount / maxTrialCount / maxFailedTrialCount / parameters`，见 [configure-experiment](https://www.kubeflow.org/docs/components/katib/user-guides/hp-tuning/configure-experiment/)）。

---

### 1.6 阿里云 PAI-DLC —— 国内最贴近「任务模板」的实现

**（a）创建任务表单的字段清单**（节选自官方「创建训练任务」）：

- 任务名称（必填，1–200 字符）
- 描述 / 标签（规范化打标 tag）
- **镜像配置**：官方镜像 / 自定义镜像 / 镜像地址（私有需账密）+ 镜像加速
- **数据集挂载**：自定义数据集（**支持在版本列表中选择数据集版本**、可设只读）/ 公共数据集（只读）+ **挂载路径**（如 `/mnt/data`）
- 代码源（Git / 代码文件 / OSS）、启动命令、超参、环境变量
- **资源配额 / 资源组 / 优先级 [1,9]**
- 亲和性 / VPC / 最长运行时间

出处：<https://help.aliyun.com/zh/pai/create-a-training-task>

**（b）任务模板 —— 本报告最重要的机制**

> "DLC 提供"任务模板"功能：可将经过验证的训练配置沉淀为模板，**预设并可锁定**镜像、资源、挂载路径等关键项，**仅开放超参数、启动命令等必要可调配置**。"
> "配置任务**只读规则**。可将预填的默认值设置为只读，基于该模板创建的任务将无法修改只读配置项，防止误改关键参数。"
> "建议将**描述**设为必填项，强制创建任务时描述参数调整信息……可在模板描述中定义填写范式（如"实验方向+调整参数"）。"
> "模板的使用记录…克隆基于模板创建的任务，不会展示在模板的使用记录中。"

出处：<https://help.aliyun.com/zh/pai/use-task-templates-and-comparisons-to-improve-tuning-efficiency>

**（c）任务运行页**：概览（状态 / 已运行时长 / 计算类型 / 任务类型 / 资源组 + **从创建→环境准备→运行→成功的阶段时间轴**）、**事件页签**（左侧事件时间轴，右侧事件日志：Job 排队、出队、Pod 创建、调度成功、运行中…；系统日志含 `ResourcePurchasing → NetworkInitializing → Initializing → ImagePulling → WaitingForRun → Running → Succeeded`）、**资源视图**（GPU 使用量 / GPU Memory / CPU / Memory / Network I/O）、日志。
出处：<https://help.aliyun.com/zh/pai/view-training-jobs>

**（d）资源配额**：树形（父配额 / 子配额），可扩缩容。
出处：<https://help.aliyun.com/zh/pai/manage-resource-quotas>

---

### 1.7 火山引擎 veMLP —— 「排队诊断」与「优先级策略」做得最细

**（a）创建训练任务表单**——官方标注的**必填项只有 5 个**：任务名称、镜像、入口命令、资源来源（队列/预约计划）、实例配置（训练模板 + 实例规格）。其余为选填：描述、标签、可见范围、源代码、参数、环境变量、TensorBoard、优先级（**1–9，提交时只开放部分档位**）、使用闲时资源、最长运行时间。
出处：<https://www.volcengine.com/docs/ml-platform/single-node-distributed-training-task>

**（b）优先级调度策略**：数字越大越优先；同优先级按创建时间；**默认策略是"资源利用率最大化"**，小资源任务先被调度（案例：1c2g 的 B 先于 8c16g 的 A 运行）；为此提供**资源预留**（低优任务持续排队直到该任务跑起来）与**闲时任务**（可被抢占的借用资源，但"非闲时任务获得资源的优先级始终高于闲时任务，与优先级数字无关"）。
出处：<https://www.volcengine.com/docs/ml-platform/priority-scheduling-policy>

**（c）「排队中」状态的 5 类根因 + 逐条提示文案**（这是 D 题最直接的范本）：

| 根因 | 界面提示（原文摘录） |
|---|---|
| 资源碎片 | "当前队列中有配额，可能存在碎片问题。您可观察队列是否已开启 GPU 碎片整理…" |
| 高优任务资源预留 | "当前有高优先级任务资源预留中，您可以选择继续等待其他资源释放或者将任务变为闲时任务…（有随时被抢占风险）" |
| 个人剩余配额不足 | "当前个人配额用量剩余不足，您可以联系队列管理员在「队列管理-组内用户」调整用量比例上限…" |
| 队列剩余配额不足 | "当前队列剩余配额不足，您可以选择继续等待资源释放或者将任务变更为闲时任务…" |
| 资源组剩余配额不足 | "当前资源组中配额被占用无法释放，您可以将任务变更为非闲时任务进行抢占或者继续等待…" |

出处：<https://www.volcengine.com/docs/ml-platform/common-causes-of-load-queuing-status>

**可借鉴点**：每个"排队中"都不是一句话"等待资源"，而是 **根因分类 + 可执行的两个出路**。这是我们 UI 必须抄的一条。

---

### 1.8 Kubeflow / Katib —— UI 是反例，`TrainJob + Runtime` 是正例

**（a）Katib Create Experiment 的 8 个 tab（反例）**：Metadata → Trial Thresholds → Objective → Search Algorithm → Early Stopping → Hyper Parameters → Metrics Collector → Trial Template。
出处：<https://www.kubeflow.org/docs/components/katib/user-guides/katib-ui/>

这 8 个 tab 每一个都要求用户理解 Katib 内部模型（metrics collector sidecar、trialParameters 与 trialTemplate 的变量替换）。**任何"一键训练"都不该长这样**。

**（b）Katib 的正向遗产**：超参实验 = `objective(maximize/minimize + objectiveMetricName)` + `algorithm`（random/grid/bayesopt/hyperband…）+ `parallelTrialCount`（默认 3）+ `maxTrialCount` + `maxFailedTrialCount` + `parameters`（int/double/categorical + 分布）。
出处：<https://www.kubeflow.org/docs/components/katib/user-guides/hp-tuning/configure-experiment/>

**（c）Kubeflow Trainer（替代 Katib 的方向）**：两层 API ——
- **`Runtime`**（平台管理员维护）：定义镜像、分布式框架、环境。即"预置运行时/配方"。
- **`TrainJob`**（算法工程师填）：引用 runtime + 数据集 + 少量参数。即"选配方、选数据、开跑"。

Kubeflow Trainer 明确列出与 **Kueue**、KAI Scheduler、JobSet 的集成。
出处：<https://www.kubeflow.org/docs/components/trainer/overview/>

> **这就是我们要的抽象形态本身**：`Runtime ≈ 训练配方`，`TrainJob ≈ 一次提交`。

---

### 1.9 Modal / Anyscale / RunPod —— 极简启动的三种流派

| 产品 | 启动方式 | 极简在哪 | 可借鉴点 | 出处 |
|---|---|---|---|---|
| **Modal** | Python 装饰器 `@app.function()` + `modal run` | "没有 YAML、没有集群"——资源声明写在函数上；Volume 用 `modal.Volume`；超 24h 训练靠 checkpoint + retry 变可重入 | 把"资源声明"和"代码"放同一处；**长任务默认就是可恢复的**（`last.ckpt` 检测 → 续训） | [Getting started](https://modal.com/docs/guide/getting-started)、[Long resumable training](https://modal.com/docs/examples/long-training) |
| **Anyscale** | `anyscale job submit job_config.yaml`，或 UI 提交 | 计算配置（compute config）与镜像都有**版本号**，不填就用 workspace/cloud 默认；`job queue` 显式支持 `execution_mode: PRIORITY / FIFO / LIFO`，**priority 0 最高**，同优先级按到达顺序 | **队列的排序语义要做成用户可选的显式模式**；默认值来自工作区上下文 | [What are Anyscale jobs](https://docs.anyscale.com/jobs)、[Job queues](https://docs.anyscale.com/jobs/queues) |
| **RunPod** | 选 GPU → 选模板（预配置镜像）→ Deploy → SSH/Jupyter | 主交互是**硬件选型引导**：按工作负载给 GPU 档位与**最低显存**建议表（如 7B–13B 推理 ≥24GB；LLM 训练/微调推荐 A100/H100 40–80GB），并给 VRAM 估算规则（LLM 约 2GB/十亿参数） | **"按工作负载推荐硬件"的对照表**可直接搬到我们的显存预估提示 | [Choose a pod](https://docs.runpod.io/pods/choose-a-pod)、[Pods overview](https://docs.runpod.io/pods/overview) |

> RunPod 本质是"租机器"，不是"提交训练任务"，所以它的"极简"来自**把训练脚本留给用户自己跑**。这一点**不适合**我们的场景（内部平台必须可审计、可排队、可复现）。

---

### 1.10 Kueue —— 我们排队层的真实能力边界（决定 UI 能承诺什么）

| 能力 | 事实 | 出处 |
|---|---|---|
| 队列层级 | `LocalQueue`（namespaced，指向）→ `ClusterQueue`（集群级配额） | [Local Queue](https://kueue.sigs.k8s.io/docs/concepts/local_queue/)、[Cluster Queue](https://kueue.sigs.k8s.io/docs/concepts/cluster_queue/) |
| 排序 | `StrictFIFO`：先优先级，再创建时间；**队首跑不动的会挡住后面的**。`BestEffortFIFO`（默认）：顺序相同，但跑不动的不会挡住能塞进来的 | Cluster Queue 页 |
| 优先级 | 两套：Pod 的 `PriorityClass` 与 Kueue 的 `WorkloadPriorityClass`（互不影响，都设时 WorkloadPriorityClass 管排队、PriorityClass 管 Pod）。作业上打 label `kueue.x-k8s.io/priority-class`；队列用 `kueue.x-k8s.io/queue-name` | [Workload Priority Class](https://kueue.sigs.k8s.io/docs/concepts/workload_priority_class/) |
| 准入 | Workload 的 `podSets[].count` × 资源需求 计算配额；超 LimitRange 会标 `Inadmissible` | [Workload](https://kueue.sigs.k8s.io/docs/concepts/workload/) |
| 驱逐 | `status.schedulingStats.evictions[]` = `reason`（`Preempted` / `PodsReadyTimeout` / `Deactivated`）+ `underlyingCause`（`WaitForStart` / `WaitForScheduling` / `WaitForRecovery` / `RequeuingLimitExceeded`）+ `count` | 同上 |
| 重排退避 | `status.requeueState.count` 与 **`status.requeueState.requeueAt`（下次重排时间）** | 同上 |
| 最长运行 | `spec.maximumExecutionTimeSeconds`，或作业 label `kueue.x-k8s.io/max-exec-time-seconds`；**只累计 Admitted 时长**（排队时间不算），超时自动 deactivate | 同上 |
| **排队位置 / ETA** | **不存在** | — |
| PyTorchJob 接入 | 作业 label `kueue.x-k8s.io/queue-name`；Kueue 默认把 `suspend` 置 true，准入后解挂 | [Run Kubeflow PyTorchJobs](https://kueue.sigs.k8s.io/docs/tasks/run/kubeflow/pytorchjobs/) |

**结论**：我们能天然展示的是 **优先级、requeueAt、驱逐原因、被谁抢占**；"第 3 位 / 约 25 分钟后"必须自己算（算法见 §5.5），且**只能标注为估算**。

---

### 1.11 LeRobot 数据集 —— 校验与版本可以直接从 meta 读

LeRobotDataset **v3.0 是文件式存储**：`meta/info.json`（features/shapes/dtypes、**FPS**、codebase 版本、路径模板）、`meta/stats.json`（**全局 mean/std/min/max，用于归一化，`dataset.meta.stats`**）、`meta/tasks.parquet`（任务描述→ID）、`meta/episodes/`（每个 episode 的长度、任务、字节/帧偏移）、`data/*.parquet`、`videos/*.mp4`。

判"可用/不可用"的硬性条件（官方明确）：
- 录制/创建后**必须调用 `dataset.finalize()`**，否则 parquet footer 不完整、**数据集无法加载**；推送前也要 finalize。
- **相机特征必须是 video-backed**；存成图片帧的数据集 reader 不支持，需先转视频。
- v2.1 → v3.0 迁移：`python -m lerobot.scripts.convert_dataset_v21_to_v30 --repo-id=...`。
- Lance 后端：`meta/info.json` 里 `"storage_format": "lance"` 选 reader，API 不变。

出处：<https://huggingface.co/docs/lerobot/main/en/lerobot-dataset-v3>（镜像 <https://hf-mirror.com/docs/lerobot/main/en/lerobot-dataset-v3>）（本机经镜像 <https://hf-mirror.com/docs/lerobot/main/en/lerobot-dataset-v3> 访问）
训练入口：`lerobot-train --dataset.repo_id=... --policy.type=act --output_dir=...`，支持 `--dataset.streaming=true`。

**结论**：我们**不需要下载全量数据**就能给数据集打"可用/不可用"徽章——只读 `meta/` 几百 KB 即可拿到 FPS、episode 数、特征、stats（stats 还在就说明 finalize 过）。

---

## 2. 共性最佳实践 Top 10

> 排序依据：对「选数据集 → 点一下 → 进队列」这一目标的直接贡献度。

| # | 实践 | 具体做法（可直接落成界面元素/字段） | 出处产品 |
|---|---|---|---|
| **1** | **模板/配方是一等公民，且可锁定** | 管理员（或算法负责人）把"镜像 + 入口命令 + 资源 + 挂载路径 + 默认超参"存成模板；创建任务时这些字段**灰显只读**，只开放白名单字段；模板有**使用记录**（列出用它创建的任务）与**克隆**两条路径 | 阿里云 PAI「任务模板 + 只读规则」；W&B Queue config `{{var}}` + min/max；Kubeflow Trainer `Runtime` |
| **2** | **参数用白名单声明，而不是全量暴露** | 训练脚本/配方里声明 `include=["trainer"] exclude=["trainer.private"]`，平台**只渲染白名单内的字段**；未声明的参数默认收进"高级设置" | W&B `launch.manage_wandb_config()` / `manage_config_file()` |
| **3** | **梯次化：快速 / 标准 / 高级 三档** | 快速 = 模板 + 数据集 + 一个"我要训多少"的量级选择；标准 = 加 lr/epoch/batch/seed；高级 = 其余全部（可折叠、可编辑 YAML） | SageMaker（必填 5 项 + 其余默认）；veMLP（必填 5 项）；HyperPod（外层 4 参数 / 内层 N 参数） |
| **4** | **数据集是带版本与状态的对象，不是路径字符串** | 数据集详情含 **版本号**、大小、episode 数、FPS、特征、stats、最后修改时间、预览（首帧/动作曲线）；**提交时选版本**；列表上用 `可用 / 需转换 / 不可用(+原因)` 徽章 | PAI「数据集版本列表 + 挂载路径」；HF dataset card 元数据（size/task/modality）；LeRobot `meta/info.json`、`meta/stats.json` |
| **5** | **提交前预检，把错误挡在入队之前** | 点"开始"前做一次**同步校验**（≤2s）：数据集 meta 完整？finalize 过？FPS/特征与配方匹配？显存预估够不够？不通过则**在按钮旁给出可修复的建议**，而不是让任务进队列后失败 | Hugging Face Dataset Viewer 的"超大/非 parquet 则降级为前 100 行预览 + 明确提示"；SageMaker 配额不足前置报 `ResourceLimitExceeded` |
| **6** | **提交后立刻可见的"作业卡"：阶段时间轴 + 事件流** | 时间轴节点：`已提交 → 已入队 → 已准入 → 拉镜像 → 启动容器 → 训练中 → 保存产物`，每节点带时间戳；事件流逐条追加（来自 Kueue 事件与 Pod 事件） | PAI 任务概览的阶段时间轴 + 事件页签（含 `ImagePulling → WaitingForRun → Running`） |
| **7** | **排队不是"等待中"，而是根因 + 出路** | 每个排队态给出**具体原因**（配额不足/高优预留/资源碎片/个人配额）+ **1–2 个可点击动作**（提优先级需申请、转可抢占、换小规格、取消） | veMLP「负载排队中状态常见原因」（5 类根因 + 逐条文案） |
| **8** | **失败诊断结构化：规则化标签 + 日志证据** | 失败时先出"诊断卡"：`OOM`、`数据加载失败`、`loss 不下降`、`训练停滞(X 分钟无新指标)`、`被抢占`、`超时`、`镜像拉取失败`；每条附**判定阈值 + 对应日志片段 + 一键跳转** | SageMaker Debugger 9 条内置规则（`overfit` 阈值 0.1、`stalled_training` 1800s 等） |
| **9** | **长任务默认可恢复；时长与续训是一等字段** | 表单里显式出现 `最长运行时间`、`断点续训(从最新 checkpoint)`、`checkpoint 间隔`；被驱逐/超时后显示"可从上一次 checkpoint 续跑"按钮 | HyperPod `time_limit / auto_checkpoint / resume_from_checkpoint`；Modal "可重入 + last.ckpt"；Kueue `maximumExecutionTimeSeconds` |
| **10** | **一次提交 = 一条可分享的 Run 记录（带版本快照）** | 提交时自动生成 `run_id` 与**配置快照**（模板版本 + 数据集版本 + 全部参数 + 镜像 digest），详情页可"克隆为参数修改"、"与上一次对比"、"复制分享链接" | W&B Job version + `Paste from…`；PAI 克隆/分享/生成脚本；MLflow run 语义 |

---

## 3. 「一键开始训练」页面设计建议（线框图）

### 3.1 首屏：训练台（Training Console）

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│ MT-Platform   算法团队 / 视觉抓取          [🔍 搜索实验]   [队列: 4090常用 ▾]  (张)│
├────────────────────────────────────────────────────────────────────────────────────┤
│  新建训练                                                                          │
│                                                                                    │
│  ┌───────────────────────────── ① 选配方（模板）─────────────────────────────┐    │
│  │  (●) ACT-抓取-v3        v4  官方维护 · 单卡4090 · 约 2h      [详情]      │    │
│  │  ( ) DiffusionPolicy-v1 v2  官方维护 · 单卡4090 · 约 4h      [详情]      │    │
│  │  ( ) 我的模板「箱子抓取A」 私有                              [复制/编辑] │    │
│  │  提示：配方的镜像、启动命令、超参默认值由维护者锁定，你不需要改。        │    │
│  └──────────────────────────────────────────────────────────────────────────┘    │
│                                                                                    │
│  ┌───────────────────────────── ② 选数据集 ─────────────────────────────────┐     │
│  │  数据源 (●) 团队数据集库   ( ) 输入路径 / 新上传                        │     │
│  │  ┌──────────────────────────────────────────────────────────────────┐    │     │
│  │  │ ✅ 抓取-箱子-2026Q1   v3  ✅可用   128 ep · 41.2k 帧 · 30fps     │    │     │
│  │  │    2 相机 · 7 维 action · meta/stats ✅ · 更新 2天前    [预览]   │    │     │
│  │  ├──────────────────────────────────────────────────────────────────┤    │     │
│  │  │ ⚠️ 抓取-抽屉-试采     v1  ⚠️未 finalize  35 ep · 11k 帧          │    │     │
│  │  │    原因：parquet footer 缺失，可能无法加载      [去修复]         │    │     │
│  │  ├──────────────────────────────────────────────────────────────────┤    │     │
│  │  │ ⛔ 抓取-旧v2格式       v2  ⛔格式不符   (v2.1，需转 v3.0)          │    │     │
│  │  │    [一键转换为 v3.0]                                             │    │     │
│  │  └──────────────────────────────────────────────────────────────────┘    │     │
│  │  版本：v3 (最新) ▾     预览：烽□首帧缩略图 × 6 · 动作曲线小图             │     │
│  └──────────────────────────────────────────────────────────────────────────┘    │
│                                                                                    │
│  ┌────────────── ③ 将运行 ───────────────┐  ┌──────────── ④ 高级选项 ▸ ──────────┐ │
│  │ 资源   RTX 4090 ×1 / 32c / 128G        │  │  ▸ 常用参数（5 项）              │ │
│  │ 队列   4090常用 (本地队列)             │  │  ▸ 训练细节（折叠，默认值已填）  │ │
│  │ 优先级  普通 ▾   (高优需申请)          │  │  ▸ 直接编辑 YAML                 │ │
│  │ 预计   需排队 · 前方 2 个任务          │  │  ▸ 超参搜索（可选）              │ │
│  │ 时长上限 6 小时 ▾                      │  └──────────────────────────────────┘ │
│  │ □ 完成后通知我                         │                                       │
│  └────────────────────────────────────────┘                                       │
│                                                                                    │
│   预检：✅ 数据集可用  ✅ 配方兼容(30fps)  ⚠️ 显存预估 21.4/24G（余量 11%）         │
│                                                     [ 保存草稿 ]   [ ▶ 开始训练 ]  │
└────────────────────────────────────────────────────────────────────────────────────┘
```

**首屏原则**：
1. **只有两件事是必须由人做的**：选配方、选数据集。其余全部有默认值且已预填。
2. 三个「将运行 / 预检」区块**首屏可见**，用户点按钮前就知道要花多久、排多久、显存够不够。
3. 「开始训练」按钮的**左侧永远挂着预检结果**，不是点完才报错。

### 3.2 最少需要用户做什么

| 场景 | 用户动作数 | 说明 |
|---|---|---|
| 老数据集 + 已有配方（最常见） | **2 次点击 + 1 次确认**：点配方 → 点数据集 → 「开始训练」 | 其余字段由配方与默认值填充 |
| 新数据集 | 4 次：配方 → 数据集 → 看预检 → 开始 | 若预检不通过，先「去修复 / 一键转换」 |
| 要调参 | 再展开「常用参数」（≤6 个字段） | lr / epoch / batch / seed / 保存间隔 / 断点续训 |

**必填最少集（我们的场景）只有 3 个**：`recipe_id`、`dataset_id + dataset_version`、`queue`。其余（镜像、命令、资源、时长上限、优先级）都能给默认值。

### 3.3 高级选项怎么放

三层，**只展开第一层**：

```
④ 高级选项 ▸
 ┌─ 常用参数（默认展开，最多 6 项）───────────────────────────────┐
 │ learning_rate   [ 1e-4 ] (默认，配方推荐值)                    │
 │ batch_size      [ 32 ]   (显存预估 21.4G/24G)                  │
 │ epochs          [ 100 ]  预计 1h52m                            │
 │ seed            [ 42 ]                                         │
 │ checkpoint 间隔 [ 每 10 min ]                                  │
 │ ☑ 允许从最新 checkpoint 续训（被抢占后自动恢复）                │
 └────────────────────────────────────────────────────────────────┘
 ▸ 训练细节（折叠）        图像增强、streaming、num_workers、AMP、compile…（均为默认值）
 ▸ 直接编辑 YAML（折叠）   只读预览 + "解锁编辑"，保存为新配方版本
 ▸ 超参搜索（可选 tab）    只对"批量实验"开放：trial 数/并行数/目标指标/搜索空间
```

**关键规则**：
- 每个高级字段后**必须括注默认值与影响**（例如 batch_size 后面直接写"显存预估"），否则用户不敢改。
- 「直接编辑 YAML」保存时**一律生成配方新版本**，不允许匿名改；这样"实验可复现"靠版本号兜底。
- 超参搜索不是第二个产品，而是**同一页面的一个模式**（trial 数 × 并行度 = 一次提交 N 个 TrainJob，共享一个 group）。

### 3.4 提交后立刻看到什么（2 秒内）

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  ✅ 已提交 · run-7f21  「ACT-抓取-v3 × 抓取-箱子-2026Q1(v3)」       [复制链接] │
│                                                                              │
│  ● 已提交   ● 已入队    ○ 已准入    ○ 拉镜像    ○ 启动容器    ○ 训练中       │
│  10:21:03   10:21:04                         (等待队列)                      │
│  配方 ACT-抓取-v3@v4 · 数据集 抓取-箱子-2026Q1@v3 · 4090×1 · 上限 6h          │
│                                                                              │
│  排队中 ──────────────────────────────────────────────────────────────       │
│  原因：队列配额已满（4090×1 全部在用）                                       │
│  你的位置：第 3 / 共 5 个排队  ⏱ 预计 ~1h20m（按历史中位时长估算）           │
│  ▸ 前方任务：抓取-抽屉A(剩 12m) · 抓取-箱子B(剩 38m) · 抓取-旧数据C(高优)     │
│  可做：[提高优先级(申请)] [改用可抢占槽位(可能被中断)] [取消]                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

**规则**：
- 提交**立即返回**、绝不阻塞等待准入。第一个可看的就是**位置 + 原因 + ETA（标注"估算"）**。
- 训练中同页切换为：**loss 曲线（水线主图）+ 资源条（GPU util / 显存 / 数据加载等待占比）+ ETA + 日志流**，日志默认折叠在"全部日志"里。
- 任何状态变化都**推送**（SSE/WebSocket），不要求用户刷新。

---

## 4. 反模式清单

| # | 反模式 | 为什么伤人 | 出处/证据 | 改法 |
|---|---|---|---|---|
| 1 | **一次暴露几十个参数** | 用户无法判断哪些该改，于是全默认 → 平台变成"黑盒"，或者乱填 → 失败率上升 | Katib 需要填 objective/algorithm/early-stop/params/collector/template 全链路；HyperPod recipe 的 model/parallelism/checkpoint 段有几十个键 | 白名单 + 三档（§2 实践 2、3） |
| 2 | **"一键"前面先逼你建 8 个前提资源** | 每新建一个算法要重新配镜像、网络、IAM、存储，一"键"就变成一"下午" | SageMaker 训练前需准备镜像/数据/LimitRange/配额；Modal 函数也需先建 Volume | **平台侧预建**：镜像仓库、只读数据挂载、默认队列；用户只选 |
| 3 | **失败只给原始日志** | 用户要在几千行 PyTorch 日志里找 `CUDA out of memory` | Katib/HF Jobs 均无结构化诊断 | Debugger 式规则化诊断卡（§2 实践 8） |
| 4 | **把"等待中"当成完整的状态说明** | 用户不知道要等 5 分钟还是 5 小时，也不知道能做什么 | Kueue 本身没有位置/ETA 字段 | 根因 + 出路 + 位置/ETA（§2 实践 7、§5.5） |
| 5 | **数据集以路径字符串出现** | 无法版本化、无法预检、换人接手就断链 | PAI 把数据集做成带版本的对象，单列挂载路径 | 数据集即对象 + 版本下拉（§2 实践 4） |
| 6 | **多 tab 向导式表单** | 用户看不到全貌，来回跳；tab 之间依赖隐藏 | Katib UI 的 8 个 tab | 单页 + 折叠分区 + 固定"将运行"摘要 |
| 7 | **同一配置能匿名改、无版本** | 昨天的实验今天复现不了 | W&B Job version、PAI 模板使用记录、MLflow run | 每次提交生成配置快照；改 YAML 必须新建版本 |
| 8 | **优先级对所有用户开放** | 人人都设"高" = 没有优先级，调度秩序失效 | veMLP 提交时只开放部分档位，完整范围由管理员调；PAI 管理员才能改运行中任务优先级 | 默认"普通"，"高优"需申请/审批，管理员可事后调整 |
| 9 | **用"从头重跑"冒充"容错"** | Anyscale 的 retry 是从头重启；长任务被抢占后白干几小时 | Anyscale jobs：`max_retries` 失败后"restart from the beginning"；Kueue 驱逐本来就很常见 | 一等字段 `resume_from_checkpoint`（§2 实践 9） |
| 10 | **ETA 说得像承诺** | 排队 ETA 依赖别人任务何时结束，必然不准；说死就失信 | 调度器只能给"会尽快"（veMLP 原文："优先级高不代表绝对先调度"） | 一律标"估算"，并给"按历史中位时长"的算法说明 |
| 11 | **不提供"克隆/重跑"** | 用户只能手工抄参数，抄错 | PAI 克隆 / 分享 / 生成脚本；W&B `Paste from…` | 每个 run 页固定"克隆并修改""按此重跑" |
| 12 | **把超参搜索做成独立系统** | 用户体验分裂：训练在 A 处，调参在 B 处，结果对不上 | Vertex/Katib 均为 job 的一个属性 | 同页面模式切换（§3.3） |

---

## 5. 对我们场景的具体建议（4090 单机 · LeRobot · Kueue · PyTorchJob）

> 目标：让算法同学**选数据集 + 点开始**即可进队列，同时平台方保住"可复现、可排队、可诊断"。

### 5.1 产品抽象：三层对象，先定数据模型

```
RecipeTemplate（训练配方，平台/算法负责人维护，有版本）
  ├─ image_ref            (镜像 digest，例：registry.local/lerobot:0.7.0-cu124@sha256:…)
  ├─ entrypoint           (例：python -m lerobot.scripts.lerobot_train)
  ├─ arg_schema[]         (JSON Schema：name/type/default/min/max/visible_in_tier/required)
  ├─ locked_fields[]      (只读字段：image、mount_path、resource 等)
  ├─ resources            {gpu: 1, gpu_type: "RTX4090", cpu: 16, mem: 64Gi}
  ├─ max_runtime_s        (默认 6h)
  ├─ checkpoint_policy    {every_min: 10, resume: true}
  └─ compatibility        {min_fps, max_fps, required_features: ["observation.images.*","action"]}

RunRequest（一次提交，不可变快照）
  ├─ run_id / recipe(template_id, version)
  ├─ dataset(dataset_id, version, mount=ro)
  ├─ args{...}            (仅 arg_schema 白名单内)
  ├─ queue / priority_class / requested_duration_s
  └─ snapshot_hash

KueueWorkload (由 PyTorchJob 自动生成)
  └─ queue-name=…, priority-class=…, max-exec-time-seconds=…
```

**直接可用的结论**：
- `resources` 写成**配方里的只读字段**，不要在表单里让用户选 GPU —— 我们只有 4090，用户没得选，也不该选。
- `compatibility` 让"数据集不可用"这件事在**配方层就被定义**，而不是运行时发现。

### 5.2 数据集管理（LeRobot 专用，可直接实现）

**A. 扫描与徽章（不用下载全量数据）**

只拉 `meta/info.json`、`meta/stats.json`、`meta/tasks.parquet`、`meta/episodes/` 的元信息（通常 <1MB），落库：

| 徽章 | 判定规则（可执行） |
|---|---|
| ✅ 可用 | `meta/info.json` 可解析；`codebase_version` ≥ v3.0；`meta/stats.json` 存在（说明 finalize 过）；`fps` 在配方 `[min_fps, max_fps]` 内；`features` 里所有 `observation.images.*` 为 **video-backed**（非图片帧） |
| ⚠️ 未完成 | 缺 `meta/stats.json` 或 `meta/episodes/` 不完整 → 文案"疑似录制后未执行 `finalize()`，parquet footer 缺失，可能无法加载"，动作：`去修复 / 联系上传者` |
| ⛔ 需转换 | `codebase_version` 为 v2.x → 动作按钮：`一键转换为 v3.0`（对应 `python -m lerobot.scripts.convert_dataset_v21_to_v30`） |
| ⛔ 不符配方 | fps 不匹配 / 缺少配方要求的特征 → 文案指明差在哪，动作：`换配方 / 查看详情` |

> 依据：LeRobot v3 官方文档明确 `finalize()` 不做会导致 parquet 损坏、数据集无法加载；相机特征必须 video-backed。来源见 §1.11。

**B. 列表列定义**（每列都是可排序/可筛选的字段）

`名称 | 版本(下拉,默认最新) | 状态徽章 | episode 数 | 总帧数 | 时长 | FPS | 相机数 | action 维度 | 大小 | 最后修改 | 上传者 | 使用次数`

**C. 版本管理**

- 版本号 = 内容摘要（对 `meta/` + 分片列表做 hash），**同一内容重复上传不产生新版本**；
- **提交时强制选版本**（默认"最新"，但显示"钉住 v3"字样）；在运行卡与实验页上统一显示 `数据集@版本`；
- 数据集删除/覆盖 → 引用它的历史 run 仍然可显示原版本（**软删除 + 不可变版本**），这是我们比"路径字符串"强的地方。

**D. 预览**

首 3 个 episode 各抽 6 帧缩略图 + 动作曲线小图（用 `meta/episodes` 的偏移做随机访问，取几十帧即可）。这是"数据集看起来对不对"的最短路径，投入小、收益大。

### 5.3 训练配方（Template）：**用 PAI 的「只读锁定」当主设计**

**表单渲染规则（一一对应到控件）**：

| 配方字段属性 | 界面表现 |
|---|---|
| `locked_fields` 内的字段 | 输入框**灰显 + 锁图标**，hover 提示"由配方 ACT-抓取-v3@v4 锁定"；整块收进"已锁定（3 项）"折叠区 |
| `arg_schema` 中 `visible_in_tier: quick` | 首屏直接显示（建议 ≤6 个：lr / batch / epochs / seed / ckpt 间隔 / 续训） |
| `visible_in_tier: advanced` | 收进「训练细节」折叠区 |
| `type/min/max` | 渲染成数字输入 + 滑杆，越界即时红框，不等到提交 |
| 不在 `arg_schema` 内 | **不渲染**（SSOT 是 schema，不是 YAML 全量）；"直接编辑 YAML"可看但保存即生成新配方版本 |

**管理侧动作**：新建配方 / 从某个成功 run 生成配方（"以此 run 为模板"，等价于 PAI 的"基于已有任务创建模板"）/ 只读钩子 / 配方使用记录（列出用它的所有 run）。

### 5.4 提交事务：一次点击，五个原子动作

```
[开始训练]
 1. 预检（同步，≤2s，失败则按钮不变灰、直接给修复建议）
      - 数据集状态 == 可用（按 5.2 规则）
      - 配方 compatibility 匹配（fps / features）
      - 显存预估 ≤ 24G × 0.9（预估公式见 5.6）
      - 用户队列配额 > 0（读 ClusterQueue 的 nominalQuota 与本用户 LocalQueue 使用量）
 2. 生成 RunRequest 快照（含 snapshot_hash）
 3. 创建 PyTorchJob：
      labels:
        kueue.x-k8s.io/queue-name: <team-localqueue>
        kueue.x-k8s.io/priority-class: <default|high>
        kueue.x-k8s.io/max-exec-time-seconds: "<max_runtime_s>"
      spec.runPolicy.suspend: true        # 由 Kueue 准入后解挂
      spec.pytorchReplicaSpecs.Master.replicas: 1
      # 单卡：不要建 Worker；nproc_per_node=1
 4. 写 Run 记录（status=Queued），返回 run_id
 5. 订阅 watch → SSE 推送状态变化
```

> 单机 4090 的一个**易错点**：PyTorchJob 的 `Master` 与 `Worker` 都要各占 1 个 Pod；单卡场景**只建 Master**，否则 Kueue 会按 2 个 Pod 的 `podSets` 计配额（`count × requests`），你的"1 卡任务"就会因为要 2 个 GPT 而永远排不上或抢占别人。依据：Kueue `podSets` 的配额计算方式见 <https://kueue.sigs.k8s.io/docs/concepts/workload/>。

**挂载约定**：数据集只读挂到**固定路径** `/data/dataset`（由配方锁定），`--dataset.root=/data/dataset`；checkpoint 写 `/outputs/<run_id>/ckpt`（PVC/共享存储）；最终产物写 `/outputs/<run_id>/final`。

### 5.5 排队体验：Kueue 能力 + 我们补的三件事

Kueue 能给：`queue-name`、`priority-class`、`requeueAt`、`evictions[].reason/underlyingCause`、`max-exec-time-seconds`。**不能给**：位置、ETA。

我们要补：

**① 排队位置（可精确算，基于 BestEffortFIFO）**

```
position = 1 + count( 同 LocalQueue 内 status=Pending 的 Workload
                     且 (priority > mine) 或 (priority == mine 且 creationTimestamp < mine) )
```
在页面写清规则："按优先级、再按提交时间排序；同优先级先来先跑"。

**② ETA（估算，标注口径）**

```
ETA ≈ Σ over front_tasks: remaining_estimate(front_task)
remaining_estimate = 该配方最近 N 次成功 run 的 (时长中位数 − 已运行时长)，下限 0
```
- 只对**同队列、同配方或同资源量级**的任务求中位，避免用"别人的 8 小时长训"吓用户；
- 展示写成 `预计 ~1h20m（按历史中位时长估算，非承诺）`；
- 若前方有"高优/资源预留"任务（对应 veMLP 的"资源预留"根因），ETA 直接显示 `不确定`。

**③ "排队中"诊断卡——直接照搬 veMLP 的 5 类根因，映射到 Kueue 语义**

| UI 提示 | 我们的判定依据 |
|---|---|
| "队列配额已满（4090×1 全部在用）" | ClusterQueue 已准入 Workload 的 requests 之和 == nominalQuota |
| "前方有高优任务正在等待/预留" | 前方同队列存在 priority 更高的 Pending Workload |
| "你的个人配额已用满（2/2 卡）" | 本人在该 LocalQueue 已准入/占用数达上限（需我们维护 per-user 上限） |
| "资源碎片：空闲卡数不足以凑齐 1 张完整卡" | 节点剩余可分配 GPU 不满足 `nvidia.com/gpu: 1`（或节点被其他 Pod 占用 CPU/内存导致无法调度） |
| "资源组已满，需等待其他团队释放" | 非队首原因，而在 ClusterQueue 层 |

每条提示下挂 **1–2 个动作按钮**：`提高优先级（申请）`、`改用可抢占槽位（有中断风险）`、`换更小 batch/更短上限`、`取消`。

**④ 优先级设计（照 veMLP 的"只开放部分档位"）**

- 用户只看到三档：`普通（默认）`、`高优（需申请，附理由）`、`低优·可抢占`；
- 完整数值范围（如 1–9）由管理员维护，**不要暴露给用户**；
- 每次被抢占，运行卡上显示：`因高优任务被驱逐（reason=Preempted），已于 10:42 重新排到第 2 位，预计 11:05 续跑`——`requeueAt` 字段直接来自 Kueue。

### 5.6 训练中可观测性（单卡 4090 的具体指标）

| 区块 | 显示内容 | 数据来源 |
|---|---|---|
| 主图 | `train loss` / `val loss` 曲线（水线），叠加"最近一次 checkpoint"竖线 | 训练脚本回调写 JSONL → 平台轮询 |
| 进度 | `epoch 37/100`、`step 4.2k/11k`、`已用 41m / 上限 6h`、`ETA ~1h10m`（按最近 10 min 速率外推） | 同上 |
| 资源 | GPU 利用率、**显存 21.4/24.0 GiB**、GPU 温度/功耗（4090 易降频）、**数据加载等待占比**（LeRobot 多相机解压常是瓶颈）、磁盘写带宽（写 ckpt 时） | nvidia-smi / DCGM exporter 指标 |
| 日志 | 默认折叠，只展开"最后 20 行 + 过滤后的 WARNING/ERROR"；提供 `全部日志` / `按级别` / `下载` | Loki / k8s logs |
| 检查点 | 最近一次 ckpt 时间与大小；"从该 ckpt 续跑"按钮 | 输出目录扫描 |

**失败诊断卡（规则 = 我们自己的 Debugger）**：

| 诊断标签 | 判定规则（可直接实现） | 显示 |
|---|---|---|
| `OOM 显存不足` | 日志匹配 `CUDA out of memory. Tried to allocate (\d+)(MiB|GiB)` | 实际峰值 + 建议：降 batch / 开 AMP / 降图像分辨率 |
| `数据集不可读` | 日志匹配 `FileNotFoundError` / `pyarrow.lib.ArrowInvalid` / `finalize` 相关；或挂在 `/data/dataset` 的文件数为 0 | 指向具体数据集版本与"去修复" |
| `相机解码失败` | 日志匹配 `av.error` / 视频流打开失败 / 帧数不一致 | 指向有问题的那台相机 |
| `训练停滞` | 距上一次上报指标 > N 分钟（默认 15min，参考 SageMaker `stalled_training` 默认 1800s） | "已停滞 21 分钟，可能是数据加载死锁" + `取堆栈` |
| `loss 不下降` | 最近 20% step 的 loss 斜率 ≥ -ε 且 val 无改善 | 建议：检查 lr、归一化（`meta/stats.json` 的 mean/std 是否被使用）、数据分布 |
| `过拟合` | `(val_loss - train_loss) / train_loss > 0.1` 连续 ≥1 次评估（参考 SageMaker `overfit` 默认阈值） | 建议：减 epoch / 加增强 |
| `被抢占` | Workload `evictions[]` 含 `reason=Preempted` | "将于 requeueAt 重新排队"，并提供"改低优·可抢占 / 提高优先级" |
| `超时终止` | 超过 `max-exec-time-seconds` | "已从上一次 checkpoint 续跑？"一键 |
| `镜像拉取失败` | Pod 事件 `ErrImagePull/ImagePullBackOff` | 显示镜像 digest + 仓库连通性检测结果 |

### 5.7 权限与协作

| 层 | 落地 | 底层对应 |
|---|---|---|
| 空间 | `团队空间`（如"视觉抓取"），成员含 `管理员 / 算法工程师 / 只读` | K8s Namespace + LocalQueue 一一对应（Kueue 文档明确"一个 namespace 通常对应一个租户"） |
| 队列 | 每个团队一个 `namespace-scoped LocalQueue` → 指向共享 `ClusterQueue`，用 `cohort` 做借还 | `LocalQueue.spec.clusterQueue`、`ClusterQueue.spec.cohort` |
| 配额展示 | 团队页显示"4090 配额 4 张，已用 3，借用兄弟队 0"；个人配额（可选）显示"你 2/2" | ClusterQueue `nominalQuota/borrowingLimit/lendingLimit` |
| 组织层级 | `项目(Project) → 配方(Recipe) → 实验组(Group) → run`；group 用于超参搜索的 N 个 trial | 直接用 **MLflow**：`experiment=项目`、`run_name=配方@v`、`tag.mlflow.parentRunId` 做 group，避免自造 schema（SageMaker 官方已转向 MLflow） |
| 可见性 | 默认团队内可见；提交时可设"仅我可见"（veMLP 有相同字段可对标） | label + 后端过滤 |
| 分享与复现 | 每个 run 页固定三个按钮：`克隆并修改` / `按此重跑` / `生成 K8s YAML` | 与 PAI 的克隆/生成脚本一致 |

### 5.8 实施优先级（建议按周推进）

| 优先级 | 交付物 | 验收标准 |
|---|---|---|
| **P0（1–2 周）** | 配方层（含 locked_fields + schema）、数据集扫描徽章、提交事务（§5.4）、排队诊断卡（§5.5 ③） | 老数据集场景 2 次点击可提交；提交 < 2s；提交后 1s 内看到位置与原因 |
| **P1（+2 周）** | 运行页（时间轴 + loss 曲线 + 显存 + 日志）、诊断卡 5 条规则、克隆/重跑 | 失败 run 100% 能分到某一类标签 |
| **P2（+2 周）** | 超参搜索模式、数据集 v2.1→v3.0 一键转换、YAML 解锁（生成新配方版本）、个人配额 | 超参搜索产出 group 视图 |
| **P3（按需）** | ETA 精度迭代（引入历史时长分位数）、通知（IM/邮件）、配方使用记录看板 | ETA 偏差中位数 < 25% |

---

## 6. 引用索引（可点开核对）

**Hugging Face**
- Jobs 配置（flavor / volumes / secrets / PEP 723）：<https://huggingface.co/docs/hub/jobs-configuration>（本机须走镜像 <https://hf-mirror.com/docs/hub/jobs-configuration>）
- 数据集卡片元数据：<https://huggingface.co/docs/hub/datasets-cards>（镜像 <https://hf-mirror.com/docs/hub/datasets-cards>）
- Dataset Viewer（5GB 内自动转 Parquet、超大降级为 100 行预览）：<https://huggingface.co/docs/hub/datasets-viewer>（镜像 <https://hf-mirror.com/docs/hub/datasets-viewer>）
- AutoTrain（含"已停止维护"公告）：<https://huggingface.co/docs/autotrain/index>
- LeRobotDataset v3.0：<https://huggingface.co/docs/lerobot/main/en/lerobot-dataset-v3>

**Weights & Biases**
- 术语（Job/Queue/Agent/Target）：<https://docs.wandb.ai/guides/launch/launch-terminology>
- 提交到队列（Job version / Overrides / Paste from / Priority / Queue Configurations）：<https://docs.wandb.ai/guides/launch/add-job-to-queue>
- 参数白名单（include/exclude、配置文件输入）：<https://docs.wandb.ai/guides/launch/job-inputs>
- 队列配置模板（`{{var}}`、type、min/max）：<https://docs.wandb.ai/guides/launch/setup-queue-advanced>
- 队列监控：<https://docs.wandb.ai/guides/launch/launch-queue-observability>

**AWS**
- Model training 总览：<https://docs.aws.amazon.com/sagemaker/latest/dg/train-model.html>
- HyperPod recipes：<https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-recipes>
- Recipe general configuration：<https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-recipes-general-configuration>
- Debugger 内置规则：<https://docs.aws.amazon.com/sagemaker/latest/dg/debugger-built-in-rules>
- Experiments（已建议迁移 MLflow）：<https://docs.aws.amazon.com/sagemaker/latest/dg/experiments.html>

**Google**
- Vertex 分布式训练官方 notebook：<https://raw.githubusercontent.com/GoogleCloudPlatform/vertex-ai-samples/main/notebooks/official/training/get_started_with_vertex_distributed_training.ipynb>

**Kubeflow / Kueue**
- Katib UI（含弃用公告）：<https://www.kubeflow.org/docs/components/katib/user-guides/katib-ui/>
- Katib Experiment 配置：<https://www.kubeflow.org/docs/components/katib/user-guides/hp-tuning/configure-experiment/>
- Kubeflow Trainer（TrainJob + Runtime）：<https://www.kubeflow.org/docs/components/trainer/overview/>
- Kueue ClusterQueue（StrictFIFO / BestEffortFIFO / cohort）：<https://kueue.sigs.k8s.io/docs/concepts/cluster_queue/>
- Kueue LocalQueue：<https://kueue.sigs.k8s.io/docs/concepts/local_queue/>
- Kueue Workload（podSets、requeueAt、schedulingStats、maximumExecutionTimeSeconds）：<https://kueue.sigs.k8s.io/docs/concepts/workload/>
- Kueue WorkloadPriorityClass：<https://kueue.sigs.k8s.io/docs/concepts/workload_priority_class/>
- Kueue 运行 PyTorchJob：<https://kueue.sigs.k8s.io/docs/tasks/run/kubeflow/pytorchjobs/>

**国内**
- PAI 创建训练任务（数据集版本、挂载路径、优先级）：<https://help.aliyun.com/zh/pai/create-a-training-task>
- PAI 任务模板（只读锁定）：<https://help.aliyun.com/zh/pai/use-task-templates-and-comparisons-to-improve-tuning-efficiency>
- PAI 查看训练详情（阶段时间轴、事件、资源视图）：<https://help.aliyun.com/zh/pai/view-training-jobs>
- PAI 管理资源配额（树形）：<https://help.aliyun.com/zh/pai/manage-resource-quotas>
- veMLP 创建训练任务（必填 5 项、优先级 1–9）：<https://www.volcengine.com/docs/ml-platform/single-node-distributed-training-task>
- veMLP 优先级调度策略：<https://www.volcengine.com/docs/ml-platform/priority-scheduling-policy>
- veMLP 排队中状态常见原因：<https://www.volcengine.com/docs/ml-platform/common-causes-of-load-queuing-status>

**极简启动**
- Modal Getting started：<https://modal.com/docs/guide/getting-started>
- Modal 长任务可恢复训练：<https://modal.com/docs/examples/long-training>
- Anyscale Jobs：<https://docs.anyscale.com/jobs>
- Anyscale Job queues（PRIORITY/FIFO/LIFO）：<https://docs.anyscale.com/jobs/queues>
- RunPod Choose a pod（按负载推荐 GPU/显存）：<https://docs.runpod.io/pods/choose-a-pod>
- RunPod Pods 概览：<https://docs.runpod.io/pods/overview>

---

### 附：调研限制说明（供后续补强）

1. 本机网络**无法直连 `huggingface.co` 与 `cloud.google.com`**。HF 文档经 `hf-mirror.com` 访问（内容为官方文档镜像）；Vertex AI 仅取得官方 GitHub 样例 notebook，**其 HiperparameterTuningJob 的 `parameter_spec` 细节未取得原文**，§1.5 已标注。建议在可访问环境补充：[Custom training](https://cloud.google.com/vertex-ai/docs/training/custom-training) 与 [Hyperparameter tuning](https://cloud.google.com/vertex-ai/docs/training/hyperparameter-tuning-overview)。
2. W&B 文档已迁移至 CoreWeave 域名，`docs.wandb.ai/*` 现为跳转入口；内容与 `docs.coreweave.com/products/wandb/...` 一致。
3. Katib、AutoTrain 均处于弃用/停维状态，相关结论仅作**交互反例/历史参考**，不作为技术选型依据。