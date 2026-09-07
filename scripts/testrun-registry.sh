#!/bin/bash
# OpenTranscribe — test-run liveness registry (issue #629)
#
# A run's leftover data is distinguished from LIVE data by TIME, not by name — every
# candidate table already carries a creation timestamp, so no seed-in-name scheme is
# needed. This file provides the "when did the oldest currently-running test run
# start" primitive `scripts/cleanup-test-data.py` needs to compute a safe cutoff.
#
# Mechanism: one flock-held marker file per live run under `.testruns/`, following the
# precedent in `scripts/safe-precommit.sh` (`exec {fd}>"$LOCK"; flock -n "$fd"`). The
# kernel releases the lock on process exit — including SIGKILL, OOM-kill, or a reboot
# — so a crashed run's marker is detectable as stale with no PID-reuse hazard and no
# separate cleanup step.
#
# Usage (bash, sourced):
#   source scripts/testrun-registry.sh
#   testrun_begin              # creates .testruns/<random>.lock, holds it for the
#                               # rest of THIS PROCESS's lifetime (fd stays open)
#
# Usage (from Python, via cleanup-test-data.py):
#   scripts/cleanup-test-data.py enumerates .testruns/*.lock itself and probes each
#   with fcntl.flock(..., LOCK_EX | LOCK_NB) to tell live from stale — this file's
#   Python-facing contract is only the on-disk FORMAT below, not a function call.
#
# Marker file format: one line, "started_at=<unix epoch seconds>".

set -uo pipefail

# Resolve relative to THIS file, not the caller's cwd, so a caller in any directory
# still finds the same .testruns/ at the repo root. TESTRUN_REGISTRY_DIR overrides this
# — used only by backend/tests/unit/test_testrun_registry.py's real crash-simulation
# proof, which needs an isolated throwaway directory rather than the repo's own.
_TESTRUN_REGISTRY_DIR="${TESTRUN_REGISTRY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.testruns}"

testrun_begin() {
    mkdir -p "$_TESTRUN_REGISTRY_DIR"
    local marker
    marker="$_TESTRUN_REGISTRY_DIR/$(date +%s%N)-$$.lock"

    # Open on a fresh fd and hold the exclusive lock for the rest of this process's
    # life — released automatically on exit, including SIGKILL/OOM (the kernel, not
    # this script, does the releasing). Do NOT background this: the fd — and the
    # lock — dies with the shell that opened it.
    exec {TESTRUN_LOCK_FD}>"$marker"
    if ! flock -n "$TESTRUN_LOCK_FD"; then
        # Practically unreachable (marker path is unique per pid+nanosecond), but
        # fail loudly rather than silently proceeding unmarked.
        echo "testrun-registry: could not lock freshly created marker $marker" >&2
        return 1
    fi
    printf 'started_at=%s\n' "$(date +%s)" >&"$TESTRUN_LOCK_FD"
    export TESTRUN_LOCK_FD
    export TESTRUN_MARKER="$marker"
}
