#!/bin/bash
# OpenTranscribe breached-password list installer
#
# Builds the offline password blocklist (the 100,000 most-breached passwords, as SHA-1
# hashes only) from Have I Been Pwned "Pwned Passwords" and writes it to
# <model_cache_dir>/password-blocklist/, which the backend container reads.
#
# Usage: ./scripts/download-password-blocklist.sh [model_cache_dir]
#        ./opentranscribe.sh download-models password-blocklist
#
# Takes a few hours (1,048,576 small requests to the official k-anonymity range API,
# resumable if interrupted) and needs internet access. Re-run it any time to refresh the
# list. Air-gapped hosts: build it on a connected machine and copy the directory, or use
# the offline package, which includes it.
#
# Environment: DOWNLOADER_IMAGE overrides the backend image used to run the builder;
#              BLOCKLIST_CONCURRENCY (default 16) sets the number of parallel requests.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$SCRIPT_DIR/common.sh" ]; then
    # shellcheck source=scripts/common.sh
    . "$SCRIPT_DIR/common.sh"
fi
if ! declare -F read_env_value >/dev/null 2>&1; then
    read_env_value() {
        local key="$1" env_file="${2:-.env}"
        [ -f "$env_file" ] || { echo ""; return 0; }
        grep -E "^${key}=" "$env_file" 2>/dev/null | head -1 | cut -d= -f2- \
            | sed -E 's/[[:space:]]+#.*$//' | tr -d ' "' || true
    }
fi

MODEL_CACHE_DIR="${1:-}"
if [ -z "$MODEL_CACHE_DIR" ]; then
    MODEL_CACHE_DIR="$(read_env_value MODEL_CACHE_DIR "$REPO_ROOT/.env")"
fi
if [ -z "$MODEL_CACHE_DIR" ] && [ -f ./.env ]; then
    MODEL_CACHE_DIR="$(read_env_value MODEL_CACHE_DIR ./.env)"
fi
MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-./models}"

# Same image-resolution rule as download-models.sh: the version this deployment runs.
resolve_image() {
    local tag="${OT_IMAGE_TAG:-}"
    [ -n "$tag" ] || tag="$(read_env_value OT_IMAGE_TAG "$REPO_ROOT/.env")"
    [ -n "$tag" ] || { [ -f ./.env ] && tag="$(read_env_value OT_IMAGE_TAG ./.env)"; } || true
    echo "${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-backend:${tag:-latest}"
}
DOWNLOADER_IMAGE="${DOWNLOADER_IMAGE:-$(resolve_image)}"

mkdir -p "$MODEL_CACHE_DIR/password-blocklist"
TARGET="$(realpath "$MODEL_CACHE_DIR/password-blocklist")"

echo "[INFO] Image:  $DOWNLOADER_IMAGE"
echo "[INFO] Target: $TARGET"
echo "[INFO] Source: Have I Been Pwned Pwned Passwords range API (no licensing or attribution requirement)"

# Run as the invoking user so the files are owned by whoever runs the install. Docker creates
# the bind-mount source as root when the stack started first; fall back to root then (the
# files are world-readable and mounted read-only by the backend, so ownership does not matter).
RUN_AS="$(id -u):$(id -g)"
if [ ! -w "$TARGET" ]; then
    echo "[WARNING] $TARGET is not writable by you (created by Docker as root?); running the download as root"
    RUN_AS="0:0"
fi
docker run --rm \
    --user "$RUN_AS" \
    -e MODELS_DIR=/app/models \
    -v "$TARGET:/app/models/password-blocklist" \
    "$DOWNLOADER_IMAGE" \
    python -m app.scripts.build_password_blocklist \
        --concurrency "${BLOCKLIST_CONCURRENCY:-16}"

chmod -R a+rX "$TARGET"
echo "[SUCCESS] Breached-password list installed in $TARGET"
echo "[INFO] Restart the backend to log the new status:  ./opentranscribe.sh restart backend"
