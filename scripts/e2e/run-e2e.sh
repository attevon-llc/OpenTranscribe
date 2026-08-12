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
# Default to the whole e2e directory when no path argument was given.
# NOTE: never use "${ARGS[@]:-}" — on an empty array it expands to ONE EMPTY
# STRING argument, which pytest treats as "collect the repo root".
#
# WHOLE_TREE records whether we are scanning every e2e file or a caller-selected
# subset. It decides how "0 tests collected" is read below — see resolve_phase.
WHOLE_TREE=true
if [[ ${#ARGS[@]} -eq 0 ]]; then
    ARGS=("backend/tests/e2e/")
else
    HAS_PATH=false
    for a in "${ARGS[@]}"; do
        case "$a" in backend/tests/e2e*|tests/e2e*) HAS_PATH=true ;; esac
    done
    if $HAS_PATH; then
        WHOLE_TREE=false
    else
        ARGS=("backend/tests/e2e/" "${ARGS[@]}")
    fi
fi

# Parallelize across files by default (loadfile keeps each file's tests
# serial on one worker, preserving module-scoped fixtures and intra-file
# ordering). Visual-regression tests are screenshot comparisons and need a
# QUIET stack — they run serially in a second phase. E2E_WORKERS=0 disables
# parallelism entirely; an explicit -n or -m in the args wins.
HAS_CUSTOM=false
for a in "${ARGS[@]}"; do
    case "$a" in -n|-n*|--numprocesses*|-m) HAS_CUSTOM=true ;; esac
done
WORKERS="${E2E_WORKERS:-3}"

if $HAS_CUSTOM || [[ "$WORKERS" == "0" ]]; then
    echo -e "${GREEN}Running E2E:${NC} pytest ${ARGS[*]}"
    exec "$VENV_PY" -m pytest "${ARGS[@]}"
fi

# Warm the Vite dev server first: after frontend edits the first browser
# visit triggers on-demand module re-transforms that can stall page loads
# past test timeouts. One throwaway headless visit compiles everything.
"$VENV_PY" - <<'PYEOF' || true
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page()
    for path in ("/", "/login"):
        try:
            page.goto(f"http://localhost:5173{path}", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
    b.close()
PYEOF

# pytest exit 5 means "no tests collected", which is NOT a test failure. Both phases below
# are marker-filtered, so a caller-selected subset can legitimately contain nothing for one
# of them: run-e2e-smoke.sh passes four files that hold no `visual` test, phase 2 collected
# 0, exited 5, and the smoke script therefore ALWAYS exited non-zero. Nobody noticed because
# the pre-merge gate's --e2e-smoke calls pytest directly rather than going through here.
#
# The narrow reading matters. Only 5 is forgiven, and only for a subset:
#   * 1/2/3/4 (failures, interrupt, internal error, usage error) always propagate, so a real
#     phase-2 failure still fails the run;
#   * on the WHOLE tree, 0 collected means the MARKER selects nothing — a renamed or dropped
#     `visual` marker would silently delete the entire screenshot suite from the gate, which
#     is exactly the class of bug --strict-markers exists to prevent. That stays a failure.
resolve_phase() {
    local code=$1 phase=$2
    if [[ $code -ne 5 ]]; then
        echo "$code"
        return
    fi
    if $WHOLE_TREE; then
        echo -e "${RED}${phase}: 0 tests collected from the whole e2e tree.${NC}" >&2
        echo -e "${RED}  The marker selects nothing — treat this as a broken selector.${NC}" >&2
        echo "$code"
    else
        echo -e "${YELLOW}${phase}: no matching tests in the selected files — phase skipped.${NC}" >&2
        echo 0
    fi
}

echo -e "${GREEN}Running E2E (parallel, ${WORKERS} workers, visual excluded):${NC} pytest ${ARGS[*]}"
status=0
"$VENV_PY" -m pytest "${ARGS[@]}" -m "not visual" -n "$WORKERS" --dist loadfile || status=$?
status=$(resolve_phase "$status" "Phase 1 (-m 'not visual')")

echo -e "${GREEN}Running E2E (visual regression, serial):${NC}"
visual_status=0
"$VENV_PY" -m pytest "${ARGS[@]}" -m visual || visual_status=$?
visual_status=$(resolve_phase "$visual_status" "Phase 2 (-m visual)")

[[ $status -eq 0 && $visual_status -eq 0 ]]
