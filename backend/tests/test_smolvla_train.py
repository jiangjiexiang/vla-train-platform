from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest

TRAIN_VLA_PATH = Path(__file__).resolve().parents[2] / "images" / "smolvla-train" / "train_vla.py"


@pytest.fixture(scope="module")
def train_vla():
    loader = importlib.machinery.SourceFileLoader("smolvla_train_under_test", str(TRAIN_VLA_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def make_dataset(root: Path) -> Path:
    meta = root / "meta"
    meta.mkdir(parents=True)
    (meta / "info.json").write_text("{}", encoding="utf-8")
    return root


def make_args(train_vla, dataset_root: Path, output_dir: Path, *extra: str):
    return train_vla.build_parser().parse_args(
        ["--dataset_root", str(dataset_root), "--output_dir", str(output_dir), *extra]
    )


def test_resolve_paths_finds_unique_nested_dataset(train_vla, tmp_path, monkeypatch):
    dataset = make_dataset(tmp_path / "datasets" / "pick")
    checkpoint_dir = tmp_path / "checkpoints" / "job-001"
    monkeypatch.setenv("CHECKPOINT_DIR", str(checkpoint_dir))
    args = train_vla.build_parser().parse_args(["--dataset_root", str(dataset.parent)])

    resolved_dataset, output_dir = train_vla.resolve_paths(args)

    assert resolved_dataset == dataset
    assert output_dir == checkpoint_dir / "run"
    assert output_dir.parent.is_dir()


def test_build_train_command_preserves_platform_defaults(train_vla, tmp_path):
    dataset = make_dataset(tmp_path / "dataset")
    output_dir = tmp_path / "job" / "run"
    output_dir.parent.mkdir()
    args = make_args(train_vla, dataset, output_dir)

    command = train_vla.build_train_command(args, dataset, output_dir)

    assert command[0] == "lerobot-train"
    assert f"--dataset.root={dataset}" in command
    assert f"--output_dir={output_dir}" in command
    assert "--dataset.video_backend=pyav" in command
    assert "--policy.push_to_hub=false" in command
    assert f"--policy.path={train_vla.DEFAULT_POLICY}" in command
    assert command[-1] == "--resume=false"
    assert not any(item.startswith("--config_path=") for item in command)


def test_restart_moves_previous_attempt_and_observation_outputs(train_vla, tmp_path):
    dataset = make_dataset(tmp_path / "dataset")
    output_dir = tmp_path / "job" / "run"
    output_dir.mkdir(parents=True)
    (output_dir / "old.txt").write_text("old attempt", encoding="utf-8")
    metrics = output_dir.parent / "metrics.jsonl"
    metrics.write_text('{"step": 1, "loss": 0.5}\n', encoding="utf-8")
    tensorboard = output_dir.parent / "tensorboard"
    tensorboard.mkdir()
    (tensorboard / "events.old").write_text("old events", encoding="utf-8")
    args = make_args(train_vla, dataset, output_dir)

    train_vla.prepare_output_directory(args, output_dir)

    legacy = output_dir.parent / ".attempt-1"
    assert not output_dir.exists()
    assert (legacy / "old.txt").read_text(encoding="utf-8") == "old attempt"
    assert (legacy / "metrics.jsonl").read_text(encoding="utf-8") == '{"step": 1, "loss": 0.5}\n'
    assert (legacy / "tensorboard" / "events.old").read_text(encoding="utf-8") == "old events"
    assert not metrics.exists()
    assert not tensorboard.exists()


def test_restart_moves_orphan_observation_outputs_without_run_dir(train_vla, tmp_path):
    dataset = make_dataset(tmp_path / "dataset")
    output_dir = tmp_path / "job" / "run"
    output_dir.parent.mkdir()
    metrics = output_dir.parent / "metrics.jsonl"
    metrics.write_text('{"step": 2000, "loss": 0.1}\n', encoding="utf-8")
    args = make_args(train_vla, dataset, output_dir)

    train_vla.prepare_output_directory(args, output_dir)

    legacy_metrics = output_dir.parent / ".attempt-1" / "metrics.jsonl"
    assert legacy_metrics.read_text(encoding="utf-8") == '{"step": 2000, "loss": 0.1}\n'
    assert not metrics.exists()
    assert not output_dir.exists()


def test_resume_keeps_observation_outputs_in_place(train_vla, tmp_path):
    dataset = make_dataset(tmp_path / "dataset")
    output_dir = tmp_path / "job" / "run"
    output_dir.mkdir(parents=True)
    metrics = output_dir.parent / "metrics.jsonl"
    metrics.write_text('{"step": 100, "loss": 0.2}\n', encoding="utf-8")
    args = make_args(train_vla, dataset, output_dir, "--resume")

    train_vla.prepare_output_directory(args, output_dir)

    assert metrics.is_file()
    assert output_dir.is_dir()
    assert not (output_dir.parent / ".attempt-1").exists()


def test_resume_copies_last_checkpoint_and_uses_saved_config(train_vla, tmp_path, monkeypatch):
    dataset = make_dataset(tmp_path / "dataset")
    checkpoint_root = tmp_path / "checkpoints"
    source = checkpoint_root / "old-job" / "run" / "checkpoints" / "000100"
    pretrained = source / "pretrained_model"
    pretrained.mkdir(parents=True)
    config = pretrained / "train_config.json"
    config.write_text('{"steps": 2000}', encoding="utf-8")
    training_state = source / "training_state"
    training_state.mkdir()
    (training_state / "training_step.json").write_text('{"step": 100}', encoding="utf-8")
    (source.parent / "last").symlink_to(source.name)

    checkpoint_dir = checkpoint_root / "new-job"
    output_dir = checkpoint_dir / "run"
    output_dir.mkdir(parents=True)
    monkeypatch.setenv("CHECKPOINT_DIR", str(checkpoint_dir))
    monkeypatch.delenv("VLA_STEP_OFFSET", raising=False)
    args = make_args(train_vla, dataset, output_dir, "--resume", "--resume_from", "old-job")

    command = train_vla.build_train_command(args, dataset, output_dir)

    copied = output_dir / "checkpoints" / "000100"
    assert (copied / "pretrained_model" / "train_config.json").is_file()
    assert (output_dir / "checkpoints" / "last").readlink() == Path("000100")
    assert os.environ["VLA_STEP_OFFSET"] == "100"
    resume_config = output_dir / "checkpoints" / "last" / "pretrained_model" / "train_config.json"
    assert resume_config.is_file()
    assert f"--config_path={resume_config}" in command
    assert command[-1] == "--resume=true"
    assert not any(item.startswith("--policy.path=") for item in command)
    assert config.is_file()


def test_resume_rejects_missing_source_checkpoint(train_vla, tmp_path, monkeypatch):
    dataset = make_dataset(tmp_path / "dataset")
    checkpoint_dir = tmp_path / "checkpoints" / "new-job"
    output_dir = checkpoint_dir / "run"
    output_dir.mkdir(parents=True)
    monkeypatch.setenv("CHECKPOINT_DIR", str(checkpoint_dir))
    args = make_args(train_vla, dataset, output_dir, "--resume", "--resume_from", "missing-job")

    with pytest.raises(SystemExit, match="没有产物目录"):
        train_vla.build_train_command(args, dataset, output_dir)


def test_run_training_injects_metrics_boot_and_returns_child_status(train_vla, tmp_path, monkeypatch):
    captured = {}

    class FakeChild:
        returncode = 7

        def wait(self):
            return self.returncode

    def fake_popen(command, env):
        captured["command"] = command
        captured["env"] = env
        return FakeChild()

    monkeypatch.setattr(train_vla.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("PYTHONPATH", "/existing")

    result = train_vla.run_training(["lerobot-train", "--steps=2"], tmp_path / "run")

    assert result == 7
    assert captured["command"] == ["lerobot-train", "--steps=2"]
    boot_dir = str(TRAIN_VLA_PATH.parent / "metrics_boot")
    assert captured["env"]["PYTHONPATH"] == f"{boot_dir}{os.pathsep}/existing"
