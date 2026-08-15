#!/bin/bash
# Self-test for safe-precommit.sh (issue #434). Never touches the real .mutation/ or the
# real pre-commit lock, and never invokes the real pre-commit -- SAFE_PRECOMMIT_DRY_RUN=1
# makes the wrapper report what it WOULD run instead.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$SCRIPT_DIR/safe-precommit.sh"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

pass=0
fail=0

# case()'s exit-code assertion needs the wrapper's real exit code, but `set -e` would abort
# the whole selftest on the first expected failure -- capture it explicitly instead.
run_case() {
    local desc="$1" expect_rc="$2"; shift 2
    local rc=0 out_file="$TMP_ROOT/out.$$"
    "$@" >"$out_file" 2>&1 || rc=$?
    if [[ "$rc" -eq "$expect_rc" ]]; then
        echo "  ok   - $desc (rc=$rc)"
        pass=$((pass + 1))
    else
        echo "  FAIL - $desc (expected rc=$expect_rc, got rc=$rc)"
        sed 's/^/         | /' "$out_file"
        fail=$((fail + 1))
    fi
    rm -f "$out_file"
}

echo "safe-precommit selftest"

# --- Case: no locks held -> guards pass, dry run reports what it would do ---------------
CASE1="$TMP_ROOT/case1"
mkdir -p "$CASE1/mutation" "$CASE1/git"
OT_MUTATION_OUT_DIR="$CASE1/mutation" OT_PRECOMMIT_LOCK="$CASE1/git/lock" \
    SAFE_PRECOMMIT_DRY_RUN=1 \
    run_case "clean state: guards clear" 0 \
    "$WRAPPER" run --all-files

# --- Case: a verify lock FILE exists but is not actually held -> must still pass --------
# (verify_survivor's lock files are gitignored and linger empty after every clean run;
# existence alone must not be read as "in progress", or every commit after any mutation
# run ever refuses forever.)
CASE2="$TMP_ROOT/case2"
mkdir -p "$CASE2/mutation" "$CASE2/git"
: > "$CASE2/mutation/.verify-lockout.lock"
OT_MUTATION_OUT_DIR="$CASE2/mutation" OT_PRECOMMIT_LOCK="$CASE2/git/lock" \
    SAFE_PRECOMMIT_DRY_RUN=1 \
    run_case "stale unheld verify lock file: guards clear" 0 \
    "$WRAPPER" run --all-files

# --- Case: a verify lock IS held by another process -> refuse ---------------------------
CASE3="$TMP_ROOT/case3"
mkdir -p "$CASE3/mutation" "$CASE3/git"
LOCKFILE="$CASE3/mutation/.verify-lockout.lock"
: > "$LOCKFILE"
(
    exec {holder_fd}>"$LOCKFILE"
    flock "$holder_fd"
    sleep 5
) &
holder_pid=$!
# Give the background holder time to actually acquire the flock before racing it.
for _ in $(seq 1 50); do
    if ! flock -n -w 0 "$LOCKFILE" true 2>/dev/null; then
        break
    fi
    sleep 0.1
done
OT_MUTATION_OUT_DIR="$CASE3/mutation" OT_PRECOMMIT_LOCK="$CASE3/git/lock" \
    SAFE_PRECOMMIT_DRY_RUN=1 \
    run_case "verify lock actively held: refuses" 3 \
    "$WRAPPER" run --all-files
kill "$holder_pid" 2>/dev/null || true
wait "$holder_pid" 2>/dev/null || true

# --- Case: precommit lock already held by "another instance" -> refuse -----------------
CASE4="$TMP_ROOT/case4"
mkdir -p "$CASE4/mutation" "$CASE4/git"
PLOCK="$CASE4/git/lock"
: > "$PLOCK"
(
    exec {holder_fd}>"$PLOCK"
    flock "$holder_fd"
    sleep 5
) &
holder_pid=$!
for _ in $(seq 1 50); do
    if ! flock -n -w 0 "$PLOCK" true 2>/dev/null; then
        break
    fi
    sleep 0.1
done
OT_MUTATION_OUT_DIR="$CASE4/mutation" OT_PRECOMMIT_LOCK="$PLOCK" \
    SAFE_PRECOMMIT_DRY_RUN=1 \
    run_case "concurrent safe-precommit: refuses" 3 \
    "$WRAPPER" run --all-files
kill "$holder_pid" 2>/dev/null || true
wait "$holder_pid" 2>/dev/null || true

# --- Case: no arguments -> usage error, not a silent no-op ------------------------------
CASE5="$TMP_ROOT/case5"
mkdir -p "$CASE5/mutation" "$CASE5/git"
OT_MUTATION_OUT_DIR="$CASE5/mutation" OT_PRECOMMIT_LOCK="$CASE5/git/lock" \
    run_case "no arguments: usage error" 2 \
    "$WRAPPER"

echo
echo "$pass passed, $fail failed"
if [[ "$fail" -gt 0 ]]; then
    exit 1
fi
