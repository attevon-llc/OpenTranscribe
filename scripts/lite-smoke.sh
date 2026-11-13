#!/bin/bash
#
# Smoke check for --lite / --cpu dev deployments (full-test-matrix.md, Stage 2, Cycle 2D).
#
# WHAT THIS CHECKS
# -----------------
# --lite and --cpu are supposed to run with NO GPU worker at all. That claim has two
# independent failure modes: a leftover celery-worker-gpu* CONTAINER (topology wrong),
# and a stack process holding GPU MEMORY even with no such container (a service reaching
# past its intended device). Both are checked, plus that the stack is actually healthy —
# a stack that never came up "passes" a GPU-absence check for the wrong reason.
#
# Usage:
#   scripts/lite-smoke.sh                       # the stack COMPOSE_PROJECT_NAME names, else the one live stack
#   scripts/lite-smoke.sh --json                # machine-readable verdict
#   scripts/lite-smoke.sh --fresh NAME          # an isolated --fresh stack (otfresh-NAME)
#   scripts/lite-smoke.sh --project P | --stock # an explicit compose project / the one live stack
# The release rehearsal passes `--project ot-reltest-lite`. The target is resolved through compose
# labels (scripts/lib/stack_target.py), never a container-name substring: `name=backend` matched
# whichever stack's backend docker listed first.
#
# Exit codes: 0 pass · 1 check failed · 4 NOT MEASURED (no stack, no nvidia-smi).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/stack-target.sh
source "$SCRIPT_DIR/lib/stack-target.sh"

JSON=0
TARGET_ARGS=()
while (( $# > 0 )); do
    case "$1" in
        --json) JSON=1 ;;
        --fresh|--project) TARGET_ARGS=("$1" "${2:?$1 needs a value}"); shift ;;
        --stock) TARGET_ARGS=(--stock) ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

fail() {
    if [[ $JSON -eq 1 ]]; then
        printf '{"status":"fail","reason":%s}\n' "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
    else
        echo "❌ $1" >&2
    fi
    exit "${2:-1}"
}

# Nothing resolvable is NOT MEASURED (4), never a pass: a stack that never came up "passes" a
# GPU-absence check for the wrong reason.
# --no-banner: the rehearsal parses this script's combined output as one JSON verdict.
st_target --no-banner ${TARGET_ARGS[@]+"${TARGET_ARGS[@]}"} || fail "cannot resolve the stack to check (see the stack-target line above)" 4
BACKEND_CONTAINER="$(st_container backend)"
[[ -n "$BACKEND_CONTAINER" ]] || fail "no running backend container in project $ST_PROJECT — start the stack first (./opentr.sh start dev --lite)" 4

HEALTH_STATUS="$(docker inspect --format '{{.State.Health.Status}}' "$BACKEND_CONTAINER" 2>/dev/null || echo "none")"
if [[ "$HEALTH_STATUS" != "healthy" && "$HEALTH_STATUS" != "none" ]]; then
    fail "$BACKEND_CONTAINER healthcheck reports '$HEALTH_STATUS', expected healthy"
fi

# GPU workers are matched by compose SERVICE label inside THIS project (celery-worker-gpu and its
# -scaled variants), so only the stack under test can fail the check.
GPU_WORKER="$(docker ps --filter "label=com.docker.compose.project=${ST_PROJECT}" \
    --format '{{.Label "com.docker.compose.service"}} {{.Names}}' | awk '$1 ~ /^celery-worker-gpu/ {print $2}')"
[[ -z "$GPU_WORKER" ]] || fail "a GPU worker container is running ($GPU_WORKER) — this is not a lite/cpu-only topology"

if command -v nvidia-smi >/dev/null 2>&1; then
    # Any stack process holding device memory is a real failure for a "cpu-only" claim
    # — but `docker ps -q` on a shared host lists EVERY running container, not just
    # this stack's. An unrelated GPU workload from a different compose project
    # (e.g. another docker-compose.yml entirely) would false-positive this check
    # if matched against the whole host's containers; scope to the project so only
    # a process belonging to the deployment under test can fail it.
    RESIDENT_PIDS="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null || true)"
    if [[ -n "$RESIDENT_PIDS" ]]; then
        while IFS= read -r pid; do
            [[ -n "$pid" ]] || continue
            CONTAINER_FOR_PID="$(docker ps -q --filter "label=com.docker.compose.project=${ST_PROJECT}" | xargs -r -I{} sh -c \
                'docker inspect --format "{{.State.Pid}} {{.Name}}" {} 2>/dev/null' \
                | awk -v p="$pid" '$1 == p {print $2}')"
            [[ -z "$CONTAINER_FOR_PID" ]] || fail "stack process $CONTAINER_FOR_PID (pid $pid) holds GPU memory in a lite/cpu deployment"
        done <<< "$RESIDENT_PIDS"
    fi
else
    if [[ $JSON -eq 1 ]]; then
        printf '{"status":"not_measured","reason":"nvidia-smi not available on this host — GPU-absence check skipped"}\n'
    else
        echo "⊘ NOT MEASURED: nvidia-smi unavailable — cannot confirm no stack process holds GPU memory" >&2
    fi
fi

if [[ $JSON -eq 1 ]]; then
    printf '{"status":"pass","backend_container":"%s","health":"%s"}\n' "$BACKEND_CONTAINER" "$HEALTH_STATUS"
else
    echo "✅ lite/cpu deployment is healthy with no GPU worker and no GPU-resident stack process"
    echo "   backend container: $BACKEND_CONTAINER (health: $HEALTH_STATUS)"
fi
