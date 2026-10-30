#!/bin/bash
# Shared logging helpers for DEV scripts: colours, print_*, die.
#
# Source it path-robustly from a script in scripts/:
#   # shellcheck source=scripts/lib/log.sh
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/log.sh"
#
# DEV/CI-only. Scripts listed in release-manifest.txt, the offline package and
# anything baked into an image must NOT source this file (they are installed
# without scripts/lib/; see backend/tests/unit/test_release_manifest.py).
#
# Output format matches what the dev scripts used before this lib existed:
# every print_* writes to stdout as "[LEVEL] message".

[[ -n "${_OT_LOG_SH_LOADED:-}" ]] && return 0
_OT_LOG_SH_LOADED=1

# These are consumed by the sourcing script, not by this file.
# shellcheck disable=SC2034
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
# shellcheck disable=SC2034
CYAN='\033[0;36m'; NC='\033[0m'

print_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }

print_header() {
    echo -e "${CYAN}========================================${NC}"
    echo -e "${CYAN}$1${NC}"
    echo -e "${CYAN}========================================${NC}"
}

# die MESSAGE [EXIT_CODE]: print an error and exit (default 1).
die() {
    print_error "$1"
    exit "${2:-1}"
}
