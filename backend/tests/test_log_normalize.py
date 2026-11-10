

# ---------------------------------------------------------------------------
# 日志换行
#
# 训练进度条用 \r 做原地刷新，终端里是动态条，网页 <pre> 里却会把几十次
# 刷新拼成一条超长行，真正的 INFO 行全被挤到后面看不清。
# ---------------------------------------------------------------------------


def test_normalize_log_newlines_converts_carriage_return():
    from app.services.platform import normalize_log_newlines

    raw = "Training: 10%|\rTraining: 20%|\rINFO step:100 loss:0.5\n"

    result = normalize_log_newlines(raw)

    assert "\r" not in result
    assert result.count("\n") == 3
    assert "INFO step:100 loss:0.5" in result


def test_normalize_log_newlines_handles_crlf():
    from app.services.platform import normalize_log_newlines

    result = normalize_log_newlines("a\r\nb\r\nc")

    assert result == "a\nb\nc"


def test_normalize_log_newlines_keeps_empty():
    from app.services.platform import normalize_log_newlines

    assert normalize_log_newlines("") == ""
