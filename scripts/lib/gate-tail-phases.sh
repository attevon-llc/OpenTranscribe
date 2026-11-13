#!/bin/bash
# Sourced by run-integration-tests.sh: T4 tail phases and D4 argv recording.
# Uses that script's run_phase, VENV_PY, SCRIPT_DIR and PROJECT_ROOT.

#: T4: the stack-independent tail phases. They read no live service (parity compares installed
#: packages, the rest are static or collect-only), so with a complete stack they run beside the
#: live integration lane at nice 10 instead of after it. Phase titles are unchanged. Allow-list
#: policed by tests/unit/test_gate_t4_background_phases.py (STACK_INDEPENDENT_PHASES).
t4_stack_independent_phases() {
    run_phase "Dependency parity: venv vs container" \
        "$PROJECT_ROOT/scripts/check-dependency-parity.sh"
    run_phase "DB session lifetime (no transaction across slow work)" \
        python3 "$SCRIPT_DIR/audit-session-lifetime.py" "$PROJECT_ROOT/backend/app"


    run_phase "Collection determinism (two processes, same test ids)" \
        bash -c '
            set -uo pipefail
            a=$(mktemp) && b=$(mktemp)
            trap "rm -f $a $b" EXIT
            "'"$VENV_PY"'" -m pytest --collect-only -q -o addopts= -p no:cacheprovider -p no:testmon -p no:stepwise \
                2>/dev/null | grep "::" | sort > "$a" &
            pid_a=$!
            "'"$VENV_PY"'" -m pytest --collect-only -q -o addopts= -p no:cacheprovider -p no:testmon -p no:stepwise \
                2>/dev/null | grep "::" | sort > "$b" &
            pid_b=$!
            # Wait on both regardless of order, and do not let a crashed collector look like an
            # empty-but-equal pair — the emptiness check below is the backstop for that.
            wait "$pid_a" || true
            wait "$pid_b" || true
            if [[ ! -s $a || ! -s $b ]]; then
                # BOTH, now that they run concurrently: one collector dying leaves an empty file,
                # and two empty files diff clean. "Identical" over nothing is the silent-skip trap
                # wearing a determinism check as a hat.
                echo "collected nothing — a probe did not run (a=$(wc -l < "$a") b=$(wc -l < "$b"))" >&2
                exit 1
            fi
            if ! diff -u "$a" "$b" > /tmp/ot-collection-diff.txt; then
                echo "Test ids differ between two collections of the SAME tree." >&2
                echo "Under -n auto this makes xdist abort the whole run. First 20 lines:" >&2
                head -20 /tmp/ot-collection-diff.txt >&2
                exit 1
            fi
            echo "$(wc -l < "$a") test ids, identical across both collections"
        '

    run_phase "Mutation ratchet (last run vs baselines)" \
        "$SCRIPT_DIR/run-mutation-tests.sh" --check-baseline
}

# D4: record each pytest phase's argv beside its junit XML so the fix-loop replay helper can replay
# it for the failed node ids only (a fix-loop aid, never evidence). Values of NAME=value words
# whose name looks secret are written as NAME=@ENV and re-read from the environment at replay.
record_phase_argv() {
    local a x="" w
    for a in "$@"; do
        case "$a" in --junitxml=*) x="${a#--junitxml=}" ;; esac
    done
    [ -n "$x" ] || return 0
    : >"$x.argv"
    for a in "$@"; do
        w="$a"
        if [[ "$a" =~ ^([A-Za-z_][A-Za-z0-9_]*)=.*$ ]] && [[ "${BASH_REMATCH[1]}" =~ (PASSWORD|TOKEN|SECRET|KEY) ]]; then
            w="${BASH_REMATCH[1]}=@ENV"
        fi
        printf '%s\n' "$w" >>"$x.argv"
    done
}
