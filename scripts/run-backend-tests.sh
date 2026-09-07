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
#   ./scripts/run-backend-tests.sh --summary        # re-report, no re-run
#   ./scripts/run-backend-tests.sh --require-fresh --summary
#                                                   # ...but refuse if the saved run is from a
#                                                   # different commit or older than
#                                                   # OT_SUMMARY_MAX_AGE_S (default 7200)
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
# Provenance of the published artifacts: which commit was checked out, and when. Without it
# `--summary` will happily re-report a run from any date against any tree — and did:
# scripts/test-matrix.sh's leg 1.2 is `--summary`, i.e. it runs NO tests, so on 2026-09-06 the
# matrix's "backend test summary" leg passed by reading a 986-byte, two-day-old artifact from
# a different commit. A gate whose evidence has no expiry is not a gate.
META="$OUT_DIR/last.meta"
# Each run writes to its own PID-scoped files and only publishes to last.* when
# it finishes. Without this, starting a short run while a long one is in flight
# has the short one overwrite last.* and then the reporting modes describe the
# WRONG run — which happened: a full-suite run was still going when a 16-test run
# clobbered its artifacts, so `--summary` reported 16 tests.
RUN_LOG="$OUT_DIR/run-$$.log"
RUN_XML="$OUT_DIR/run-$$.xml"
mkdir -p "$OUT_DIR"

# ── Reporting from the saved artifacts (no pytest) ─────────────────────────

# Refuse to re-report an artifact that does not describe the tree in front of us.
#
# Two independent conditions, either of which makes the summary a statement about something
# else: a different commit, or one older than OT_SUMMARY_MAX_AGE_S (uncommitted work moves the
# tree without moving the SHA, so age is the only thing that catches that). Missing metadata is
# a refusal too, not a pass — an artifact from before this check existed is exactly the case it
# was added for.
assert_summary_is_fresh() {
    local want_sha="$1"
    local max_age="${OT_SUMMARY_MAX_AGE_S:-7200}"
    if [[ ! -f "$META" ]]; then
        echo -e "${RED}NOT MEASURED: $XML has no $META beside it, so there is no evidence it" >&2
        echo -e "  describes this tree. Run the suite: $0${NC}" >&2
        return 1
    fi
    local ran_sha ran_epoch age
    # shellcheck disable=SC1090
    ran_sha=$(sed -n 's/^sha=//p' "$META")
    ran_epoch=$(sed -n 's/^epoch=//p' "$META")
    age=$(( $(date +%s) - ${ran_epoch:-0} ))
    if [[ "$ran_sha" != "$want_sha" ]]; then
        echo -e "${RED}NOT MEASURED: the saved run is from commit ${ran_sha:-<unknown>}, the tree" >&2
        echo -e "  is at $want_sha. Re-reporting it would describe different code. Run: $0${NC}" >&2
        return 1
    fi
    if (( age > max_age )); then
        echo -e "${RED}NOT MEASURED: the saved run is ${age}s old (limit ${max_age}s, set" >&2
        echo -e "  OT_SUMMARY_MAX_AGE_S to change). Uncommitted work moves the tree without" >&2
        echo -e "  moving the SHA, so age is the only guard against that. Run: $0${NC}" >&2
        return 1
    fi
    echo -e "${GREEN}artifact is from ${ran_sha:0:12}, ${age}s ago${NC}" >&2
    return 0
}

report_summary() {
    [[ -f "$XML" ]] || { echo -e "${YELLOW}no previous run at $XML${NC}" >&2; return 1; }
    if [[ -n "${REQUIRE_FRESH_SHA:-}" ]]; then
        assert_summary_is_fresh "$REQUIRE_FRESH_SHA" || return 1
    fi
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
# ZERO COLLECTED IS NOT A PASS. `(fail + err) == 0` is trivially true when the
# selection matched nothing, so this printed "PASS  0 passed" for a path typo —
# and `--summary` then exited 0 over it. That is the failure this whole suite's
# auditors exist to catch (issue #431: a gate that selects nothing is
# indistinguishable from a clean run), so the runner must not commit it itself.
# pytest's own exit 5 says the same thing, but only on the run path; the saved
# artifact has to carry the verdict too, because re-reporting is the point.
if tot == 0:
    print("EMPTY  0 tests collected — the selection matched nothing (check the paths; "
          "they are relative to backend/)")
    sys.exit(1)

status = "PASS" if (fail + err) == 0 else "FAIL"
print(f"{status}  {passed} passed, {fail} failed, {err} errors, {skip} skipped "
      f"({tot} total, {time:.1f}s)")
# Exit non-zero when the RECORDED run failed, so `--summary && something` cannot
# report success over a run with failures in it. Printing "FAIL" and exiting 0 is
# the same defect this suite keeps finding in the product: a green signal that
# means nothing. The run path below ignores this status, because there `rc` is
# pytest's own and is the more direct answer.
sys.exit(0 if (fail + err) == 0 else 1)
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

# `--require-fresh [<sha>]` before `--summary`: assert the saved artifact describes this tree.
# Defaults to HEAD when no sha is given.
if [[ "${1:-}" == "--require-fresh" ]]; then
    shift
    if [[ -n "${1:-}" && "${1:-}" != --* ]]; then
        REQUIRE_FRESH_SHA="$1"; shift
    else
        REQUIRE_FRESH_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null)"
        [[ -n "$REQUIRE_FRESH_SHA" ]] || {
            echo -e "${RED}--require-fresh: not a git checkout and no sha given${NC}" >&2; exit 2; }
    fi
fi

case "${1:-}" in
    --summary)  report_summary; exit $? ;;
    --failures) report_failures; exit $? ;;
    --log)      echo "$LOG"; exit 0 ;;
    -h|--help)  sed -n '2,28p' "$0" | sed 's/^# \?//'; exit 0 ;;
esac

# ── Interpreter resolution (only needed to RUN; the modes above do not) ────

# A git worktree cannot use $REPO_ROOT/backend/venv: docker-compose.override.yml
# declares an anonymous volume at /app/venv and /app binds ./backend, so Docker
# CREATES backend/venv on the host — empty and root-owned — in order to mount over
# it. Building a real venv there needs sudo to clear the stub and then duplicates a
# multi-gigabyte install for every worktree. OT_TEST_PYTHON lets a worktree borrow
# the main checkout's interpreter instead, which is the same environment CI and the
# gate use:
#
#   OT_TEST_PYTHON=/path/to/main/backend/venv/bin/python ./scripts/run-backend-tests.sh
#
# It must be a PYTHON, not a pytest: `python -m pytest` picks up the interpreter's
# own site-packages, whereas a pytest shim from another tree resolves its imports
# against wherever it was installed.
if [[ -n "${OT_TEST_PYTHON:-}" ]]; then
    if [[ ! -x "$OT_TEST_PYTHON" ]]; then
        echo -e "${RED}OT_TEST_PYTHON is set but not executable: $OT_TEST_PYTHON${NC}" >&2
        exit 2
    fi
    if ! "$OT_TEST_PYTHON" -c 'import pytest' 2>/dev/null; then
        echo -e "${RED}OT_TEST_PYTHON has no pytest: $OT_TEST_PYTHON${NC}" >&2
        echo -e "  Point it at a venv python that has the backend requirements installed." >&2
        exit 2
    fi
    PY_CMD=("$OT_TEST_PYTHON" -m pytest)
    echo -e "${BLUE}Using OT_TEST_PYTHON: $OT_TEST_PYTHON${NC}" >&2
else
    PY_CMD=("$REPO_ROOT/backend/venv/bin/pytest")
fi

PY="$REPO_ROOT/backend/venv/bin/pytest"
if [[ -z "${OT_TEST_PYTHON:-}" && ! -x "$PY" ]]; then
    VENV_DIR="$REPO_ROOT/backend/venv"
    echo -e "${RED}missing $PY${NC}" >&2
    echo -e "  Or borrow another checkout's interpreter without building one here:" >&2
    echo -e "    ${GREEN}OT_TEST_PYTHON=/path/to/backend/venv/bin/python $0${NC}" >&2
    # `bare mountpoint` is by far the most common cause in a worktree and the least
    # guessable: docker-compose.override.yml masks the host venv with an anonymous volume
    # at /app/venv, and because /app is a bind of ./backend, Docker has to CREATE
    # backend/venv on the host to mount over it. In a checkout that has no venv yet that
    # directory arrives empty and owned by root, so the failure looks like a corrupted
    # venv rather than one that was never created — and `python -m venv` into it fails
    # with a permission error that names neither Docker nor the override file.
    if [[ -d "$VENV_DIR" && ! -e "$VENV_DIR/bin/python" ]]; then
        owner=$(stat -c '%U' "$VENV_DIR" 2>/dev/null || echo "?")
        echo >&2
        echo -e "${YELLOW}$VENV_DIR exists but is empty (owner: $owner).${NC}" >&2
        echo -e "${YELLOW}The dev stack created it as a bare mount point, not a venv:${NC}" >&2
        echo -e "${YELLOW}  docker-compose.override.yml has an anonymous volume at /app/venv,${NC}" >&2
        echo -e "${YELLOW}  and /app is a bind of ./backend, so Docker creates ./backend/venv${NC}" >&2
        echo -e "${YELLOW}  on the host if it is absent. Nothing is installed in it.${NC}" >&2
        echo >&2
        if [[ "$owner" != "$(id -un)" ]]; then
            echo -e "  It is owned by ${RED}$owner${NC}, so removing it needs elevation:" >&2
            echo -e "    ${GREEN}sudo rmdir '$VENV_DIR'${NC}" >&2
        else
            echo -e "    ${GREEN}rmdir '$VENV_DIR'${NC}" >&2
        fi
        echo -e "  then create the real venv (the two-step install is mandatory —" >&2
        echo -e "  see backend/requirements-nodeps.txt for why):" >&2
        echo -e "    ${GREEN}cd '$REPO_ROOT/backend' && python3.11 -m venv venv${NC}" >&2
        echo -e "    ${GREEN}venv/bin/pip install -r requirements.txt${NC}" >&2
        echo -e "    ${GREEN}venv/bin/pip install --no-deps -r requirements-nodeps.txt${NC}" >&2
        echo -e "    ${GREEN}venv/bin/pip install pre-commit mypy ruff bandit${NC}" >&2
    fi
    exit 2
fi


# ── Run ────────────────────────────────────────────────────────────────────

# ⚠️ `--gated` is GONE, and re-adding it is a regression. It exported
# RUN_PKI_TESTS / RUN_MFA_TESTS / RUN_LLM_TESTS / RUN_FEDRAMP_TESTS / RUN_FIPS_TESTS /
# RUN_AUTH_CONFIG_TESTS / RUN_ADVANCED_ADMIN_TESTS — seven variables that **no test reads**.
# The module-level `skipif` gates were removed from all eight security suites (each now opens
# `# Runs by DEFAULT. This module was gated behind RUN_<X>_TESTS...`) and the flag was left
# behind, so `--gated` and a bare run selected exactly the same tests while the extra output
# line claimed otherwise. Those suites are ordinary members of `tests/` now and need no flag.
# `backend/tests/unit/test_gate_run_env_vars_are_live.py` fails if any script in this family
# sets a `RUN_*` variable that no test reads through a live expression.
ARGS=("$@")
[[ ${#ARGS[@]} -gt 0 ]] || ARGS=(tests/ --ignore=tests/e2e)

echo -e "${BLUE}Running the backend suite once; artifacts in $OUT_DIR${NC}" >&2

"${PY_CMD[@]}" "${ARGS[@]}" \
    -p no:warnings \
    --junitxml="$RUN_XML" \
    2>&1 | tee "$RUN_LOG"
rc=${PIPESTATUS[0]}

# Publish atomically, only now that this run is complete.
mv -f "$RUN_LOG" "$LOG"
[[ -f "$RUN_XML" ]] && mv -f "$RUN_XML" "$XML"
# Provenance, so `--summary --require-fresh` can tell whether this describes the current tree.
printf 'sha=%s\nepoch=%s\n' \
    "$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)" \
    "$(date +%s)" > "$META"

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
