#!/usr/bin/env bash
# 平台 PPO 训练入口：把 --flag 风格参数翻译成 RLinf 的 Hydra 配置覆盖。
# 日志与 checkpoint 写到 $CHECKPOINT_DIR（平台约定目录），未设置时用 results/。
set -euo pipefail

source switch_env "${RLINF_ENV:-openvla}"

export EMBODIED_PATH=/workspace/RLinf/examples/embodiment
export REPO_PATH=/workspace/RLinf
export PYTHONPATH="${REPO_PATH}:${PYTHONPATH:-}"
export MUJOCO_GL="${MUJOCO_GL:-egl}"
export PYOPENGL_PLATFORM="${PYOPENGL_PLATFORM:-egl}"
export ROBOT_PLATFORM="${ROBOT_PLATFORM:-LIBERO}"

CONFIG="${RLINF_CONFIG:-maniskill_ppo_openvla_quickstart}"
OVERRIDES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)         CONFIG="$2"; shift 2 ;;
    # RLinf 的 actor 和 rollout 各自持有一份 model_path，必须同时覆盖，
    # 否则 rollout 仍加载配置里的旧路径（上游配置默认是 /path/to/ 占位）。
    --model-path)     OVERRIDES+=("actor.model.model_path=$2" "rollout.model.model_path=$2"); shift 2 ;;
    # GR00T N1.7 还需要独立的 VLM backbone（Cosmos-Reason2-2B）。
    --backbone-path)  OVERRIDES+=("actor.model.backbone_model_path=$2" "rollout.model.backbone_model_path=$2"); shift 2 ;;
    --max-steps)      OVERRIDES+=("runner.max_steps=$2"); shift 2 ;;
    --save-interval)  OVERRIDES+=("runner.save_interval=$2"); shift 2 ;;
    --num-envs)       OVERRIDES+=("env.train.total_num_envs=$2" "env.eval.total_num_envs=$2"); shift 2 ;;
    --placement)      OVERRIDES+=("cluster.component_placement=$2"); shift 2 ;;
    *)                OVERRIDES+=("$1"); shift ;;
  esac
done

LOG_PATH="${CHECKPOINT_DIR:-/workspace/RLinf/results}"
mkdir -p "$LOG_PATH"
OVERRIDES+=("runner.logger.log_path=$LOG_PATH")
OVERRIDES+=("runner.logger.experiment_name=${JOB_NAME:-rlinf-ppo}")

echo "config: $CONFIG"
echo "overrides: ${OVERRIDES[*]}"

exec python "${EMBODIED_PATH}/train_embodied_agent.py" \
  --config-path "${EMBODIED_PATH}/config/" \
  --config-name "$CONFIG" \
  "${OVERRIDES[@]}"
