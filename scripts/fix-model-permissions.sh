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

echo -e "${GREEN}OpenTranscribe Model Cache Permission Fixer${NC}"
echo "=============================================="
echo ""

# Read MODEL_CACHE_DIR from .env file if it exists
if [ -f "$PROJECT_ROOT/.env" ]; then
    # Source the .env file to get MODEL_CACHE_DIR
    # Filter out comments (both full-line and inline) and empty lines
    MODEL_CACHE_DIR=$(grep 'MODEL_CACHE_DIR' "$PROJECT_ROOT/.env" | grep -v '^#' | cut -d'#' -f1 | cut -d'=' -f2 | tr -d ' "' | head -1)
    export MODEL_CACHE_DIR
fi

# Use default if not set
MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-$PROJECT_ROOT/models}"

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
