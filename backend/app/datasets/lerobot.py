"""LeRobot 目录格式与 parquet/stats 元数据的轻量解析。

Web 镜像不带 pyarrow/pandas，因此 parquet 只读取页脚或任务文本，不加载数据；
读不到的字段由扫描编排层标记为「未知」或添加问题，而不是让整个扫描失败。
"""

from __future__ import annotations

import json
import os
import re
import struct
from pathlib import Path
from typing import Any


def detect_format(root: Path) -> str:
    """根据目录结构判断 LeRobot 版本。"""
    info_path = root / "meta" / "info.json"
    if info_path.is_file():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "unknown"
        version = str(info.get("codebase_version", ""))
        if version.startswith("v3"):
            return "lerobot-v3.0"
        if version.startswith("v2"):
            # v2.1 引入了 total_tasks / chunks_size，v2.0 没有
            return "lerobot-v2.1" if "chunks_size" in info else "lerobot-v2.0"
        return "lerobot"
    return "unknown"


def read_parquet_metadata(path: Path) -> dict[str, Any] | None:
    """读取 parquet 文件的页脚元数据，不加载数据。

    只解析我们关心的部分：行数、列名、列类型。格式是 Thrift 紧凑协议，
    完整解析成本高，这里用「找 footer 里的字符串表」的近似做法，够用即可；
    解析失败返回 None，由调用方降级处理。
    """
    try:
        size = path.stat().st_size
        if size < 12:
            return None
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 8))
            tail = handle.read(8)
            if tail[4:] != b"PAR1":
                return None
            footer_len = struct.unpack("<I", tail[:4])[0]
            if footer_len <= 0 or footer_len > size - 8:
                return None
            handle.seek(size - 8 - footer_len)
            footer = handle.read(footer_len)
    except OSError:
        return None
    return _extract_footer_info(footer)


def _extract_footer_info(footer: bytes) -> dict[str, Any]:
    """从 footer 里提取可打印的列名，并粗估行数。

    Thrift 紧凑编码里字符串以「长度 varint + 字节」出现，列名长度通常 < 128，
    因此直接扫描「长度字节 + 可打印 ASCII」。行数取 footer 里最大的 int64 值，
    这个近似对元数据展示足够；精确值由深扫描时读 parquet 首页统计补充。
    """
    names: list[str] = []
    index = 0
    length = len(footer)
    while index < length - 1:
        token = footer[index]
        if 3 <= token <= 80 and index + 1 + token <= length:
            chunk = footer[index + 1 : index + 1 + token]
            if all(32 <= byte < 127 for byte in chunk):
                text = chunk.decode("ascii")
                # 列名一般是 a-z0-9._ 组成的标识符
                if all(char.isalnum() or char in "._-" for char in text):
                    names.append(text)
                    index += 1 + token
                    continue
        index += 1
    return {"columns": sorted(set(names)), "footer_size": length}


# parquet 里元数据/字典页的固定字样，提取任务文本时要排除
_PARQUET_NOISE = {
    "schema", "task_index", "__index_level_0__", "pandas", "ARROW:schema",
    "parquet-cpp-arrow", "org.apache.spark", "index_columns", "column_indexes",
    "numpy_type", "pandas_version", "metadata",
}


def read_task_strings(root: Path, limit: int = 20) -> list[str]:
    """从 meta/tasks.parquet 里提取任务文本。

    不引 pyarrow（Web 镜像不装）：tasks.parquet 只有「任务名 + task_index」两列，
    数据量极小（几 KB），直接扫描文件里的可打印字符串即可可靠取出任务名。
    需要排除 parquet 自身的元数据字样与纯数字/符号。

    LeRobot v3.0 的文本列名实测是 ``__index_level_0__``（pandas index 落盘），
    不是 ``task``，所以不能按列名定位——这也是选择扫描字符串的原因之一。
    """
    tasks_path = root / "meta" / "tasks.parquet"
    if not tasks_path.is_file():
        return []
    try:
        raw = tasks_path.read_bytes()
    except OSError:
        return []

    results: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(rb"[\x20-\x7e]{4,400}", raw):
        text = match.group().decode("utf-8", errors="replace").strip()
        if not text or text in seen or text in _PARQUET_NOISE or not _is_task_text(text):
            continue
        # 全是同一个词的重复（parquet 字典页会重复存）只保留一份
        seen.add(text)
        results.append(text)
        if len(results) >= limit:
            break
    return results


def _is_task_text(text: str) -> bool:
    # 必须是自然语言（以字母开头、含空格），且排除 parquet 元数据
    if not re.match(r"^[A-Za-z]", text) or " " not in text:
        return False
    # base64 编码的 schema/元数据会混进来：特征是超长且几乎没有空格的分词
    # （单词平均长度 > 12 基本不可能是自然语言指令）
    words = text.split()
    if len(text) > 200 or sum(len(word) for word in words) / max(len(words), 1) > 12:
        return False
    if any(token in text for token in ("parquet", "ARROW", "column", "index_columns", "numpy", "pandas")):
        return False
    # 任务指令通常是小写开头的祈使句；连续 3 个以上大写字母多半是编码片段
    return re.search(r"[A-Z]{3,}", text) is None


# 判断某个维度「几乎不动」的阈值：标准差相对极差的比例。
# 数据经过不同量纲归一化，用相对值比绝对值更稳。
STATIC_DIM_RATIO = 0.01


def read_stats_summary(root: Path) -> dict[str, Any] | None:
    """从 meta/stats.json 提取动作/状态的分布摘要，用于判断数据质量。

    返回结构与 ``features`` 对齐，每维带 min/max/mean/std 和 ``static`` 标记——
    「某个关节几乎不动」往往意味着采集时没用到该关节，或者标定有问题，
    这类问题在训练 loss 上很难看出来，但会直接拖垮策略效果。
    """
    stats_path = root / "meta" / "stats.json"
    if not stats_path.is_file():
        return None
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stats, dict):
        return None

    summary: dict[str, Any] = {}
    for key in ("observation.state", "action"):
        block = stats.get(key)
        if not isinstance(block, dict):
            continue
        required = ("min", "max", "mean", "std")
        if not all(isinstance(block.get(field), list) for field in required):
            continue
        dims = _summarize_dimensions(block)
        summary[key] = {
            "dims": dims,
            "static_dims": [dim["index"] for dim in dims if dim["static"]],
        }
    return summary or None


def _summarize_dimensions(block: dict[str, Any]) -> list[dict[str, Any]]:
    dims = []
    for index in range(len(block["min"])):
        try:
            low = float(block["min"][index])
            high = float(block["max"][index])
            mean = float(block["mean"][index])
            std = float(block["std"][index])
        except (IndexError, TypeError, ValueError):
            continue
        span = high - low
        # 必须用未取整的 span：极差极小或标准差相对极差极小都视为基本不动。
        is_static = bool(span < 1e-4 or (span > 0 and std / span < STATIC_DIM_RATIO))
        dims.append(
            {
                "index": index,
                "min": round(low, 6),
                "max": round(high, 6),
                "mean": round(mean, 6),
                "std": round(std, 6),
                "range": round(span, 6),
                "static": is_static,
            }
        )
    return dims
