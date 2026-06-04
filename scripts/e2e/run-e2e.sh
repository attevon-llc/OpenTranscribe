#!/bin/bash
# OpenTranscribe — E2E test runner (issue #123)
#
# Runs the Playwright browser suite against the live dev stack.
# E2E is LOCAL-ONLY by design (no GitHub Actions job) — see issue #123.
#
# Usage:
#   ./scripts/e2e/run-e2e.sh                     # full e2e suite, headless
#   ./scripts/e2e/run-e2e.sh -m upload           # one marker (upload/search/...)
#   ./scripts/e2e/run-e2e.sh --headed            # visible browser (DISPLAY=:11)
#   ./scripts/e2e/run-e2e.sh tests/e2e/test_search.py -v   # pytest passthrough

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_PY="$PROJECT_ROOT/backend/venv/bin/python"

port_open() { (exec 3<>"/dev/tcp/localhost/$1") 2>/dev/null && exec 3>&- && return 0 || return 1; }

if [ ! -x "$VENV_PY" ]; then
    echo -e "${RED}backend/venv not found — create it per CLAUDE.md first.${NC}"
    exit 1
fi
if ! port_open 5173 || ! port_open 5174; then
    echo -e "${RED}Frontend (5173) / backend (5174) not reachable.${NC}"
    echo -e "Start the stack with: ${YELLOW}./opentr.sh start dev${NC}"
    exit 1
fi

cd "$PROJECT_ROOT"
ARGS=("$@")
# Default to the whole e2e directory when no path argument was given
HAS_PATH=false
for a in "${ARGS[@]:-}"; do
    case "$a" in backend/tests/e2e*|tests/e2e*) HAS_PATH=true ;; esac
done
$HAS_PATH || ARGS=("backend/tests/e2e/" "${ARGS[@]:-}")

echo -e "${GREEN}Running E2E:${NC} pytest ${ARGS[*]}"
exec "$VENV_PY" -m pytest "${ARGS[@]}"
