#!/bin/bash
# =============================================================================
# Fix Model Cache Permissions for Non-Root User Migration
# =============================================================================
# This script fixes ownership of model cache directories for the non-root
# user implementation in OpenTranscribe backend containers.
#
# USAGE:
#   ./scripts/fix-model-permissions.sh
#
# WHAT IT DOES:
#   - Changes ownership of model cache directories to the container user
#     (UID:GID 1000:999 — appuser; see CONTAINER_UID_GID in scripts/common.sh)
#   - Ensures proper permissions (755 for directories, 644 for files)
#   - Works with both host-mounted volumes and Docker volumes
#
# REQUIREMENTS:
#   - Docker installed and running
#   - User must have permission to run Docker commands (or use sudo)
#
# =============================================================================

set -e  # Exit on error

# Container user ownership. appuser in the backend image is `useradd -u 1000` (UID pinned)
# but `groupadd -r appuser` (system group, no GID pin) — it lands at gid 999, so a chown to
# 1000:1000 sets a group that does not exist in the image (issue #580). Kept in sync with
# CONTAINER_UID_GID in scripts/common.sh; this script is standalone, so it defines its own.
CONTAINER_UID_GID="${CONTAINER_UID_GID:-1000:999}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get the script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# scripts/lib/env_reader.py is a dev/CI-only helper -- it lives in the repo checkout but
# is NOT in release-manifest.txt, so it never reaches a standalone
# `setup-opentranscribe.sh` install (issue #590/#581). Calling it there is a missing-file
# failure under this script's `set -e`, which previously meant a customized
# MODEL_CACHE_DIR silently fell through to the `$PROJECT_ROOT/models` default below and
# this script chowned the WRONG directory. scripts/common.sh IS shipped
# (release-manifest.txt) and its read_env_value() is the grep/cut equivalent already used
# by opentranscribe.sh's shipped backup/restore arm, so use that here instead.
# Conditional source + fallback definition, identical pattern to opentranscribe.sh
# (~line 29): an install predating release-manifest.txt's common.sh entry still works,
# and common.sh's definition wins when present (bash keeps the last definition).
# Safe under this file's `set -e` (no `set -u`): read_env_value ends in `|| true`, and a
# missing $env_file returns "" via its own explicit early return.
if [ -f "$SCRIPT_DIR/common.sh" ]; then
    # shellcheck source=scripts/common.sh
    . "$SCRIPT_DIR/common.sh"
fi
if ! declare -F read_env_value >/dev/null 2>&1; then
    read_env_value() {
        local key="$1" env_file="${2:-.env}"
        [ -f "$env_file" ] || { echo ""; return 0; }
        grep -E "^${key}=" "$env_file" 2>/dev/null \
            | head -1 \
            | cut -d= -f2- \
            | sed -E 's/[[:space:]]+#.*$//' \
            | tr -d ' "' \
            || true
    }
fi

echo -e "${GREEN}OpenTranscribe Model Cache Permission Fixer${NC}"
echo "=============================================="
echo ""

# Read MODEL_CACHE_DIR from .env file if it exists. An already-exported MODEL_CACHE_DIR
# wins over .env -- the `${VAR:-...}` pattern every other script in this repo uses for a
# .env read. This used to be a bare assignment that clobbered a caller's exported value
# (issue #602).
if [ -f "$PROJECT_ROOT/.env" ]; then
    # read_env_value, not env_reader.py -- this script ships to end users and
    # env_reader.py does not (see the sourcing block above).
    MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-$(read_env_value MODEL_CACHE_DIR "$PROJECT_ROOT/.env")}"
    export MODEL_CACHE_DIR
fi

# Use default if not set.
MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-$PROJECT_ROOT/models}"

# Anchor a relative value (the shipped .env.example default, `./models`) to PROJECT_ROOT
# rather than the caller's CWD. Unanchored, running this script from anywhere but the repo
# root either silently no-ops (the CWD-relative path doesn't exist there, so the
# "directory does not exist yet" branch below fires and skips) or, worse, chowns an
# unrelated directory that happens to exist at that CWD-relative path (issue #602).
case "$MODEL_CACHE_DIR" in
    /*) ;;
    *) MODEL_CACHE_DIR="$PROJECT_ROOT/$MODEL_CACHE_DIR" ;;
esac

echo -e "${YELLOW}Model cache directory: ${MODEL_CACHE_DIR}${NC}"
echo ""

# Check if model directory exists
if [ ! -d "$MODEL_CACHE_DIR" ]; then
    echo -e "${YELLOW}Warning: Model cache directory does not exist yet.${NC}"
    echo "This is normal for fresh installations. Skipping permission fix."
    echo ""
    exit 0
fi

# Function to fix permissions using Docker
fix_permissions_docker() {
    echo -e "${GREEN}Fixing permissions using Docker container...${NC}"

    if docker run --rm \
        -v "$MODEL_CACHE_DIR:/models" \
        busybox:latest \
        sh -c "chown -R $CONTAINER_UID_GID /models && find /models -type d -exec chmod 755 {} \; && find /models -type f -exec chmod 644 {} \;"; then
        echo -e "${GREEN}✓ Permissions fixed successfully!${NC}"
        return 0
    else
        echo -e "${RED}✗ Failed to fix permissions using Docker${NC}"
        return 1
    fi
}

# Function to fix permissions using sudo (fallback)
fix_permissions_sudo() {
    echo -e "${YELLOW}Attempting to fix permissions using sudo...${NC}"

    if ! command -v sudo &> /dev/null; then
        echo -e "${RED}✗ sudo not available${NC}"
        return 1
    fi

    if sudo chown -R "$CONTAINER_UID_GID" "$MODEL_CACHE_DIR" && \
       sudo find "$MODEL_CACHE_DIR" -type d -exec chmod 755 {} \; && \
       sudo find "$MODEL_CACHE_DIR" -type f -exec chmod 644 {} \;; then
        echo -e "${GREEN}✓ Permissions fixed successfully using sudo!${NC}"
        return 0
    else
        echo -e "${RED}✗ Failed to fix permissions using sudo${NC}"
        return 1
    fi
}

# Try Docker method first
if command -v docker &> /dev/null; then
    if fix_permissions_docker; then
        echo ""
        echo -e "${GREEN}Migration complete!${NC}"
        echo "Your model cache is now ready for the non-root container."
        exit 0
    fi
fi

# Fallback to sudo if Docker failed
echo ""
echo -e "${YELLOW}Docker method failed, trying sudo...${NC}"
if fix_permissions_sudo; then
    echo ""
    echo -e "${GREEN}Migration complete!${NC}"
    echo "Your model cache is now ready for the non-root container."
    exit 0
fi

# If both methods failed
echo ""
echo -e "${RED}Failed to fix permissions!${NC}"
echo ""
echo "Manual steps:"
echo "1. Run the following command:"
echo "   sudo chown -R $CONTAINER_UID_GID $MODEL_CACHE_DIR"
echo "2. Or use Docker:"
echo "   docker run --rm -v $MODEL_CACHE_DIR:/models busybox chown -R $CONTAINER_UID_GID /models"
echo ""
exit 1
