from __future__ import annotations

import pytest
from app.models import (
    MAX_COMMAND_BYTES,
    MAX_ENV_BYTES,
    SubmitRequest,
    SubmitSpec,
    check_payload_size,
)
from pydantic import ValidationError


def make_spec(**overrides):
    payload = {
        "name": "exp001",
        "image": "vla-smoke:latest",
        "gpus": 1,
        "queue": "default",
        "priority": "normal",
        "command": "python /app/train.py",
        "env": {"PYTHONUNBUFFERED": "1"},
        "max_retries": 0,
    }
    payload.update(overrides)
    return SubmitSpec(**payload)


def test_minimal_spec_is_valid():
    spec = make_spec()
    assert spec.name == "exp001"
    assert spec.gpus == 1


@pytest.mark.parametrize("name", ["Exp001", "exp_001", "-exp", "exp-", "", "a" * 51])
def test_invalid_names_rejected(name):
    with pytest.raises(ValidationError):
        make_spec(name=name)


def test_priority_must_be_known_value():
    with pytest.raises(ValidationError):
        make_spec(priority="urgent")


def test_max_retries_limited_to_two():
    with pytest.raises(ValidationError):
        make_spec(max_retries=3)


def test_grace_period_defaults_to_30():
    assert make_spec().termination_grace_period_seconds == 30


@pytest.mark.parametrize("value", [0, 601])
def test_grace_period_bounds(value):
    with pytest.raises(ValidationError):
        make_spec(termination_grace_period_seconds=value)


def test_gpus_must_be_positive():
    with pytest.raises(ValidationError):
        make_spec(gpus=0)


def test_env_key_must_be_identifier():
    with pytest.raises(ValidationError):
        make_spec(env={"BAD-NAME": "x"})


def test_command_rejects_nul_byte():
    with pytest.raises(ValidationError):
        make_spec(command="echo \x00")


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        make_spec(privileged=True)


def test_submit_request_applies_defaults():
    request = SubmitRequest(name="exp002", gpus=1, image=None, command=None)
    spec = request.with_defaults("vla-smoke:latest", "python /app/train.py")
    assert spec.image == "vla-smoke:latest"
    assert spec.command == "python /app/train.py"


def test_submit_request_accepts_explicit_null():
    """A client sending "image": null must fall back to the default, not 500."""
    request = SubmitRequest(name="exp003", gpus=1, image=None, command=None)
    assert request.image is None
    spec = request.with_defaults("vla-smoke:latest", "python /app/train.py")
    assert spec.image == "vla-smoke:latest"


def test_blank_image_still_rejected():
    with pytest.raises(ValidationError):
        SubmitRequest(name="exp004", gpus=1, image="   ")


def test_payload_size_accepts_normal_spec():
    check_payload_size(make_spec())


def test_payload_size_rejects_long_command():
    with pytest.raises(ValueError, match="command"):
        check_payload_size(make_spec(command="a" * (MAX_COMMAND_BYTES + 1)))


def test_payload_size_rejects_large_env():
    env = {f"K{i}": "v" * 4000 for i in range(20)}
    assert sum(len(k) + len(v) for k, v in env.items()) > MAX_ENV_BYTES
    with pytest.raises(ValueError, match="env"):
        check_payload_size(make_spec(env=env))


def test_oversized_spec_can_still_be_parsed_back():
    """Size caps must not apply on read, or legacy jobs become unreadable."""
    oversized = make_spec(command="a" * (MAX_COMMAND_BYTES + 10))
    assert SubmitSpec.model_validate(oversized.model_dump()).name == "exp001"
