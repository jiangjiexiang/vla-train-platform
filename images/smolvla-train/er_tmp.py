"""端到端模拟：源任务有 checkpoint → 新任务续训。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app")
import train_vla

root = Path(tempfile.mkdtemp())

# 源任务：003 的 3000 步 checkpoint
src = root / "smolvla-old" / "run" / "checkpoints"
(cand := src / "003000").mkdir(parents=True)
(cand / "pretrained_model").mkdir()
(cand / "pretrained_model" / "train_config.json").write_text('{"steps": 3000}')
(cand / "pretrained_model" / "model.safetensors").write_bytes(b"x" * 100)
(cand / "training_state").mkdir()
(cand / "training_state" / "training_step.json").write_text('{"step": 3000}')
(src / "last").symlink_to("003000")

# 新任务：续训
new_dir = root / "smolvla-new"
new_dir.mkdir()
os.environ["CHECKPOINT_DIR"] = str(new_dir)

args = train_vla.build_parser().parse_args([
    "--dataset_root", "/datasets/pick1028",
    "--resume", "--resume_from", "smolvla-old",
])
args.steps = 3100

out = new_dir / "run"
cmd = train_vla.build_train_command(args, Path("/datasets/pick1028"), out)

print("=== 续训命令 ===")
print(" ".join(c if "config_path" not in c else "--config_path=<...>" for c in cmd))
print()
print("policy.path 存在:", any("policy.path" in c for c in cmd), "(应为 False)")
print("config_path 存在:", any("config_path" in c for c in cmd), "(应为 True)")
print("resume=true:", any(c == "--resume=true" for c in cmd))
print()
copied = list((out / "checkpoints").iterdir()) if (out / "checkpoints").exists() else []
print("复制过来的 checkpoint:", [p.name for p in copied])
cfg = out / "checkpoints" / "003000" / "pretrained_model" / "train_config.json"
print("config 可读:", cfg.exists())
print("training_step:", (out / "checkpoints" / "003000" / "training_state" / "training_step.json").read_text() if (out / "checkpoints" / "003000" / "training_state" / "training_step.json").exists() else "无")
