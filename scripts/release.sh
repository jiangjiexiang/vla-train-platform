#!/usr/bin/env bash
set -euo pipefail

# 发一个大版本：校验版本号与 CHANGELOG、可选跑验收、更新 VERSION、提交并打 tag。
# 约定见 README「版本管理」一节。

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
VERSION_FILE="$ROOT_DIR/VERSION"
CHANGELOG_FILE="$ROOT_DIR/CHANGELOG.md"
DRY_RUN=0

usage() {
  cat <<EOF
用法：scripts/release.sh <版本号> [--dry-run]

  <版本号>    形如 0.2.0，必须大于当前 VERSION 中的版本
  --dry-run  只打印将要执行的动作，不修改文件、不提交、不打 tag

前置条件：
  1. 工作区干净（所有改动已提交）
  2. CHANGELOG.md 已存在 "## [<版本号>]" 小节
  3. 未设置 SKIP_VERIFY=1 时，scripts/verify-4090.sh 必须通过

示例：
  # 先写好 CHANGELOG 并提交
  \$EDITOR CHANGELOG.md && git add CHANGELOG.md && git commit -m "docs: 记录 v0.2.0 变更"
  scripts/release.sh 0.2.0
EOF
}

die() { printf "[ERROR] %s\n" "$*" >&2; exit 1; }
log() { printf "==> %s\n" "$*"; }

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    --dry-run) DRY_RUN=1 ;;
    -*) usage >&2; die "未知参数：$arg" ;;
    *) NEW_VERSION="$arg" ;;
  esac
done

[[ -n "${NEW_VERSION:-}" ]] || { usage >&2; die "缺少版本号"; }
[[ "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "版本号必须形如 X.Y.Z：$NEW_VERSION"

cd "$ROOT_DIR"
[[ -f "$VERSION_FILE" ]] || die "缺少 $VERSION_FILE"
CURRENT_VERSION=$(tr -d "[:space:]" < "$VERSION_FILE")
[[ -n "$CURRENT_VERSION" ]] || die "$VERSION_FILE 为空"

if [[ "$NEW_VERSION" == "$CURRENT_VERSION" ]]; then
  die "版本号与当前值相同：$CURRENT_VERSION"
fi
HIGHEST=$(printf "%s\n%s\n" "$CURRENT_VERSION" "$NEW_VERSION" | sort -V | tail -1)
[[ "$HIGHEST" == "$NEW_VERSION" ]] || die "新版本 $NEW_VERSION 必须大于当前版本 $CURRENT_VERSION"

[[ -f "$CHANGELOG_FILE" ]] || die "缺少 $CHANGELOG_FILE"
grep -qE "^## \[${NEW_VERSION//./\\.}\]" "$CHANGELOG_FILE" \
  || die "CHANGELOG.md 中没有 \"## [$NEW_VERSION]\" 小节，请先补写并提交"

if [[ -n "$(git status --porcelain)" ]]; then
  die "工作区不干净，请先提交或清理：$(git status --porcelain | head -5 | tr "\n" " ")"
fi

if [[ "$DRY_RUN" != "1" && "${SKIP_VERIFY:-0}" != "1" ]]; then
  log "运行验收脚本 scripts/verify-4090.sh"
  bash "$SCRIPT_DIR/verify-4090.sh" || die "验收未通过，已中止发版（可用 SKIP_VERIFY=1 跳过）"
fi

log "版本：$CURRENT_VERSION -> $NEW_VERSION"
if [[ "$DRY_RUN" == "1" ]]; then
  log "[dry-run] 将写入 VERSION、提交 \"release: v$NEW_VERSION\" 并打 tag v$NEW_VERSION"
  exit 0
fi

printf "%s\n" "$NEW_VERSION" > "$VERSION_FILE"
git add "$VERSION_FILE"
git commit -q -m "release: v$NEW_VERSION"
git tag -a "v$NEW_VERSION" -m "v$NEW_VERSION"
log "已提交并打 tag v$NEW_VERSION"
git --no-pager log --oneline -1
