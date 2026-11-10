#!/usr/bin/env python3
"""核对集群里的配置与仓库清单是否一致。

为什么需要这个：
`kubectl rollout restart` 只会重建 Pod，**不会重新应用 yaml 里的 env**。
所以改了 deploy/*.yaml 之后如果不 apply，集群会继续跑旧配置，
而 rollout 看起来是成功的——这种不一致极难发现。

真实踩过的坑：默认镜像在 yaml 里早就改成 vla-smolvla 了，
集群里却还是 vla-smoke:latest（那个镜像已经删了）。
走「高级提交」的用户会卡在 ImagePullBackOff，还占着 GPU 配额。

用法：
    python3 scripts/check_drift.py            # 只报告
    python3 scripts/check_drift.py --strict   # 有不一致时退出码 1（给 CI 用）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

try:
    import yaml
except ImportError:
    yaml = None

NAMESPACE = "vla-platform"
DEPLOYMENT = "vla-web"
OVERLAY = "deploy/web/overlays/4090"


def _env_map(container: dict) -> dict[str, str]:
    """把 env 列表转成 name -> 值。Secret 引用记成 secret:<key>。

    不直接比较原始结构：K8s 会在环境变量里加一些平台注入的字段
    （如 service link），逐字段比较会产生大量误报。
    """
    result: dict[str, str] = {}
    for item in container.get("env") or []:
        name = item.get("name", "")
        if "value" in item:
            result[name] = str(item["value"])
        elif "valueFrom" in item:
            ref = item["valueFrom"].get("secretKeyRef") or {}
            result[name] = f"secret:{ref.get('key', '')}"
        else:
            result[name] = "<empty>"
    return result


def repo_env() -> dict[str, str]:
    if yaml is None:
        raise SystemExit("需要 pyyaml：pip install pyyaml")
    out = subprocess.run(
        ["kubectl", "kustomize", OVERLAY],
        capture_output=True, text=True, check=True,
    ).stdout
    for doc in yaml.safe_load_all(out):
        if doc and doc.get("kind") == "Deployment":
            return _env_map(doc["spec"]["template"]["spec"]["containers"][0])
    raise SystemExit(f"{OVERLAY} 里没有 Deployment")


def cluster_env() -> dict[str, str]:
    out = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "get", "deploy", DEPLOYMENT, "-o", "json"],
        capture_output=True, text=True, check=True,
    ).stdout
    return _env_map(json.loads(out)["spec"]["template"]["spec"]["containers"][0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="有不一致时退出码 1")
    args = parser.parse_args()

    want = repo_env()
    have = cluster_env()

    drift = []
    for name, expected in sorted(want.items()):
        actual = have.get(name)
        if actual != expected:
            drift.append((name, expected, actual))

    if not drift:
        print(f"一致：{len(want)} 个环境变量与仓库清单相同")
        return 0

    print(f"发现 {len(drift)} 处不一致（仓库清单 vs 集群）：")
    for name, expected, actual in drift:
        print(f"  {name}")
        print(f"    仓库: {expected!r}")
        print(f"    集群: {actual!r}")
    print()
    print("修复：kubectl apply -k " + OVERLAY)
    print("注意 rollout restart 不会同步 env，必须 apply。")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
