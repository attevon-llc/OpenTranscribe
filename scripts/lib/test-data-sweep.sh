#!/bin/bash
# OpenTranscribe — run scripts/cleanup-test-data.py's Tier A sweep at the END of a test run
# as well as at its start (issue #1195).
#
# A start-of-run sweep only ever clears the PREVIOUS run's leftovers, so whatever the last
# run of the day leaks stays on the stack until somebody runs a suite again. Sourcing this
# file and calling `tds_sweep_at_exit "$VENV_PY" "$PROJECT_ROOT"` installs an EXIT trap that
# runs the sweep after the run, including after a failing phase, a `set -e` abort, Ctrl-C
# and `kill` (bash runs an EXIT trap on SIGINT/SIGTERM too; SIGKILL runs nothing, and that
# is what the start-of-run sweep is for).
#
# Contract:
#   * The sweep DEGRADES, never decides: its own failure prints a warning and the script
#     still exits with the run's own status. A green run is not turned red by an unreachable
#     backend, and a red run is never turned green.
#   * OT_SKIP_TEST_DATA_SWEEP=1 skips it, the same escape hatch the start-of-run sweep honours.
#   * OT_TEST_DATA_SWEEP_AT_EXIT=parent is exported by a wrapper (run-dev-tests.sh) that
#     sweeps once itself after all of its phases; the wrapped scripts then skip their own
#     end-of-run sweep. Each child would otherwise sweep while the parent's liveness marker
#     still protects everything the whole chain created, i.e. sweep for nothing.
#   * It ends this run's liveness marker first (`testrun_end`), so the sweep's cutoff is not
#     this run's own start time. A caller that never called `testrun_begin` loses nothing.
#   * An extra handler (e.g. run-dev-tests.sh's overlay teardown) is passed by name and runs
#     after the sweep, so one EXIT trap owns both: a second `trap ... EXIT` would replace this
#     one silently.
#   * TDS_SWEEP_ENABLED=0 installs the trap (so the extra handler is always run) without
#     sweeping yet; set it to 1 once the run is about to touch the stack, so a --dry-run or a
#     failed precondition does not sweep.

# shellcheck source=scripts/testrun-registry.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/testrun-registry.sh"

TDS_PYTHON=""
TDS_ROOT=""
TDS_EXTRA_HANDLER=""
TDS_SWEEP_ENABLED="${TDS_SWEEP_ENABLED:-1}"

tds_run_sweep() {
    local when="$1"
    if [ "${OT_SKIP_TEST_DATA_SWEEP:-}" = "1" ]; then
        echo "--- Test-data sweep ($when): skipped (OT_SKIP_TEST_DATA_SWEEP=1) ---"
        return 0
    fi
    echo "--- Test-data sweep ($when, Tier A) ---"
    local sweep_rc=0
    "$TDS_PYTHON" "$TDS_ROOT/scripts/cleanup-test-data.py" --execute-unambiguous || sweep_rc=$?
    if [ "$sweep_rc" -ne 0 ]; then
        echo "WARNING: test-data sweep ($when) exited $sweep_rc; the run's own result stands." \
            "Re-run: $TDS_PYTHON scripts/cleanup-test-data.py (dry run first)" >&2
    fi
    return 0
}

_tds_on_exit() {
    local rc=$?
    trap - EXIT
    if [ "$TDS_SWEEP_ENABLED" != "1" ]; then
        :
    elif [ "${OT_TEST_DATA_SWEEP_AT_EXIT:-}" = "parent" ] && [ "${TDS_IS_PARENT:-}" != "1" ]; then
        echo "--- Test-data sweep (end): deferred to the wrapping script ---"
    else
        testrun_end
        tds_run_sweep "end"
    fi
    if [ -n "$TDS_EXTRA_HANDLER" ]; then
        "$TDS_EXTRA_HANDLER" || true
    fi
    exit "$rc"
}

# tds_sweep_at_exit <python> <repo-root> [extra-exit-handler-function]
tds_sweep_at_exit() {
    TDS_PYTHON="$1"
    TDS_ROOT="$2"
    TDS_EXTRA_HANDLER="${3:-}"
    trap _tds_on_exit EXIT
}

# tds_own_end_sweep_for_children — call in a wrapper that runs other sweeping scripts, so it
# performs the single end-of-run sweep after all of them.
tds_own_end_sweep_for_children() {
    TDS_IS_PARENT=1
    export OT_TEST_DATA_SWEEP_AT_EXIT=parent
}
