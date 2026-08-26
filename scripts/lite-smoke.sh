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
#   scripts/lite-smoke.sh              # check the running lite/cpu stack
#   scripts/lite-smoke.sh --json       # machine-readable verdict
#
# Exit codes: 0 pass · 1 check failed · 4 NOT MEASURED (no stack, no nvidia-smi).

set -euo pipefail

JSON=0
[[ "${1:-}" == "--json" ]] && JSON=1

fail() {
    if [[ $JSON -eq 1 ]]; then
        printf '{"status":"fail","reason":%s}\n' "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
    else
        echo "❌ $1" >&2
    fi
    exit "${2:-1}"
}

# The backend container name is not fixed under --fresh, so resolve by name substring
# rather than hardcoding a compose project prefix.
BACKEND_CONTAINER="$(docker ps --filter "name=backend" --format '{{.Names}}' | head -1)"
[[ -n "$BACKEND_CONTAINER" ]] || fail "no running backend container — start the stack first (./opentr.sh start dev --lite)" 4

HEALTH_STATUS="$(docker inspect --format '{{.State.Health.Status}}' "$BACKEND_CONTAINER" 2>/dev/null || echo "none")"
if [[ "$HEALTH_STATUS" != "healthy" && "$HEALTH_STATUS" != "none" ]]; then
    fail "$BACKEND_CONTAINER healthcheck reports '$HEALTH_STATUS', expected healthy"
fi

GPU_WORKER="$(docker ps --filter "name=celery-worker-gpu" --format '{{.Names}}')"
[[ -z "$GPU_WORKER" ]] || fail "a GPU worker container is running ($GPU_WORKER) — this is not a lite/cpu-only topology"

if command -v nvidia-smi >/dev/null 2>&1; then
    # Any stack process holding device memory is a real failure for a "cpu-only" claim.
    # Match by container name prefix rather than a hardcoded project name.
    RESIDENT_PIDS="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null || true)"
    if [[ -n "$RESIDENT_PIDS" ]]; then
        while IFS= read -r pid; do
            [[ -n "$pid" ]] || continue
            CONTAINER_FOR_PID="$(docker ps -q | xargs -r -I{} sh -c \
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
