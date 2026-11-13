#!/bin/bash
# Evidence runs (the gate, the release test stage) must run EVERYTHING. Sourced by
# run-integration-tests.sh and run-dev-tests.sh. This file is the one place the selection flags
# are named; backend/tests/unit/test_release_path_never_selects.py scans every other
# release-path script for them.

# -p no:<name> deactivates a plugin by name (pytest docs, "Deactivating / unregistering a
# plugin by name"); it is accepted whether or not the plugin is installed.
NO_SELECT=(-p no:testmon -p no:stepwise)

# refuse_selection_flags "$@": exit 2 when the arguments or PYTEST_ADDOPTS (prepended to every
# pytest command line, pytest "Configuration" reference) ask for change- or outcome-based selection.
refuse_selection_flags() {
    local re='(^|[[:space:]=])(--testmon|--testmon-forceselect|--lf|--last-failed|--ff|--failed-first|--sw|--stepwise|--picked)([[:space:]=]|$)'
    local hay=" ${PYTEST_ADDOPTS:-} $* "
    if [[ "$hay" =~ $re ]]; then
        echo "REFUSED: test selection (${BASH_REMATCH[2]}) is for the dev loop only; this script is" \
            "evidence and runs everything. Unset PYTEST_ADDOPTS / drop the flag." >&2
        exit 2
    fi
}

# Prints NO_SELECT one word per line (for callers that cannot read the array directly).
no_select_args() { printf '%s\n' "${NO_SELECT[@]}"; }
