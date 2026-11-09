from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


class GpuMetricsClient:
    def __init__(self, url: str, timeout: float = 2.0):
        self.url = url
        self.timeout = timeout

    def snapshot(self) -> dict[str, Any]:
        if not self.url:
            return {"available": False, "items": []}
        try:
            with urlopen(self.url, timeout=self.timeout) as response:
                payload = json.load(response)
            if not isinstance(payload, list):
                raise ValueError("GPU metrics response is not a list")
            items = [_validate_metric(item) for item in payload]
            return {"available": True, "items": items}
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, KeyError):
            return {"available": False, "items": []}


def _validate_metric(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise TypeError("GPU metric must be an object")
    return {
        "index": int(item["index"]),
        "name": str(item["name"]),
        "utilization_percent": _percentage(item["utilization_percent"]),
        "memory_used_mib": _non_negative_number(item["memory_used_mib"]),
        "memory_total_mib": _non_negative_number(item["memory_total_mib"]),
        "temperature_celsius": _non_negative_number(item["temperature_celsius"]),
    }


def _percentage(value: Any) -> float:
    number = _non_negative_number(value)
    if number > 100:
        raise ValueError("GPU utilization exceeds 100")
    return number


def _non_negative_number(value: Any) -> float:
    number = float(value)
    if number < 0:
        raise ValueError("GPU metric cannot be negative")
    return number
