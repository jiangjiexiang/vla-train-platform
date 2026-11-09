from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).resolve().parents[2] / "cli" / "vla-train"


def load_cli():
    loader = importlib.machinery.SourceFileLoader("vla_train_cli", str(CLI_PATH))
    spec = importlib.util.spec_from_loader("vla_train_cli", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cli():
    return load_cli()


def test_normalize_server_strips_trailing_slash(cli):
    assert cli.normalize_server("http://host:30080/") == "http://host:30080"


@pytest.mark.parametrize(
    "value",
    ["ftp://host", "host:30080", "http://user:pass@host", "http://host/?a=1", "http://host/#frag", ""],
)
def test_normalize_server_rejects_bad_input(cli, value):
    with pytest.raises(cli.CliError):
        cli.normalize_server(value)


def test_api_base_appends_prefix_once(cli):
    assert cli.api_base("http://h:30080") == "http://h:30080/api/v1"
    assert cli.api_base("http://h:30080/api/v1") == "http://h:30080/api/v1"


def test_validate_name(cli):
    assert cli.validate_name("exp-001") == "exp-001"
    for bad in ("Exp001", "exp_001", "-exp", "a" * 51):
        with pytest.raises(cli.CliError):
            cli.validate_name(bad)


def test_parse_env(cli):
    assert cli.parse_env(["A=1", "B=x=y"]) == {"A": "1", "B": "x=y"}
    for bad in (["NOEQUALS"], ["1BAD=x"]):
        with pytest.raises(cli.CliError):
            cli.parse_env(bad)


def test_quoted_name_escapes(cli):
    assert cli.quoted_name("exp-1") == "exp-1"


def test_parser_accepts_events(cli):
    parser = cli.build_parser()
    args = parser.parse_args(["events", "exp001", "--json"])
    assert args.action == "events"
    assert args.name == "exp001"
    assert args.json is True

    args = parser.parse_args(["events"])
    assert args.action == "events"
    assert args.name is None
    assert args.json is False


def test_parser_requires_submit_fields(cli):
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["submit", "--name", "exp001"])
    args = parser.parse_args(
        ["submit", "--name", "exp001", "--gpus", "1", "--command", "python /app/train.py"]
    )
    assert args.gpus == 1


def test_parser_accepts_grace_period(cli):
    parser = cli.build_parser()
    args = parser.parse_args(
        ["submit", "--name", "exp001", "--gpus", "1", "--command", "true", "--termination-grace-period", "5"]
    )
    assert args.termination_grace_period == 5

    args = parser.parse_args(["submit", "--name", "exp001", "--gpus", "1", "--command", "true"])
    assert args.termination_grace_period == 30


def test_print_events_handles_empty(cli, capsys):
    cli.print_events("exp001", {"name": "exp001", "items": []}, False)
    assert "暂无事件" in capsys.readouterr().out


def test_print_events_rejects_bad_payload(cli):
    with pytest.raises(cli.CliError):
        cli.print_events("exp001", {"name": "exp001"}, False)


def test_print_events_renders_fields(cli, capsys):
    payload = {
        "name": "exp001",
        "items": [
            {
                "type": "Warning",
                "reason": "Failed",
                "message": "Failed to pull image",
                "count": 3,
                "object_kind": "Pod",
                "object_name": "exp001-master-0",
                "last_seen": "2026-01-01T00:00:09Z",
            }
        ],
    }
    cli.print_events("exp001", payload, False)
    out = capsys.readouterr().out
    assert "Warning" in out and "Failed" in out and "x3" in out
    assert "Failed to pull image" in out


def test_parse_labels(cli):
    assert cli.parse_labels(["team=perc", "stage=v2"]) == {"team": "perc", "stage": "v2"}
    assert cli.parse_labels([]) == {}
    for bad in (["NOEQUALS"], ["Bad=x"], ["k="], ["k=v"][:0] + ["chain=" + "x" * 300]):
        with pytest.raises(cli.CliError):
            cli.parse_labels(bad)


def test_parser_accepts_login_and_node_flags(cli):
    parser = cli.build_parser()
    args = parser.parse_args(["login", "--username", "alice", "--password-stdin"])
    assert args.action == "login" and args.username == "alice" and args.password_stdin is True
    assert parser.parse_args(["whoami"]).action == "whoami"
    assert parser.parse_args(["logout"]).action == "logout"

    args = parser.parse_args(
        ["submit", "--name", "exp001", "--nodes", "2", "--gpus-per-node", "1", "--command", "true"]
    )
    # --gpus 在多机时可省略，由 nodes * gpus_per_node 推导。
    assert args.gpus is None and args.nodes == 2 and args.gpus_per_node == 1


def test_submit_dry_run_derives_gpus_for_multi_node(cli, monkeypatch, capsys, tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"server": "http://h:30080", "token": "t"}', encoding="utf-8")
    config.chmod(0o600)
    monkeypatch.setattr(cli, "default_config_path", lambda: config)
    import sys as _sys

    monkeypatch.setattr(
        _sys,
        "argv",
        [
            "vla-train", "submit", "--name", "exp001", "--nodes", "2",
            "--gpus-per-node", "4", "--command", "true", "--label", "a=b", "--dry-run",
        ],
    )
    assert cli.main() == 0
    import json as _json

    body = _json.loads(capsys.readouterr().out)["body"]
    assert body["gpus"] == 8
    assert body["nodes"] == 2
    assert body["labels"] == {"a": "b"}


def test_submit_rejects_inconsistent_gpu_flags(cli, monkeypatch, tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text('{"server": "http://h:30080", "token": "t"}', encoding="utf-8")
    config.chmod(0o600)
    monkeypatch.setattr(cli, "default_config_path", lambda: config)
    import sys as _sys

    for argv in (
        ["submit", "--name", "exp001", "--nodes", "2", "--command", "true"],
        ["submit", "--name", "exp001", "--gpus", "3", "--nodes", "2", "--gpus-per-node", "1", "--command", "true"],
        ["submit", "--name", "exp001", "--command", "true"],
        ["submit", "--name", "exp001", "--gpus", "1", "--gpus-per-node", "1", "--command", "true"],
    ):
        monkeypatch.setattr(_sys, "argv", ["vla-train", *argv, "--dry-run"])
        assert cli.main() == 2, argv
