#!/bin/bash
#
# scripts/run-dev-tests.sh — chained local dev-cycle test runner, one command, one report.
#
# NOT the same job as scripts/test-matrix.sh. This is the quick "does my current branch
# work" check (backend suite + e2e + frontend, current dev stack, minutes not hours).
# test-matrix.sh is the exhaustive deployment-mode REHEARSAL (dev/prod/lite/PKI/GPU-scale/
# fresh-install/upgrade, stages 1-4, up to hours) — run that before cutting a release, run
# this one constantly during normal development.
#
# THIS SCRIPT OWNS NO TEST LOGIC OF ITS OWN — same convention as scripts/test-matrix.sh.
# Every phase wraps an existing script:
#   backend  -> scripts/run-integration-tests.sh
#   e2e      -> scripts/e2e/run-e2e.sh (or run-e2e-smoke.sh for --fast)
#   frontend -> scripts/frontend-check.sh --no-claude --check-only
#
# If a phase needs new behaviour, the behaviour belongs in the wrapped script, not here.
#
# Usage:
#   scripts/run-dev-tests.sh --full           # backend gate + full e2e + frontend check
#   scripts/run-dev-tests.sh --fast           # backend gate (--e2e-smoke) + frontend check
#   scripts/run-dev-tests.sh --backend-only
#   scripts/run-dev-tests.sh --e2e-only
#   scripts/run-dev-tests.sh --frontend-only
#
# Requires: ./opentr.sh start dev (live stack up) for --full/--fast/--backend-only/--e2e-only.
#
# Exit codes (matches scripts/release.sh / scripts/test-matrix.sh):
#   0 pass · 1 gate failed · 2 misuse · 3 precondition unmet

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 1

EXIT_GATE=1
EXIT_MISUSE=2
EXIT_PRECONDITION=3

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

REPORT_DIR="$(mktemp -d /tmp/ot-run-dev-tests.XXXXXX)"
mkdir -p "$REPORT_DIR"

RUN_BACKEND=false
RUN_E2E=false
RUN_FRONTEND=false
E2E_SMOKE=false

# Whether we brought the mock-llm container up ourselves, so we know whether to tear it
# back down. This script is invoked deliberately at specific points in the dev cycle, not
# left running — so unlike the base dev stack (which stays up per repo convention), a
# mock-llm instance THIS script started should not outlive the run. But if the overlay was
# already up before we got here (someone using it for other work), leave it alone.
MOCK_LLM_STARTED_BY_US=false

teardown_mock_llm() {
    if [[ "$MOCK_LLM_STARTED_BY_US" == "true" ]]; then
        echo -e "${YELLOW}==>${NC} stopping mock-llm (this run started it)"
        docker stop opentranscribe-mock-llm >/dev/null 2>&1 || true
    fi
}
trap teardown_mock_llm EXIT

usage() {
    cat <<'EOF'
Usage:
  scripts/run-dev-tests.sh --full           backend gate + full e2e + frontend check
  scripts/run-dev-tests.sh --fast           backend gate (+e2e-smoke) + frontend check
  scripts/run-dev-tests.sh --backend-only   just scripts/run-integration-tests.sh
  scripts/run-dev-tests.sh --e2e-only       just the full e2e suite
  scripts/run-dev-tests.sh --frontend-only  just the frontend check

Report and per-phase logs are written to a fresh temp dir, printed at the end.
Requires the live dev stack up (./opentr.sh start dev) for any phase but
--frontend-only.
EOF
}

case "${1:-}" in
    --full)          RUN_BACKEND=true; RUN_E2E=true; RUN_FRONTEND=true ;;
    --fast)          RUN_BACKEND=true; RUN_E2E=false; RUN_FRONTEND=true; E2E_SMOKE=true ;;
    --backend-only)  RUN_BACKEND=true ;;
    --e2e-only)      RUN_E2E=true ;;
    --frontend-only) RUN_FRONTEND=true ;;
    -h|--help|"")    usage; exit "$EXIT_MISUSE" ;;
    *) echo -e "${RED}error:${NC} unknown option: $1" >&2; usage; exit "$EXIT_MISUSE" ;;
esac

# Preconditions — fail fast with a clear reason rather than a confusing mid-run failure.
if [[ "$RUN_BACKEND" == "true" || "$RUN_E2E" == "true" ]]; then
    if ! curl -sf http://localhost:5174/health >/dev/null 2>&1; then
        echo -e "${RED}error:${NC} dev backend not reachable at :5174 — run ./opentr.sh start dev first" >&2
        exit "$EXIT_PRECONDITION"
    fi

    # backend/tests/CLAUDE.md's mock-llm suites (test_mock_llm_fixture.py, test_llm_reasoning_*,
    # test_chat_redactor_egress_style.py, ...) need the mock-llm container reachable, not just
    # the base stack. Without --with-mock-llm here, they don't skip — they fail outright, every
    # single run, on a bare `./opentr.sh start dev`. We detect whether it's already running
    # (rather than unconditionally starting it) so the EXIT trap below only tears down a
    # container THIS run brought up — never one already in use for something else.
    if docker ps --filter "name=^opentranscribe-mock-llm$" --filter "status=running" --format '{{.Names}}' 2>/dev/null | grep -q .; then
        echo -e "${YELLOW}==>${NC} mock-llm overlay already up — leaving it (not ours to stop)"
    else
        echo -e "${YELLOW}==>${NC} ensuring mock-llm overlay is up (required by backend/e2e suites)"
        if ! "$REPO_ROOT/opentr.sh" start dev --with-mock-llm >/dev/null 2>&1; then
            echo -e "${RED}error:${NC} failed to bring up the mock-llm overlay — run ./opentr.sh start dev --with-mock-llm manually to see why" >&2
            exit "$EXIT_PRECONDITION"
        fi
        MOCK_LLM_STARTED_BY_US=true
    fi
fi
if [[ "$RUN_E2E" == "true" && "$E2E_SMOKE" == "false" ]]; then
    if ! curl -sf http://localhost:5173 >/dev/null 2>&1; then
        echo -e "${RED}error:${NC} dev frontend not reachable at :5173 — run ./opentr.sh start dev first" >&2
        exit "$EXIT_PRECONDITION"
    fi
fi

declare -a PHASE_NAMES=()
declare -a PHASE_STATUS=()
declare -a PHASE_LOGS=()
declare -a PHASE_SECONDS=()

run_phase() {
    local name="$1"; shift
    local log
    log="$REPORT_DIR/$(echo "$name" | tr ' /' '__').log"
    echo -e "${YELLOW}==>${NC} $name"
    local start end elapsed
    start=$(date +%s)
    if "$@" 2>&1 | tee "$log"; then
        local rc=${PIPESTATUS[0]}
    else
        local rc=${PIPESTATUS[0]}
    fi
    end=$(date +%s)
    elapsed=$((end - start))

    PHASE_NAMES+=("$name")
    PHASE_LOGS+=("$log")
    PHASE_SECONDS+=("$elapsed")
    if [[ "$rc" -eq 0 ]]; then
        PHASE_STATUS+=("PASS")
        echo -e "${GREEN}<==${NC} $name — PASS (${elapsed}s)"
    else
        PHASE_STATUS+=("FAIL (exit $rc)")
        echo -e "${RED}<==${NC} $name — FAIL exit $rc (${elapsed}s)"
    fi
}

if [[ "$RUN_BACKEND" == "true" ]]; then
    if [[ "$E2E_SMOKE" == "true" ]]; then
        run_phase "backend (run-integration-tests.sh --e2e-smoke --cleanup)" \
            "$REPO_ROOT/scripts/run-integration-tests.sh" --e2e-smoke --cleanup
    else
        run_phase "backend (run-integration-tests.sh --coverage --cleanup)" \
            "$REPO_ROOT/scripts/run-integration-tests.sh" --coverage --cleanup
    fi
fi

if [[ "$RUN_E2E" == "true" ]]; then
    run_phase "e2e (run-e2e.sh, full suite)" \
        "$REPO_ROOT/scripts/e2e/run-e2e.sh"
fi

if [[ "$RUN_FRONTEND" == "true" ]]; then
    run_phase "frontend (frontend-check.sh --check-only)" \
        "$REPO_ROOT/scripts/frontend-check.sh" --no-claude --check-only
fi

echo ""
echo "=============================================================="
echo " run-dev-tests report — $REPORT_DIR"
echo "=============================================================="
overall_rc=0
for i in "${!PHASE_NAMES[@]}"; do
    status="${PHASE_STATUS[$i]}"
    printf "  %-55s %s (%ss)\n" "${PHASE_NAMES[$i]}" "$status" "${PHASE_SECONDS[$i]}"
    [[ "$status" == PASS ]] || overall_rc=$EXIT_GATE
done
echo "=============================================================="
if [[ "$overall_rc" -eq 0 ]]; then
    echo -e "${GREEN}ALL PHASES PASSED${NC}"
else
    echo -e "${RED}ONE OR MORE PHASES FAILED${NC} — see logs above for the failing phase(s)"
fi
echo "Full logs: $REPORT_DIR"

exit "$overall_rc"
