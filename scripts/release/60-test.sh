#!/bin/bash
# The existing pre-merge gates, run against the release candidate.
#
# Deliberately delegates rather than reimplementing: run-integration-tests.sh is
# the canonical gate and already knows about the RUN_*-gated suites and both FIPS
# modes. A second definition of "the tests" would drift from it.
#
# Brings up (and destroys) its own isolated --fresh stack; the dev stack is neither required nor used.
#
# Exit: 0 pass · 1 failures · 3 precondition unmet

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'

# Severities from release-criteria.yaml; outcomes from here. Bidirectional — see
# criteria-lib.sh. Exported because the consumer lives across a file boundary.
export STAGE_ID=test
# shellcheck source=scripts/release/criteria-lib.sh
source "$SCRIPT_DIR/criteria-lib.sh"

# The gate runs on its OWN isolated --fresh stack, never the dev stack or the NAS data
# (rationale + configuration: fresh-stack-lib.sh). Torn down at exit, and again before the
# next run starts, so every run seeds identically.
# shellcheck source=scripts/release/fresh-stack-lib.sh
source "$SCRIPT_DIR/fresh-stack-lib.sh"
# shellcheck source=scripts/lib/stack-target.sh
source "$REPO_ROOT/scripts/lib/stack-target.sh"
FRESH_NAME="$(fresh_stack_name "$VERSION")"
PORT_OFFSET="${RELEASE_TEST_PORT_OFFSET:-400}"

fail_precondition() {
    record dev-stack-up fail "$1" "$2"
    echo -e "${RED}$1${NC}" >&2
    if [[ "$JSON_OUT" == "true" ]]; then
        printf '{"stage":"test","version":"%s","status":"fail","criteria":[%s],"next":["%s"]}\n' \
            "$VERSION" "$(criteria_json)" "$2"
    fi
    # Exit 3 (precondition unmet). Deliberately NOT via criteria_assert_all_checked: the gate
    # genuinely never ran, and reporting that as wiring drift (2) would bury the real reason.
    exit 3
}

teardown_fresh() {
    if [[ "${RELEASE_TEST_KEEP:-0}" == "1" ]]; then
        echo "RELEASE_TEST_KEEP=1: leaving fresh deployment '$FRESH_NAME' up" >&2
        return
    fi
    fresh_stack_teardown "$FRESH_NAME"
}
trap teardown_fresh EXIT

PREFLIGHT_WHY="$(fresh_stack_preflight_before "$FRESH_NAME" 2>&1)" \
    || fail_precondition "preflight: ${PREFLIGHT_WHY}" "fix the cause above, then re-run the test stage"

# The first-run wizard e2e consumes the wizard's one-time state, so the stack is brought up with
# the wizard still pending and it is completed right after that test (issue #1366).
export FRESH_STACK_DEFER_FIRST_RUN=1

echo -e "${BLUE}Starting isolated fresh stack '${FRESH_NAME}' (port offset +${PORT_OFFSET})${NC}" >&2
if ! fresh_stack_up "$FRESH_NAME" "$PORT_OFFSET"; then
    fail_precondition "could not start the isolated fresh stack '${FRESH_NAME}'" \
        "./opentr.sh start dev --fresh ${FRESH_NAME} --port-offset ${PORT_OFFSET}"
fi
if ! (exec 3<>/dev/tcp/127.0.0.1/"$POSTGRES_PORT") 2>/dev/null; then
    fail_precondition "nothing answering on 127.0.0.1:${POSTGRES_PORT} after the fresh stack started" \
        "./opentr.sh logs postgres"
fi
# The stack the gate will test is the fresh one, at this checkout's schema head, running this
# checkout's code: a stack that came up on a stale schema or another worktree's bind mount made a
# whole stage report meaningless failures (exit 3, not 1: nothing was measured).
st_target --fresh "$FRESH_NAME" || fail_precondition "cannot resolve the fresh stack '${FRESH_NAME}'" \
    "./opentr.sh start dev --fresh ${FRESH_NAME} --port-offset ${PORT_OFFSET}"
st_require schema-head code=bind healthy || fail_precondition \
    "stack-target refused the fresh stack '${FRESH_NAME}' (see the FAIL line above)" \
    "fix the cause named above, then re-run the test stage"
PREFLIGHT_WHY="$(fresh_stack_preflight_after "$FRESH_NAME" 2>&1)" \
    || fail_precondition "preflight: ${PREFLIGHT_WHY}" "fix the cause above, then re-run the test stage"
record dev-stack-up pass

# First, on the untouched stack: run-e2e.sh excludes the `first_run` marker from every phase and
# the gates below dismiss the wizard, so this is the only place the wizard itself is exercised.
wizard_rc=0
./scripts/e2e/run-e2e-first-run.sh || wizard_rc=$?
fresh_stack_complete_first_run
if (( wizard_rc == 0 )); then
    record first-run-wizard-e2e pass
else
    record first-run-wizard-e2e fail "run-e2e-first-run.sh exited $wizard_rc" \
        "read the failing step above; re-run ./scripts/e2e/run-e2e-first-run.sh --fresh ${FRESH_NAME} on a new fresh stack"
fi

echo -e "${BLUE}Running the canonical pre-merge gate${NC}" >&2
rc=0
# --export-capability drives a REAL diar-native model export (~150s, downloads the gated
# pyannote/speaker-diarization-community-1 weights) against the live backend container.
# It is opt-in in run-integration-tests.sh (too heavy for the everyday dev loop), but a
# release must never ship on the strength of a test that has never actually run — so the
# release gate always asks for it here. 10-preflight.sh already warns when no
# HUGGINGFACE_TOKEN is configured; without one this phase skips loudly (never silently,
# never counted as a pass) rather than failing the release outright.
# ⚠️ Through run-dev-tests.sh, NOT run-integration-tests.sh directly.
#
# The gate itself is unchanged — run-dev-tests.sh delegates to the same
# run-integration-tests.sh with the same flags. What it adds is OVERLAY ORCHESTRATION, and
# that is why calling the gate directly was wrong: mock-asr and mock-llm are not running on
# a plain dev stack, so all 6 tests in test_lite_mode_mocked_providers.py skipped with
# "mock-asr and/or mock-llm containers not running". Measured 2026-09-08 on this stage: 14
# skips against the integration phase's ceiling of 7, so the phase reported NOT MEASURED and
# the release gate could not be counted AT ALL. Starting the overlays by hand here would be
# worse than delegating: `opentr.sh start dev --with-X` recreates the app services, so
# omitting any OTHER active overlay's flag silently un-configures it (see the measured
# GLADIA_API_BASE_URL case in scripts/lib/dev-test-overlays.sh). That library knows the full
# set; this stage must not keep a second copy of that knowledge.
#
# Same delegation argument as this file's header, applied one level out.
./scripts/run-dev-tests.sh --backend-only --e2e-smoke --export-capability || rc=$?

# ⚠️ run-dev-tests.sh reports NOT MEASURED as **5**, not 4 — deliberately, because 4 already
# means "operator abort" in the repo-wide release contract this stage runs under. Translate
# it, or an honest "a phase verified nothing" would be recorded as a plain failure.
if (( rc == 5 )); then
    rc=4
fi

if (( rc == 0 )); then
    record integration-gate pass
elif (( rc == 4 )); then
    # Exit 4 = at least one phase declined to be counted (a mass-skipped phase, or a check
    # with no evidence). It is neither a pass nor a failure, and recording it as either has
    # already misled a release: the 2026-09-06 run recorded `integration-gate pass` on a gate
    # whose 733-second integration phase had printed NOT MEASURED for itself. Blocking, per
    # release-criteria.yaml — a release must not ship on a gate that verified nothing.
    record integration-gate not-measured "run-integration-tests.sh exited 4 (NOT MEASURED)" \
        "read the ⊘ phases above; each names what it declined to count and why"
else
    record integration-gate fail "run-integration-tests.sh exited $rc" \
        "read the per-phase output above; re-run ./scripts/run-integration-tests.sh"
fi

# The v0.6.0 browser journeys (#1282 item A), on the SAME fresh stack. Through run-dev-tests.sh
# for the same reason as the gate above: the chat journey needs the mock-llm overlay, which the
# gate's own run stopped when it finished (a direct run-e2e-release.sh call got 400 on the
# private mock URL, 3 setup errors). run-e2e.sh reads the
# E2E_* / *_PORT variables fresh-stack-lib.sh exported, so this cannot reach the dev stack.
# Its own criterion, because it is a separate runner with its own exit code; the integration
# gate above only runs the 5-file smoke set.
journeys_rc=0
./scripts/run-dev-tests.sh --e2e-release || journeys_rc=$?
# 5 = NOT MEASURED (see above): the journeys verified nothing, so this blocking criterion fails.
if (( journeys_rc == 0 )); then
    record release-e2e-journeys pass
else
    record release-e2e-journeys fail "run-dev-tests.sh --e2e-release exited $journeys_rc" \
        "read the failing journey above; re-run ./scripts/e2e/run-e2e-release.sh --fresh ${FRESH_NAME}"
    (( rc == 0 )) && rc=1
fi

# Both halves of the contract. Reachable on both outcomes — the gate above always records
# one — so this cannot convert a test failure (1) into a wiring-misuse exit (2).
(( wizard_rc != 0 && rc == 0 )) && rc=1
criteria_assert_all_checked

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"test","version":"%s","status":"%s","criteria":[%s],"next":%s}\n' \
        "$VERSION" "$([[ $rc -eq 0 ]] && echo pass || echo fail)" "$(criteria_json)" \
        "$([[ $rc -eq 0 ]] && echo '["build"]' || echo '["fix the failing suites"]')"
fi
exit $rc
