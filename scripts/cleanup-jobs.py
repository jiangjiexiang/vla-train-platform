#!/usr/bin/env python3
"""清理已结束的历史任务。

通过 REST API 删除，而不是直接 kubectl delete —— 这样删除动作会进入平台历史，
并且保留任务的真实终态（SUCCEEDED / FAILED 不会被改写成 CANCELLED）。

默认只列出将要删除的任务（dry-run），确认后加 --apply。

用法：
  scripts/cleanup-jobs.py                          # 列出 7 天前结束的任务
  scripts/cleanup-jobs.py --older-than-days 1      # 列出 1 天前结束的
  scripts/cleanup-jobs.py --apply                  # 真正删除
  scripts/cleanup-jobs.py --status SUCCEEDED       # 只清理成功的
  scripts/cleanup-jobs.py --keep smoke001 --keep w # 明确保留某些任务
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_STATUSES = ("SUCCEEDED", "FAILED")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="清理平台上已结束的历史任务")
    parser.add_argument("--server", default=os.environ.get("PLATFORM_SERVER", "http://127.0.0.1:30080"))
    parser.add_argument("--token", default=os.environ.get("PLATFORM_API_TOKEN", ""))
    parser.add_argument("--older-than-days", type=float, default=7.0,
                        help="只清理结束时间早于 N 天的任务（默认 7）")
    parser.add_argument("--status", action="append", choices=sorted(DEFAULT_STATUSES),
                        help="只清理指定状态，可重复；默认 SUCCEEDED 与 FAILED")
    parser.add_argument("--keep", action="append", default=[], metavar="NAME",
                        help="明确保留的任务名，可重复")
    parser.add_argument("--apply", action="store_true", help="真正执行删除；默认只预览")
    parser.add_argument("--page-size", type=int, default=200, help="分页大小（1-200，默认 200）")
    args = parser.parse_args(argv)
    if not 1 <= args.page_size <= 200:
        parser.error("--page-size 必须在 1-200 之间")
    if args.older_than_days < 0:
        parser.error("--older-than-days 不能为负")
    return args


def api_base(server: str) -> str:
    server = server.rstrip("/")
    return server if server.endswith("/api/v1") else server + "/api/v1"


def request(base, token, method, path, timeout=30.0):
    url = base + path
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(512).decode("utf-8", errors="replace")
        raise SystemExit(f"[ERROR] {method} {path} -> HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"[ERROR] 无法连接 {url}: {exc}") from exc
    return json.loads(raw) if raw else {}


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def collect_jobs(base, token, statuses, page_size):
    seen = []
    for status in statuses:
        offset = 0
        while True:
            payload = request(base, token, "GET", f"/jobs?status={status}&limit={page_size}&offset={offset}")
            items = payload.get("items", [])
            seen.extend(items)
            total = payload.get("total", 0)
            offset += len(items)
            if not items or offset >= total:
                break
    return seen


def main(argv=None):
    args = parse_args(argv)
    base = api_base(args.server)
    statuses = tuple(args.status or DEFAULT_STATUSES)
    keep = set(args.keep)

    jobs = collect_jobs(base, args.token, statuses, args.page_size)
    now = datetime.now(timezone.utc)
    cutoff_seconds = args.older_than_days * 86400

    candidates = []
    for job in jobs:
        name = job.get("name")
        if not name or name in keep:
            continue
        finished = parse_time(job.get("finished_at")) or parse_time(job.get("created_at"))
        if finished is None:
            continue
        age = (now - finished).total_seconds()
        if age >= cutoff_seconds:
            candidates.append((age, job))

    candidates.sort(key=lambda item: item[0], reverse=True)

    if not candidates:
        print(f"没有需要清理的任务（状态 {', '.join(statuses)}，早于 {args.older_than_days:g} 天）。")
        return 0

    print(f"符合条件：{len(candidates)} 个（状态 {', '.join(statuses)}，早于 {args.older_than_days:g} 天）")
    print(f"{'任务名':<28} {'状态':<10} {'结束时间':<22} {'已过天数':>8}")
    for age, job in candidates:
        finished = job.get("finished_at") or job.get("created_at")
        print(f"{job['name']:<28} {job.get('status', '-'):<10} {finished or '-':<22} {age / 86400:>8.1f}")

    if not args.apply:
        print("\n这是预览。确认无误后加 --apply 执行删除。")
        return 0

    print("\n开始删除……")
    failures = 0
    for _, job in candidates:
        name = job["name"]
        try:
            result = request(base, args.token, "DELETE", f"/jobs/{name}")
            print(f"  已删除 {name} -> {result.get('status')}")
        except SystemExit as exc:
            failures += 1
            print(f"  删除 {name} 失败：{exc}")
    print(f"\n完成：成功 {len(candidates) - failures}，失败 {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
