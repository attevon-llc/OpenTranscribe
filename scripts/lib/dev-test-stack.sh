#!/usr/bin/env bash
# The stack run-dev-tests.sh tests, resolved once at entry. Sourced; needs the caller's colour
# variables, RUN_*/WITH_* phase flags, LIST_OVERLAYS/DRY_RUN/PRINT_OVERLAY_FLAGS, TARGET_ARGS and
# EXIT_PRECONDITION.
#
# st_target exports COMPOSE_PROJECT_NAME and the OBSERVED ports (scripts/lib/stack_target.py), so
# overlay start-up, teardown, the preconditions and every wrapped script (they inherit the env) act
# on one stack. `--fresh NAME` / `--project P` pick it; otherwise COMPOSE_PROJECT_NAME, the owner of
# an exported POSTGRES_PORT, or the one live project. List/dry-run modes start nothing and only warn;
# --print-overlay-flags never looks at a stack (the release stage asks BEFORE it starts one).

# shellcheck source=stack-target.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/stack-target.sh"

dev_tests_resolve_stack() {
    if $PRINT_OVERLAY_FLAGS; then
        return 0
    fi
    if ! { $RUN_BACKEND || $RUN_E2E || $WITH_GPU_DIARIZATION || $WITH_MUTATION_TESTS || $WITH_PIPELINE_SMOKE || $WITH_SUMMARY_EVAL; }; then
        return 0
    fi
    local st_rc=0
    st_target ${TARGET_ARGS[@]+"${TARGET_ARGS[@]}"} || st_rc=$?
    if [[ "$st_rc" -ne 0 ]]; then
        if $LIST_OVERLAYS || $DRY_RUN; then
            echo -e "${YELLOW}warning:${NC} no stack resolved (stack-target exit $st_rc); overlay detection below assumes none" >&2
            return 0
        fi
        echo -e "${RED}error:${NC} could not resolve the stack to test (stack-target exit $st_rc) — ./opentr.sh start dev, or pass --fresh NAME / --project P" >&2
        exit "$EXIT_PRECONDITION"
    fi
    if ! $LIST_OVERLAYS && ! $DRY_RUN; then
        echo -e "${GREEN}==>${NC} stack under test: project=$ST_PROJECT backend=:${BACKEND_PORT:-?}"
        st_require healthy schema-head code=bind || exit "$EXIT_PRECONDITION"
    fi
}
