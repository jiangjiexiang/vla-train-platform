#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
PROFILE="${PROFILE:-4090}"
WEB_IMAGE="${WEB_IMAGE:-vla-platform:local}"
WEB_IMAGE_TAR="${WEB_IMAGE_TAR:-}"
SMOKE_IMAGE_TAR="${SMOKE_IMAGE_TAR:-}"
BUILD_IMAGE="${BUILD_IMAGE:-auto}"
WEB_DOCKERFILE="${WEB_DOCKERFILE:-$ROOT_DIR/backend/Dockerfile}"
WEB_BUILD_CONTEXT="${WEB_BUILD_CONTEXT:-$ROOT_DIR}"
PLATFORM_API_TOKEN_FILE="${PLATFORM_API_TOKEN_FILE:-}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-300s}"

usage() {
  cat <<EOF
Usage: sudo PROFILE=4090|a100 PLATFORM_API_TOKEN=<secret> scripts/deploy-web.sh

Deploys the single-container FastAPI/React Web image and NodePort 30080. The
4090 profile allows 1 GPU; a100 allows 1,2,4,8. PLATFORM_API_TOKEN is required
only when the vla-web-auth Secret does not already exist. Supplying a new token
updates the Secret and restarts the Web Deployment. PLATFORM_API_TOKEN_FILE may
be used instead of the environment variable; the token is never printed.

No external registry is required. The default image is vla-platform:local and
imagePullPolicy is IfNotPresent. Image sources, in priority order:
  WEB_IMAGE_TAR=/path/to/web-image.tar       # import this exact archive
  BUILD_IMAGE=1                              # force a local build/import
  existing image in K3s containerd           # reused when BUILD_IMAGE=auto
  BUILD_IMAGE=auto                           # build only if image is absent

WEB_IMAGE_TAR is always imported as-is and cannot be combined with BUILD_IMAGE=1.
Local builds prefer Docker when both tools are installed, and otherwise fall
back to Buildah with --isolation=chroot. The resulting image is exported as a
docker-archive and imported with k3s ctr; no registry push is performed.
Neither builder is installed or configured by this script. Offline builds still
require all base/build images to be available in the local builder cache.

Optional build overrides:
  WEB_IMAGE=vla-platform:local
  WEB_DOCKERFILE=$ROOT_DIR/backend/Dockerfile
  WEB_BUILD_CONTEXT=$ROOT_DIR

Optional smoke image import:
  SMOKE_IMAGE_TAR=/path/to/vla-smoke-latest.tar
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ $# -gt 0 ]]; then
  usage >&2
  exit 2
fi

log() { printf '\n==> %s\n' "$*"; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
case "$PROFILE" in
  4090|a100) ;;
  *) die "PROFILE must be 4090 or a100" ;;
esac
case "$BUILD_IMAGE" in
  auto|0|1) ;;
  *) die "BUILD_IMAGE must be auto, 0, or 1" ;;
esac
[[ -n "$WEB_IMAGE" ]] || die "WEB_IMAGE cannot be empty"
if [[ -n "${PLATFORM_API_TOKEN:-}" && -n "$PLATFORM_API_TOKEN_FILE" ]]; then
  die "Set only one of PLATFORM_API_TOKEN and PLATFORM_API_TOKEN_FILE"
fi
if (( EUID == 0 )); then
  SUDO=()
elif command -v sudo >/dev/null 2>&1; then
  SUDO=(sudo)
else
  die "Run as root or install sudo"
fi
run_root() { "${SUDO[@]}" "$@"; }
command -v k3s >/dev/null 2>&1 || run_root test -x /usr/local/bin/k3s || die "K3s is not installed"
kube() { run_root k3s kubectl "$@"; }
kube get nodes --request-timeout=10s >/dev/null || die "Cannot reach K3s"

TEMP_FILES=()
cleanup() {
  if (( ${#TEMP_FILES[@]} > 0 )); then
    rm -f -- "${TEMP_FILES[@]}"
  fi
}
trap cleanup EXIT

image_present() {
  local wanted=$1
  run_root k3s ctr images list --quiet 2>/dev/null | awk -v wanted="$wanted" '
    $0 == wanted { found=1 }
    index(wanted, "/") == 0 && $0 == "docker.io/library/" wanted { found=1 }
    END { exit(found ? 0 : 1) }
  '
}
import_image() {
  local archive=$1
  [[ -f "$archive" ]] || die "Image archive does not exist: $archive"
  run_root k3s ctr images import "$archive"
}

if [[ -n "$WEB_IMAGE_TAR" && "$BUILD_IMAGE" == "1" ]]; then
  die "WEB_IMAGE_TAR and BUILD_IMAGE=1 are mutually exclusive; choose one image source"
fi
if [[ -n "$WEB_IMAGE_TAR" ]]; then
  log "Importing Web image archive into K3s containerd"
  import_image "$WEB_IMAGE_TAR"
elif [[ "$BUILD_IMAGE" == "1" ]] || { [[ "$BUILD_IMAGE" == "auto" ]] && ! image_present "$WEB_IMAGE"; }; then
  [[ -f "$WEB_DOCKERFILE" ]] || die "Web Dockerfile not found: $WEB_DOCKERFILE; provide WEB_IMAGE_TAR or preload $WEB_IMAGE"
  [[ -d "$WEB_BUILD_CONTEXT" ]] || die "Web build context is not a directory: $WEB_BUILD_CONTEXT"
  if command -v docker >/dev/null 2>&1; then
    log "Building $WEB_IMAGE with Docker (no registry push)"
    docker build -f "$WEB_DOCKERFILE" -t "$WEB_IMAGE" "$WEB_BUILD_CONTEXT"
    WEB_ARCHIVE=$(mktemp --suffix=.tar)
    TEMP_FILES+=("$WEB_ARCHIVE")
    docker save --output "$WEB_ARCHIVE" "$WEB_IMAGE"
    import_image "$WEB_ARCHIVE"
  elif command -v buildah >/dev/null 2>&1; then
    BUILDAH_SOURCE_REF=$WEB_IMAGE
    BUILDAH_ARCHIVE_REF=$WEB_IMAGE
    if [[ "$WEB_IMAGE" != */* ]]; then
      BUILDAH_SOURCE_REF="localhost/$WEB_IMAGE"
      BUILDAH_ARCHIVE_REF="docker.io/library/$WEB_IMAGE"
    else
      BUILDAH_REGISTRY=${WEB_IMAGE%%/*}
      if [[ "$BUILDAH_REGISTRY" != *.* && "$BUILDAH_REGISTRY" != *:* && "$BUILDAH_REGISTRY" != "localhost" ]]; then
        BUILDAH_SOURCE_REF="localhost/$WEB_IMAGE"
        BUILDAH_ARCHIVE_REF="docker.io/$WEB_IMAGE"
      fi
    fi
    log "Building $WEB_IMAGE with Buildah (no registry push)"
    buildah bud --isolation=chroot -t "$WEB_IMAGE" -f "$WEB_DOCKERFILE" "$WEB_BUILD_CONTEXT"
    if [[ "$BUILDAH_SOURCE_REF" != "$BUILDAH_ARCHIVE_REF" ]]; then
      buildah tag "$BUILDAH_SOURCE_REF" "$BUILDAH_ARCHIVE_REF"
    fi
    WEB_ARCHIVE=$(mktemp --suffix=.tar)
    TEMP_FILES+=("$WEB_ARCHIVE")
    # containers/image appends to an existing docker-archive, so hand it a fresh path.
    rm -f -- "$WEB_ARCHIVE"
    buildah push "$BUILDAH_ARCHIVE_REF" "docker-archive:$WEB_ARCHIVE"
    import_image "$WEB_ARCHIVE"
  else
    die "Neither Docker nor Buildah is available; provide WEB_IMAGE_TAR or preload $WEB_IMAGE into K3s"
  fi
else
  log "Reusing Web image already present in K3s containerd"
fi
image_present "$WEB_IMAGE" || die "K3s containerd does not contain $WEB_IMAGE; check archive tags or set WEB_IMAGE correctly"

if [[ -n "$SMOKE_IMAGE_TAR" ]]; then
  log "Importing smoke training image into K3s containerd"
  import_image "$SMOKE_IMAGE_TAR"
  image_present "vla-smoke:latest" || die "Smoke archive did not provide the contract image vla-smoke:latest"
fi

log "Applying namespace, RBAC, history volume, and $PROFILE Web configuration"
kube apply -k "$ROOT_DIR/deploy/base"
WEB_EXISTED=0
if kube -n vla-platform get deployment/vla-web >/dev/null 2>&1; then
  WEB_EXISTED=1
fi

TOKEN_UPDATED=0
if [[ -n "${PLATFORM_API_TOKEN:-}" || -n "$PLATFORM_API_TOKEN_FILE" ]]; then
  TOKEN="${PLATFORM_API_TOKEN:-}"
  if [[ -n "$PLATFORM_API_TOKEN_FILE" ]]; then
    [[ -f "$PLATFORM_API_TOKEN_FILE" && -r "$PLATFORM_API_TOKEN_FILE" ]] || die "Token file is not readable: $PLATFORM_API_TOKEN_FILE"
    TOKEN=$(<"$PLATFORM_API_TOKEN_FILE")
  fi
  [[ -n "$TOKEN" ]] || die "Platform API token cannot be empty"
  [[ "$TOKEN" != *$'\n'* && "$TOKEN" != *$'\r'* ]] || die "Platform API token must be a single line without CR/LF"
  TOKEN_SOURCE=$(mktemp)
  TEMP_FILES+=("$TOKEN_SOURCE")
  chmod 0600 "$TOKEN_SOURCE"
  printf '%s' "$TOKEN" > "$TOKEN_SOURCE"
  if (( WEB_EXISTED == 1 )); then
    printf 'RISK: updating vla-web-auth and restarting Web will rotate the API bearer token.\n'
  fi
  kube -n vla-platform create secret generic vla-web-auth \
    --from-file=PLATFORM_API_TOKEN="$TOKEN_SOURCE" \
    --dry-run=client -o yaml | kube -n vla-platform apply -f -
  TOKEN_UPDATED=1
elif ! kube -n vla-platform get secret/vla-web-auth >/dev/null 2>&1; then
  die "Missing API token. Set PLATFORM_API_TOKEN or PLATFORM_API_TOKEN_FILE for the first deployment"
else
  printf 'Reusing existing vla-platform/vla-web-auth Secret; token unchanged.\n'
fi

kube apply -k "$ROOT_DIR/deploy/web/overlays/$PROFILE"
if [[ "$WEB_IMAGE" != "vla-platform:local" ]]; then
  kube -n vla-platform set image deployment/vla-web web="$WEB_IMAGE"
fi
if (( TOKEN_UPDATED == 1 && WEB_EXISTED == 1 )); then
  kube -n vla-platform rollout restart deployment/vla-web
fi
kube -n vla-platform rollout status deployment/vla-web --timeout="$WAIT_TIMEOUT" || die "Web Deployment did not become ready; inspect pod logs and events"
kube -n vla-platform get deployment/vla-web service/vla-web persistentvolumeclaim/vla-web-history

printf '\nWeb deployment complete. NodePort: http://<host-ip>:%s\n' "${WEB_NODE_PORT:-30080}"
printf 'History SQLite path: /data/history/platform.db (PersistentVolumeClaim vla-web-history).\n'
