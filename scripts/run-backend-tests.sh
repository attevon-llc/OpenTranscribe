#!/bin/bash
# Run the backend suite ONCE and keep everything it produced.
#
# WHY
#
# The suite takes ~4 minutes and pins the CPU. Re-running it to read a tally that
# scrolled past, or to find out which tests failed, is pure waste — the run
# already contained that information. This captures the full log AND a JUnit XML
# on the first invocation, prints the summary, and then answers every follow-up
# question from those artifacts.
#
# The re-report modes are the point: after a run, `--summary` and `--failures`
# answer from disk in ~50 ms, with no pytest involved. If you want to know what
# failed, or how many passed, ask THOSE — do not run the suite again.
#
# `-p no:warnings` is not cosmetic: this repo emits enough Pydantic deprecation
# warnings to push pytest's own summary line out of a `tail`, which is exactly how
# the tally got lost and the suite got re-run.
#
# Usage:
#   ./scripts/run-backend-tests.sh                  # full suite (no e2e)
#   ./scripts/run-backend-tests.sh tests/unit       # a subset
#   ./scripts/run-backend-tests.sh --gated          # + the RUN_*-gated suites
#   ./scripts/run-backend-tests.sh --summary        # re-report, no re-run
#   ./scripts/run-backend-tests.sh --failures       # list failures, no re-run
#   ./scripts/run-backend-tests.sh --log            # path to the full log

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT/backend" || exit 2

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

OUT_DIR="${OT_TEST_OUT_DIR:-/tmp/ot-backend-tests}"
LOG="$OUT_DIR/last.log"
XML="$OUT_DIR/last.xml"
# Each run writes to its own PID-scoped files and only publishes to last.* when
# it finishes. Without this, starting a short run while a long one is in flight
# has the short one overwrite last.* and then the reporting modes describe the
# WRONG run — which happened: a full-suite run was still going when a 16-test run
# clobbered its artifacts, so `--summary` reported 16 tests.
RUN_LOG="$OUT_DIR/run-$$.log"
RUN_XML="$OUT_DIR/run-$$.xml"
mkdir -p "$OUT_DIR"

PY="$REPO_ROOT/backend/venv/bin/pytest"
[[ -x "$PY" ]] || { echo -e "${RED}missing $PY${NC}" >&2; exit 2; }

# ── Reporting from the saved artifacts (no pytest) ─────────────────────────

report_summary() {
    [[ -f "$XML" ]] || { echo -e "${YELLOW}no previous run at $XML${NC}" >&2; return 1; }
    python3 - "$XML" <<'PY'
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
suites = root.findall(".//testsuite") or [root]
tot = fail = err = skip = 0
for s in suites:
    tot  += int(s.get("tests", 0))
    fail += int(s.get("failures", 0))
    err  += int(s.get("errors", 0))
    skip += int(s.get("skipped", 0))
passed = tot - fail - err - skip
time = sum(float(s.get("time", 0)) for s in suites)
status = "PASS" if (fail + err) == 0 else "FAIL"
print(f"{status}  {passed} passed, {fail} failed, {err} errors, {skip} skipped "
      f"({tot} total, {time:.1f}s)")
PY
}

report_failures() {
    [[ -f "$XML" ]] || { echo -e "${YELLOW}no previous run at $XML${NC}" >&2; return 1; }
    python3 - "$XML" <<'PY'
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
bad = []
for case in root.iter("testcase"):
    for kind in ("failure", "error"):
        node = case.find(kind)
        if node is not None:
            name = f"{case.get('classname','')}::{case.get('name','')}".lstrip(":")
            msg = (node.get("message") or "").strip().splitlines()
            bad.append((kind, name, msg[0][:180] if msg else ""))
if not bad:
    print("no failures in the last run")
else:
    print(f"{len(bad)} failing test(s):\n")
    for kind, name, msg in bad:
        print(f"  [{kind}] {name}")
        if msg:
            print(f"      {msg}")
PY
}

case "${1:-}" in
    --summary)  report_summary; exit $? ;;
    --failures) report_failures; exit $? ;;
    --log)      echo "$LOG"; exit 0 ;;
    -h|--help)  sed -n '2,28p' "$0" | sed 's/^# \?//'; exit 0 ;;
esac

# ── Run ────────────────────────────────────────────────────────────────────

GATED=false
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --gated) GATED=true ;;
        *) ARGS+=("$arg") ;;
    esac
done
[[ ${#ARGS[@]} -gt 0 ]] || ARGS=(tests/ --ignore=tests/e2e)

echo -e "${BLUE}Running the backend suite once; artifacts in $OUT_DIR${NC}" >&2

env_prefix=()
if $GATED; then
    # Same set scripts/run-integration-tests.sh enables.
    env_prefix=(env
        RUN_PKI_TESTS=true RUN_MFA_TESTS=true RUN_LLM_TESTS=true
        RUN_FEDRAMP_TESTS=true RUN_FIPS_TESTS=true
        RUN_AUTH_CONFIG_TESTS=true RUN_ADVANCED_ADMIN_TESTS=true)
    echo -e "${BLUE}  (RUN_*-gated security suites enabled)${NC}" >&2
fi

"${env_prefix[@]}" "$PY" "${ARGS[@]}" \
    -p no:warnings \
    --junitxml="$RUN_XML" \
    2>&1 | tee "$RUN_LOG"
rc=${PIPESTATUS[0]}

# Publish atomically, only now that this run is complete.
mv -f "$RUN_LOG" "$LOG"
[[ -f "$RUN_XML" ]] && mv -f "$RUN_XML" "$XML"

echo
report_summary
echo
if [[ $rc -ne 0 ]]; then
    report_failures
    echo
    echo -e "${RED}Full output: $LOG${NC}"
    echo -e "${YELLOW}Re-report without re-running: $0 --failures${NC}"
else
    echo -e "${GREEN}Full output: $LOG  (re-report with $0 --summary)${NC}"
fi
exit $rc
