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
