"""失败原因聚合统计。

单任务的诊断告诉用户「这次为什么挂」，但运维需要的是「最近都在挂什么」：
如果十次失败里有七次是同一个原因，那可能是平台配置问题而不是用户误用。

这里按诊断码聚合，同时给出每个码最近的一个代表任务——点进去就能看到原始日志。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

FAILED_STATUSES = ("FAILED",)


def summarize_failures(
    jobs: list[dict[str, Any]],
    diagnoses: dict[str, dict[str, Any] | None],
    *,
    limit: int = 10,
) -> dict[str, Any]:
    """按诊断码聚合失败任务。

    ``diagnoses`` 是 {任务名: 诊断结果}。失败但没有诊断结论的归到 unknown——
    不要假装它们有原因。

    返回结构包含 ``example_job``：一个数字看不出任何东西，
    能一键跳到具体任务看日志，排查才有起点。
    """
    counts: Counter[str] = Counter()
    examples: dict[str, dict[str, Any]] = {}
    unknown = 0

    failed = [job for job in jobs if str(job.get("status")) in FAILED_STATUSES]
    for job in failed:
        name = str(job.get("name") or "")
        result = diagnoses.get(name)
        code = (result or {}).get("code")
        if not code:
            unknown += 1
            continue
        counts[code] += 1
        if code not in examples:
            examples[code] = {
                "job": name,
                "summary": result.get("summary"),
                "suggestion": result.get("suggestion"),
            }

    top = [
        {
            "code": code,
            "count": count,
            "summary": (examples.get(code) or {}).get("summary"),
            "suggestion": (examples.get(code) or {}).get("suggestion"),
            "example_job": (examples.get(code) or {}).get("job"),
        }
        for code, count in counts.most_common(limit)
    ]

    return {
        "total_failed": len(failed),
        "classified": len(failed) - unknown,
        "unknown": unknown,
        "reasons": top,
    }
