"""失败诊断测试。

用真实的错误文本（来自 CUDA、K8s、Python 的实际输出）验证归一化是否准确。
诊断写得准不准，直接决定用户能不能自己把问题解决掉。
"""

from __future__ import annotations

import pytest
from app.services.diagnostics import DIAGNOSES, diagnose


def test_returns_none_for_successful_job():
    assert diagnose(status="SUCCEEDED") is None
    assert diagnose(status="RUNNING") is None
    assert diagnose(status="QUEUED") is None


def test_detects_image_pull_error_from_events():
    result = diagnose(
        status="FAILED",
        events=[
            {
                "reason": "Failed",
                "message": 'Failed to pull image "nope:doesnotexist": Error response from daemon',
            }
        ],
    )

    assert result is not None
    assert result["code"] == "image_pull_backoff"
    assert result["suggestion"]


def test_detects_cuda_oom_from_log():
    log = """
Traceback (most recent call last):
  File "/app/train.py", line 88, in <module>
    loss = model(batch)
torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB
"""
    result = diagnose(status="FAILED", log_tail=log)

    assert result is not None
    assert result["code"] == "oom_gpu"
    assert "batch" in result["suggestion"]


def test_detects_oomkilled_from_reason():
    result = diagnose(status="FAILED", reason="OOMKilled")

    assert result is not None
    assert result["code"] == "oom_container"


def test_detects_missing_python_module():
    log = "ModuleNotFoundError: No module named 'lerobot'"
    result = diagnose(status="FAILED", log_tail=log)

    assert result is not None
    assert result["code"] == "module_not_found"
    assert "镜像" in result["suggestion"]


def test_detects_missing_dataset():
    log = "FileNotFoundError: No such file or directory: '/datasets/pick1028/meta/info.json'"
    result = diagnose(status="FAILED", log_tail=log)

    assert result is not None
    assert result["code"] == "dataset_missing"


def test_detects_readonly_checkpoint():
    log = "sqlite3.OperationalError: attempt to write a readonly database"
    result = diagnose(status="FAILED", log_tail=log)
    # 这条日志不匹配 checkpoint 专用规则，应落到通用 traceback/兜底
    assert result is not None
    assert result["code"] in ("python_error", "checkpoint_write")


def test_detects_eviction_from_message():
    result = diagnose(status="FAILED", message="Pod was evicted due to disk pressure")

    assert result is not None
    # evicted 目前没有专门的正则；确保不会误判成别的高优先级问题
    assert result["code"] in ("python_error", "evicted")


def test_events_take_priority_over_logs():
    """事件是 K8s 层的确切原因，比日志里偶然出现的字符串更可信。"""
    result = diagnose(
        status="FAILED",
        events=[{"reason": "Failed", "message": "ErrImagePull"}],
        log_tail="Traceback (most recent call last):\n  ...",
    )

    assert result is not None
    assert result["code"] == "image_pull_backoff"


def test_falls_back_without_losing_information():
    result = diagnose(status="FAILED", message="something odd happened")

    assert result is not None
    assert result["code"] == "python_error"
    assert "something odd happened" in result["evidence"]


def test_all_diagnoses_have_summary_and_suggestion():
    for code, (summary, suggestion) in DIAGNOSES.items():
        assert summary, code
        assert len(suggestion) > 10, code


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ImagePullBackOff", "image_pull_backoff"),
        ("ErrImagePull: manifest unknown", "image_pull_backoff"),
        ("CUDA out of memory", "oom_gpu"),
        ("OOMKilled", "oom_container"),
        ("No module named 'torch'", "module_not_found"),
        ("No such file or directory: '/datasets/x'", "dataset_missing"),
        ("Read-only file system: /checkpoints/job1/metrics.jsonl", "checkpoint_write"),
        ("PermissionError: [Errno 13] Permission denied", "permission_denied"),
        ("NCCL error: unhandled cuda error", "nccl"),
        ("ValueError: dimension mismatch in action shape", "config_error"),
        ("Traceback (most recent call last):", "python_error"),
    ],
)
def test_pattern_table(text, expected):
    result = diagnose(status="FAILED", log_tail=text)

    assert result is not None
    assert result["code"] == expected

# ---------------------------------------------------------------------------
# 以下用例的错误文本都取自真实跑 SmolVLA 训练时的日志，不是编造的
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("expected", "log"),
    [
        (
            "hf_auth",
            "huggingface_hub.errors.HfHubHTTPError: 401 Client Error: Unauthorized "
            "for url: https://hf-mirror.com/api/repos/create",
        ),
        ("torchcodec", "RuntimeError: Could not load libtorchcodec"),
        (
            "output_dir_exists",
            "FileExistsError: Output directory /checkpoints/x/local already exists "
            "and resume is False. Please change your output directory",
        ),
        (
            "missing_required_arg",
            "draccus.utils.DecodingError: `dataset`: Missing required field(s) `repo_id` "
            "for DatasetConfig",
        ),
    ],
)
def test_real_training_failures_are_diagnosed(expected, log):
    """这些错误在接入真实训练时逐个遇到过，必须能归到正确的诊断码。

    若只靠 python_error 兜底，用户看到的是一句「训练脚本抛异常」，
    而真正的原因（缺 token、目录已存在、参数名不匹配）完全体现不出来。
    """
    result = diagnose(status="FAILED", reason=None, message=None, log_tail=log, events=[])

    assert result is not None
    assert result["code"] == expected


def test_hf_auth_suggestion_mentions_push():
    """训练成功后才抛 401 是最糟的失败形态，建议必须指向真正的原因。"""
    result = diagnose(
        status="FAILED",
        reason=None,
        message=None,
        log_tail="401 Client Error: Unauthorized for url: https://hf-mirror.com/api/repos/create",
        events=[],
    )

    assert "推送" in result["suggestion"] or "push" in result["suggestion"].lower()


# ---------------------------------------------------------------------------
# 共享内存不足：这是容器训练最容易踩、也最难自己看出来的坑
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "log",
    [
        "RuntimeError: DataLoader worker (pid 192) is killed by signal: Bus error.",
        "ERROR: Unexpected bus error encountered in worker. "
        "This might be caused by insufficient shared memory (shm).",
        "It is possible that dataloader's workers are out of shared memory.",
    ],
)
def test_shm_exhaustion_is_diagnosed(log):
    """Bus error 必须与其他 python 异常区分开。

    被归成 python_error 时提示是"查看 traceback 定位出错行"，
    而真正原因是共享内存小，用户看着 traceback 也想不到去改 /dev/shm。
    """
    result = diagnose(status="FAILED", reason=None, message=None, log_tail=log, events=[])

    assert result is not None
    assert result["code"] == "shm_exhausted"
    # 建议必须包含可执行的下一步
    assert "shm" in result["suggestion"].lower() or "worker" in result["suggestion"].lower()


def test_shm_suggestion_is_actionable():
    result = diagnose(
        status="FAILED",
        reason=None,
        message=None,
        log_tail="DataLoader worker is killed by signal: Bus error",
        events=[],
    )

    # 要么告诉用户调 shm，要么告诉用户降 worker，不能只说"去看看日志"
    suggestion = result["suggestion"].lower()
    assert any(word in suggestion for word in ("shm", "shared memory", "num_workers", "worker"))


def test_other_python_error_still_classified():
    """不能把普通 Python 异常也误判成 shm 问题。"""
    result = diagnose(
        status="FAILED",
        reason=None,
        message=None,
        log_tail="Traceback (most recent call last):\nValueError: bad config",
        events=[],
    )

    assert result is not None
    assert result["code"] != "shm_exhausted"
