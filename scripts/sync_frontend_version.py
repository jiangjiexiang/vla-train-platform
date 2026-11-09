#!/usr/bin/env python3
"""把 frontend/package.json 的 version 字段改成指定值，供 release.sh 调用。"""

import re
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: sync_frontend_version.py <package.json> <version>", file=sys.stderr)
        return 2
    path, version = sys.argv[1], sys.argv[2]
    text = open(path, encoding="utf-8").read()
    updated, count = re.subn(
        r'("version"\s*:\s*")[^"]*(")',
        lambda match: match.group(1) + version + match.group(2),
        text,
        count=1,
    )
    if count != 1:
        print(f"cannot locate the version field in {path}", file=sys.stderr)
        return 1
    open(path, "w", encoding="utf-8").write(updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
