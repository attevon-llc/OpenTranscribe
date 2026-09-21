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
#   ./scripts/e2e/run-e2e.sh --fresh myname      # target an isolated `--fresh --port-offset`
#                                                 # deployment (offset read from .fresh/myname.offset)
#
# E2E_FRONTEND_URL / E2E_BACKEND_URL (or --fresh <name>) point this runner at an
# isolated stack instead of the live dev one (issue #965). Without either, it targets
# http://localhost:5173 / :5174, same as always. `--fresh <name>` also exports
# POSTGRES_PORT/OPENSEARCH_PORT/MINIO_PORT for the handful of fixtures that talk to
# those services directly rather than through the backend's HTTP API — set them by hand
# (matching your own `--port-offset`) if you point this at a fresh stack via the env vars
# instead of `--fresh <name>`, or those fixtures will silently read the main dev stack.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_PY="$PROJECT_ROOT/backend/venv/bin/python"

# --fresh <name>: derive E2E_FRONTEND_URL/E2E_BACKEND_URL from the port offset recorded
# for an isolated `--fresh --port-offset` deployment (issue #965). Parsed out of the
# argument list FIRST, before the generic ARGS handling below, since `--fresh <name>`
# is not a pytest path/marker and must never reach pytest as one.
FRESH_NAME=""
_RUN_E2E_FILTERED_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fresh)
            if [[ $# -lt 2 || -z "${2:-}" ]]; then
                echo -e "${RED}--fresh requires a deployment name, e.g. --fresh myname${NC}" >&2
                exit 1
            fi
            FRESH_NAME="$2"
            shift 2
            ;;
        *)
            _RUN_E2E_FILTERED_ARGS+=("$1")
            shift
            ;;
    esac
done
set -- "${_RUN_E2E_FILTERED_ARGS[@]}"

if [[ -n "$FRESH_NAME" ]]; then
    FRESH_OFFSET_FILE="$PROJECT_ROOT/.fresh/${FRESH_NAME}.offset"
    if [[ -f "$FRESH_OFFSET_FILE" ]]; then
        FRESH_OFFSET="$(cat "$FRESH_OFFSET_FILE")"
    else
        FRESH_OFFSET=0
        echo -e "${YELLOW}--fresh ${FRESH_NAME}: no ${FRESH_OFFSET_FILE} found (no --port-offset" \
            "recorded for this deployment) -- assuming offset 0${NC}" >&2
    fi
    if ! [[ "$FRESH_OFFSET" =~ ^[0-9]+$ ]]; then
        echo -e "${RED}--fresh ${FRESH_NAME}: ${FRESH_OFFSET_FILE} does not contain a plain" \
            "integer offset (got: '${FRESH_OFFSET}')${NC}" >&2
        exit 1
    fi
    export E2E_FRONTEND_URL="http://localhost:$((5173 + FRESH_OFFSET))"
    export E2E_BACKEND_URL="http://localhost:$((5174 + FRESH_OFFSET))"
    # POSTGRES_PORT/OPENSEARCH_PORT/MINIO_PORT: fixtures that talk to those services
    # DIRECTLY (never through the backend's HTTP API — e.g. owned_corpus.py's
    # _wait_for_chunks_indexed polling OpenSearch, or cleanup-test-data.py's Postgres
    # sweep) resolve their target from these env vars via backend/tests/conftest.py,
    # completely independent of E2E_FRONTEND_URL/E2E_BACKEND_URL above. Without them a
    # `--fresh --port-offset` run's HTTP traffic correctly hits the isolated stack while
    # every direct-connection fixture silently hits the MAIN dev stack's Postgres/
    # OpenSearch instead — a mixed-stack read that manufactured 48 spurious `owned_*`
    # fixture failures (fanning out from one shared session fixture) when this was
    # missing, even though #965's frontend/backend URL fix worked correctly. conftest.py
    # already special-cases these exact three names for exactly this scenario ("this
    # brings MinIO/OpenSearch in line" with POSTGRES_PORT's existing behaviour) — this
    # was the missing wiring on the `--fresh <name>` convenience path, not a new contract.
    export POSTGRES_PORT="$((5176 + FRESH_OFFSET))"
    export OPENSEARCH_PORT="$((5180 + FRESH_OFFSET))"
    export MINIO_PORT="$((5178 + FRESH_OFFSET))"
fi

# Same defaults conftest.py's FRONTEND_URL/BACKEND_URL constants use, so a bare
# invocation with neither --fresh nor an env var behaves exactly as before.
E2E_FRONTEND_URL="${E2E_FRONTEND_URL:-http://localhost:5173}"
E2E_BACKEND_URL="${E2E_BACKEND_URL:-http://localhost:5174}"
export E2E_FRONTEND_URL E2E_BACKEND_URL

# Pure bash URL host/port extraction (no python dependency for this early check).
# Assumes the simple `scheme://host[:port]` shape every URL this script deals with has.
url_host() {
    local rest="${1#*://}"
    local hostport="${rest%%/*}"
    echo "${hostport%%:*}"
}
url_port() {
    local rest="${1#*://}"
    local hostport="${rest%%/*}"
    if [[ "$hostport" == *:* ]]; then
        echo "${hostport##*:}"
    else
        echo "$2"
    fi
}

FRONTEND_HOST="$(url_host "$E2E_FRONTEND_URL")"
FRONTEND_PORT="$(url_port "$E2E_FRONTEND_URL" 80)"
BACKEND_HOST="$(url_host "$E2E_BACKEND_URL")"
BACKEND_PORT="$(url_port "$E2E_BACKEND_URL" 80)"

# Logged unconditionally (issue #965) — a result must always be attributable to the
# stack that produced it. The mixed-stack guard itself lives in conftest.py's
# `e2e_stack_preflight` fixture (stack_urls.mixed_stack_problem), which runs before any
# test and pytest.exit(3)s loudly on a mismatch rather than letting the run continue
# against two different stacks.
echo -e "${GREEN}E2E target stack:${NC} frontend=${E2E_FRONTEND_URL} backend=${E2E_BACKEND_URL}"

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

port_open() { (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && exec 3>&- && return 0 || return 1; }

if [ ! -x "$VENV_PY" ]; then
    echo -e "${RED}backend/venv not found — create it per CLAUDE.md first.${NC}"
    exit 1
fi
if ! port_open "$FRONTEND_HOST" "$FRONTEND_PORT" || ! port_open "$BACKEND_HOST" "$BACKEND_PORT"; then
    echo -e "${RED}Frontend (${E2E_FRONTEND_URL}) / backend (${E2E_BACKEND_URL}) not reachable.${NC}"
    echo -e "Start the stack with: ${YELLOW}./opentr.sh start dev${NC}" \
        "(or point --fresh <name> / E2E_FRONTEND_URL+E2E_BACKEND_URL at an isolated stack)"
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
        --base-url "$E2E_FRONTEND_URL" --backend-url "$E2E_BACKEND_URL" \
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
import os
from playwright.sync_api import sync_playwright
base_url = os.environ.get("E2E_FRONTEND_URL", "http://localhost:5173")
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page()
    for path in ("/", "/login"):
        try:
            page.goto(f"{base_url}{path}", timeout=60000)
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
"$VENV_PY" -m pytest --base-url "$E2E_FRONTEND_URL" --backend-url "$E2E_BACKEND_URL" \
    "${ARGS[@]}" -m "not visual and not chat" -n "$WORKERS" --dist loadfile \
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
"$VENV_PY" -m pytest --base-url "$E2E_FRONTEND_URL" --backend-url "$E2E_BACKEND_URL" \
    "${ARGS[@]}" -m chat \
    "${SKIP_REASONS[@]}" --junitxml="$E2E_ARTIFACT_DIR/e2e-chat.xml" || chat_status=$?
chat_status=$(resolve_phase "$chat_status" "Phase 2 (-m chat)")
enforce_skip_ceiling "Phase 2 (-m chat)" "$chat_status" \
    "$E2E_ARTIFACT_DIR/e2e-chat.xml" "$E2E_CHAT_SKIP_CEILING"

echo -e "${GREEN}Running E2E (visual regression, serial):${NC}"
visual_status=0
"$VENV_PY" -m pytest --base-url "$E2E_FRONTEND_URL" --backend-url "$E2E_BACKEND_URL" \
    "${ARGS[@]}" -m visual \
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
