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

# A phase that declined to be counted is neither a pass nor a failure. 4 is the same code
# scripts/run-integration-tests.sh uses for it, and the same one scripts/run-dev-tests.sh's
# run_phase already translates into a `NOT MEASURED` row (and its own exit 5). Reusing it
# means the e2e phase needs no special case there.
EXIT_NOT_MEASURED=4

# Where per-phase junit XML lands, so a skip can be attributed after the fact and
# scripts/analyze-test-timing.py has something to read. Same convention (and same
# override shape) as run-integration-tests.sh's GATE_ARTIFACT_DIR, so run-dev-tests.sh can
# point it at its own per-run report dir.
E2E_ARTIFACT_DIR="${E2E_ARTIFACT_DIR:-${REPORT_DIR:-/tmp/ot-e2e-gate}}"
mkdir -p "$E2E_ARTIFACT_DIR"

# ⚠️ `-rs` and `--junitxml` on EVERY pytest phase below, deliberately — the rule
# run-integration-tests.sh already applies to the backend gate.
#
# Measured drift across three runs of THIS suite, same tree, same day, with nothing
# recording it anywhere (phase 1 | chat | visual):
#   2026-09-06 19:58  standalone   323 passed 41 skipped | 11 passed 24 skipped | 2 passed 8 skipped
#   2026-09-06 20:50  --full       333 passed 24 skipped | 11 passed 24 skipped | 2 passed 8 skipped
#   2026-09-07 01:31  --full       314 passed 25 skipped | 29 passed  3 skipped | 4 passed 6 skipped
#   2026-09-07 11:43  --full       340 passed 23 skipped | 31 passed  2 skipped | 2 passed 6 skipped
# 21 chat tests silently stopped running between the second and third runs and the phase
# still reported PASS both times. `grep SKIPPED` on any of those logs returns nothing,
# because no phase ran with `-rs`. Both flags cost nothing at runtime.
SKIP_REASONS=(-rs)

#: Per-phase skip ceilings: over the ceiling, an exit-0 phase is NOT MEASURED, never PASS.
#: Same reasoning as run-integration-tests.sh's INTEGRATION_SKIP_CEILING/GPU_SKIP_CEILING —
#: exit 0 with mass skips is indistinguishable from a real pass.
#:
#: ⚠️ These are TODAY'S REALITY, not the target. A ceiling is a FLOOR TO DRIVE DOWN — never
#: raise one to make a phase green, and re-derive it whenever anything changes what the suite
#: owns. 25/3/6 were set before the data-ownership work removed skips, and this comment
#: already said that leaving them stale is itself a failure.
#:
#: RE-DERIVED 2026-09-07 from the junit artifacts of the run that had just completed
#: (`/tmp/ot-run-dev-tests.mYICD5/e2e-xml/*.xml`, 11:43-11:49), not from the terminal log —
#: mock-llm/keycloak overlays up, NO --with-pki, NO --with-watch, no RUN_PIPELINE_SMOKE, no
#: RUN_SEARCH_QUALITY_TESTS. Re-derive with:
#:   python3 -c "import xml.etree.ElementTree as E,sys;r=E.parse(sys.argv[1]).getroot();
#:               print(sum(int(t.get('skipped')) for t in r.iter('testsuite')))" <phase>.xml
#:
#: ⚠️ That run had TWO FAILURES IN EVERY PHASE (phase 1: two OIDC login tests; chat: two
#: test_chat tests; visual: the chat_trace light/dark pair), so these are measured
#: under-failure, not a clean-run baseline: a fixture that errors takes its dependants' skips
#: with it, in either direction. Re-derive again after the first fully green run.
#:
#: Phase 1 — 365 tests, 23 skipped, each attributed by `-rs`:
#:   10  PKI            — 7 "requires RUN_PKI_E2E=true and PKI overlay running" + 3 "PKI is
#:                        not enabled" (test_pki.py, test_auth_buttons.py::TestPKIButton)
#:    4  MFA            — one "User cannot set up MFA", the rest of the chain then skips on
#:                        "MFA not configured for test user — run setup test first"
#:    3  pipeline smoke — RUN_PIPELINE_SMOKE=1, strict opt-in (--with-pipeline-smoke)
#:    2  search corpus  — needs the self-seeded RUN_SEARCH_QUALITY_TESTS corpus
#:    2  watch sources  — stack started without --with-watch
#:    1  MFA (setup)    — "MFA not required for this user — run setup test first"
#:    1  search pager   — 'administration' matches <2 pages in this dev corpus
#: Phase 2 (chat) — 35 tests, 2 skipped: "LLM is configured — the setup CTA path does not
#:   apply" and "Settings modal could not be opened programmatically in this build". The 24
#:   this file used to record were the whole family gated off by the mock-llm container the
#:   unit suite was killing (fixed on this branch), so even 3 was already history.
#: Phase 3 (visual) — 10 tests, 6 skipped: gallery / file_detail / speakers, light+dark, each
#:   "needs an isolated, seeded stack — the shared dev stack's file/cluster counts change
#:   between runs and cannot be fully masked".
#:
#: TODO: the residue that is NOT deployment shape (PKI / watch / opt-in gates) is the
#: dev-data-dependent tail — the 3 visual pairs, the search pager, the search corpus. Those
#: are what the data-ownership work should take to 0; lower these in the same commit that
#: lands it, or this file records a target nobody moved.
E2E_SKIP_CEILING="${E2E_SKIP_CEILING:-23}"           # 23 measured with overlays up; 41 with none
E2E_CHAT_SKIP_CEILING="${E2E_CHAT_SKIP_CEILING:-2}"  # 2 with the mock LLM up; 24 = the whole family gated off
E2E_VISUAL_SKIP_CEILING="${E2E_VISUAL_SKIP_CEILING:-6}"  # 6 measured; 8 = no completed file in the dataset

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

# Signature-scoped sweep of orphaned test data (issue #629) — registers this run as a
# live testrun marker first (protecting anything IT creates from a concurrently
# starting sweep), then clears Tier A leftovers from any PREVIOUS killed run. A sweep
# failure must never block the e2e run itself — this file has no log-dir convention
# and no trap, so it is guarded the same way the pytest phases further down are
# (`|| sweep_rc=$?` + a printed warning), not with `set +e`.
source "$PROJECT_ROOT/scripts/testrun-registry.sh"
testrun_begin
if [ "${OT_SKIP_TEST_DATA_SWEEP:-}" = "1" ]; then
    echo -e "${YELLOW}Test-data sweep: skipped (OT_SKIP_TEST_DATA_SWEEP=1)${NC}"
else
    sweep_rc=0
    "$VENV_PY" "$PROJECT_ROOT/scripts/cleanup-test-data.py" --execute-unambiguous || sweep_rc=$?
    if [ "$sweep_rc" -ne 0 ]; then
        echo -e "${YELLOW}Test-data sweep exited $sweep_rc — continuing with the e2e run anyway${NC}"
    fi
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
    # Our flags go FIRST so a caller passing its own --junitxml (or -p no:cacheprovider,
    # or anything else) still wins: pytest takes the last occurrence. No ceiling is applied
    # to this path — the caller chose the selection, so its legitimate skip count is theirs
    # to know, not this script's.
    #
    # ⚠️ NOT `exec`. pytest's own exit 4 means "usage error" — and 4 is THIS script's
    # NOT MEASURED code, which run-dev-tests.sh renders as a phase that declined to be
    # counted. `exec`ing pytest handed its raw code straight to the caller, so a mistyped
    # flag on this path reported as "verified nothing" rather than as a failure: the
    # green-ish reading of a broken invocation. The three-phase path below already maps
    # pytest's codes through resolve_phase; this one could not, because exec left no process
    # to map them. pytest's other codes keep their meaning and propagate unchanged.
    custom_rc=0
    "$VENV_PY" -m pytest "${SKIP_REASONS[@]}" \
        --junitxml="$E2E_ARTIFACT_DIR/e2e-custom.xml" "${ARGS[@]}" || custom_rc=$?
    if [[ $custom_rc -eq $EXIT_NOT_MEASURED ]]; then
        echo -e "${RED}pytest exited ${custom_rc} — a USAGE ERROR, not a measurement." \
            "Reporting it as a failure so it cannot be read as NOT MEASURED.${NC}" >&2
        exit 1
    fi
    exit "$custom_rc"
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

# Skipped-test count for one phase, read from its junit XML rather than grepped out of the
# terminal output. The XML is the record that survives the run, and "could not count" must
# never read as zero — an unreadable/absent report exits non-zero here and the caller treats
# that as NOT MEASURED rather than as a clean phase.
phase_skipped() {
    "$VENV_PY" - "$1" <<'PYEOF'
import sys
import xml.etree.ElementTree as ET

try:
    root = ET.parse(sys.argv[1]).getroot()
except Exception as exc:  # absent, empty, or truncated by a crashed worker
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)
suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
print(sum(int(s.get("skipped", 0)) for s in suites))
PYEOF
}

# Records a phase as NOT MEASURED when it PASSED but skipped past its ceiling.
#
# Only for the WHOLE tree: a caller-selected subset legitimately holds a different, unknown
# number of environment-gated tests, and applying the full suite's ceiling to
# `run-e2e-smoke.sh`'s four files (or run-dev-tests.sh's single pipeline-smoke file) would
# report NOT MEASURED for a selection that measured exactly what it was asked to.
NOT_MEASURED_PHASES=()
enforce_skip_ceiling() {
    local phase=$1 rc=$2 xml=$3 ceiling=$4
    $WHOLE_TREE || return 0
    [[ $rc -eq 0 ]] || return 0   # a real failure outranks "unmeasured", as in the backend gate

    local skipped
    if ! skipped=$(phase_skipped "$xml" 2>/dev/null); then
        echo -e "${YELLOW}⊘ ${phase} NOT MEASURED — could not read $xml${NC}" >&2
        NOT_MEASURED_PHASES+=("$phase (no readable junit report)")
        return 0
    fi
    if [[ "$skipped" -gt "$ceiling" ]]; then
        echo -e "${YELLOW}⊘ ${phase} NOT MEASURED — ${skipped} test(s) skipped, ceiling is ${ceiling}${NC}" >&2
        echo -e "  Exit 0 with mass skips is indistinguishable from a real pass. Something the" >&2
        echo -e "  suite needs is unreachable, or a gate has started skipping silently." >&2
        echo -e "  Every phase runs with -rs: grep this run's output for 'SKIPPED' to see why," >&2
        echo -e "  or read $xml." >&2
        NOT_MEASURED_PHASES+=("$phase ($skipped skipped > $ceiling)")
    else
        echo -e "${GREEN}${phase}: ${skipped} skipped (ceiling ${ceiling})${NC}"
    fi
    return 0
}

echo -e "${GREEN}Running E2E (parallel, ${WORKERS} workers, visual+chat excluded):${NC} pytest ${ARGS[*]}"
status=0
"$VENV_PY" -m pytest "${ARGS[@]}" -m "not visual and not chat" -n "$WORKERS" --dist loadfile \
    "${SKIP_REASONS[@]}" --junitxml="$E2E_ARTIFACT_DIR/e2e-phase1.xml" || status=$?
status=$(resolve_phase "$status" "Phase 1 (-m 'not visual and not chat')")
enforce_skip_ceiling "Phase 1 (-m 'not visual and not chat')" "$status" \
    "$E2E_ARTIFACT_DIR/e2e-phase1.xml" "$E2E_SKIP_CEILING"

# The chat family runs serially: each test is a full RAG turn (retrieval +
# rerank + LLM stream) against the one shared stack, and measured under
# 3 parallel workers the stack occasionally pushes a turn past the 90s
# stream watchdog — failing a RANDOM chat test each run. Serial is the
# honest fix: the flake was contention, never the tests or the app.
echo -e "${GREEN}Running E2E (chat, serial):${NC}"
chat_status=0
"$VENV_PY" -m pytest "${ARGS[@]}" -m chat \
    "${SKIP_REASONS[@]}" --junitxml="$E2E_ARTIFACT_DIR/e2e-chat.xml" || chat_status=$?
chat_status=$(resolve_phase "$chat_status" "Phase 2 (-m chat)")
enforce_skip_ceiling "Phase 2 (-m chat)" "$chat_status" \
    "$E2E_ARTIFACT_DIR/e2e-chat.xml" "$E2E_CHAT_SKIP_CEILING"

echo -e "${GREEN}Running E2E (visual regression, serial):${NC}"
visual_status=0
"$VENV_PY" -m pytest "${ARGS[@]}" -m visual \
    "${SKIP_REASONS[@]}" --junitxml="$E2E_ARTIFACT_DIR/e2e-visual.xml" || visual_status=$?
visual_status=$(resolve_phase "$visual_status" "Phase 3 (-m visual)")
enforce_skip_ceiling "Phase 3 (-m visual)" "$visual_status" \
    "$E2E_ARTIFACT_DIR/e2e-visual.xml" "$E2E_VISUAL_SKIP_CEILING"

echo -e "${GREEN}junit reports:${NC} $E2E_ARTIFACT_DIR"

# A real failure outranks a not-measured phase, and collapses to 1 rather than propagating
# pytest's own code: pytest's exit 4 means "usage error", which under this script's contract
# is now NOT MEASURED — propagating it would turn a broken invocation into a green-ish
# "verified nothing" instead of a failure.
if [[ $status -ne 0 || $chat_status -ne 0 || $visual_status -ne 0 ]]; then
    exit 1
fi
if [[ ${#NOT_MEASURED_PHASES[@]} -gt 0 ]]; then
    echo -e "${YELLOW}NOT MEASURED — the e2e suite ran without failing, but did not verify" \
        "what it claims to:${NC}" >&2
    for phase in "${NOT_MEASURED_PHASES[@]}"; do echo -e "${YELLOW}  ⊘ $phase${NC}" >&2; done
    exit "$EXIT_NOT_MEASURED"
fi
exit 0
