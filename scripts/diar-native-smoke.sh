#!/bin/bash
#
# diar-native sidecar smoke check (issue #520).
#
# WHY THIS DOES NOT GREP THE LOGS
# -------------------------------
# The issue prescribes verifying the CUDA execution provider with:
#
#     ./opentr.sh logs diar-native | grep -F 'Successfully registered `CUDAExecutionProvider`'
#
# That check CANNOT FIRE. `diar-server` initialises no tracing subscriber — only
# `diar-cli` does (diar-native/crates/diar-cli/src/main.rs) — so the `ort` crate's
# `info!` line is never emitted and `RUST_LOG` is inert. Measured on the running
# container: the entire log is one line, `diar-server listening on 0.0.0.0:8701`.
# A check that can never fire is worse than no check: written strictly it fails
# always, written leniently it passes always.
#
# `/healthz` proves nothing either — it is literally `async fn healthz() -> "ok"`,
# with no ORT, session or provider inspection.
#
# WHAT THIS CHECKS INSTEAD
# ------------------------
# Device-memory residency: the container's own PID must appear in
# `nvidia-smi --query-compute-apps` holding non-zero memory ON THE GPU THE PROJECT
# CONFIGURED. This is strictly stronger than the log line:
#
#   * It is falsifiable. A process that fell back to CPU holds ZERO device memory
#     and appears in no compute-apps list at all.
#   * It also proves correct GPU PINNING, which the log line does not. The overlay
#     resolves `DIAR_NATIVE_GPU` -> `GPU_DEVICE_ID`, and getting that wrong parks a
#     ~4.7 GB warm ORT arena on a card reserved for something else.
#   * It needs no upstream change.
#
# Restart state is checked too: the overlay carries `restart: unless-stopped` with
# the comment "known upstream teardown crash", so a crash-loop is the real failure
# mode. diar-native calls `.error_on_failure()` at every EP construction site,
# overriding the `ort` crate's silent-CPU-fallback default, so a CUDA load failure
# crash-loops rather than quietly serving on CPU.
#
# Usage:
#   scripts/diar-native-smoke.sh              # check the running sidecar
#   scripts/diar-native-smoke.sh --json       # machine-readable verdict
#
# Exit codes: 0 pass · 1 check failed · 4 NOT MEASURED (no container, no nvidia-smi).
# 4 rather than 3 on purpose: it is the code `run_phase` in run-integration-tests.sh
# reserves for "this phase examined nothing", which is exactly what an absent sidecar
# means. Reporting it as a pass would be the failure mode this script exists to remove.

set -euo pipefail

JSON=0
[[ "${1:-}" == "--json" ]] && JSON=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
    if [[ $JSON -eq 1 ]]; then
        printf '{"status":"fail","reason":%s}\n' "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
    else
        echo "❌ $1" >&2
    fi
    exit "${2:-1}"
}

command -v nvidia-smi >/dev/null 2>&1 || fail "nvidia-smi not available — cannot verify GPU residency" 4

# shellcheck source=lib/compose-project.sh
source "$REPO_ROOT/scripts/lib/compose-project.sh"

# The container name is not fixed: compose derives it from the project name, which
# --fresh deployments change. Resolve it by compose PROJECT+SERVICE label, never a bare
# name filter — an unscoped `name=diar-native` reads whatever stack happens to be up on
# this host (e.g. the live dev one) instead of the one this check is meant to examine.
CONTAINER="$(overlay_container_name diar-native)"
[[ -n "$CONTAINER" ]] || fail "no running diar-native container in compose project $(compose_project_name) (start it with ./opentr.sh start dev --with-diar-native)" 4

read -r RESTARTING RESTART_COUNT PID < <(
    docker inspect --format '{{.State.Restarting}} {{.RestartCount}} {{.State.Pid}}' "$CONTAINER"
)

[[ "$RESTARTING" == "false" ]] || fail "$CONTAINER is restarting — a CUDA load failure crash-loops the container"
[[ "$RESTART_COUNT" == "0" ]] || fail "$CONTAINER has restarted $RESTART_COUNT time(s); expected 0"
[[ "$PID" != "0" ]] || fail "$CONTAINER has no running process"

# Which GPU the project told it to use. The overlay's own precedence is
# DIAR_NATIVE_GPU -> GPU_DEVICE_ID -> 0; mirror it exactly rather than guessing.
ENV_FILE="$REPO_ROOT/.env"
# Real dotenv parsing (issue #590) via python-dotenv, not a hand-rolled grep/cut/tr
# pipeline — see gpu-scale-smoke.sh's read_env for the exact corruption this used to
# cause (a trailing `  # comment` glued onto the value).
read_env() {
    [[ -f "$ENV_FILE" ]] || return 0
    python3 "$REPO_ROOT/scripts/lib/env_reader.py" "$ENV_FILE" "$1"
}
EXPECTED_GPU="${DIAR_NATIVE_GPU:-$(read_env DIAR_NATIVE_GPU)}"
[[ -n "$EXPECTED_GPU" ]] || EXPECTED_GPU="${GPU_DEVICE_ID:-$(read_env GPU_DEVICE_ID)}"
[[ -n "$EXPECTED_GPU" ]] || EXPECTED_GPU=0

EXPECTED_UUID="$(nvidia-smi --query-gpu=index,uuid --format=csv,noheader \
    | awk -F', *' -v idx="$EXPECTED_GPU" '$1 == idx {print $2}')"
[[ -n "$EXPECTED_UUID" ]] || fail "configured GPU index $EXPECTED_GPU does not exist on this host" 4

RESIDENCY="$(nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv,noheader \
    | awk -F', *' -v pid="$PID" '$1 == pid {print $2 "|" $3}')"

if [[ -z "$RESIDENCY" ]]; then
    fail "diar-server (pid $PID) holds NO GPU memory — the CUDA execution provider did not register, so it is serving on CPU"
fi

ACTUAL_UUID="${RESIDENCY%%|*}"
USED_MEM="${RESIDENCY##*|}"
USED_MIB="${USED_MEM%% *}"

[[ "$ACTUAL_UUID" == "$EXPECTED_UUID" ]] || fail \
    "diar-server is on GPU $ACTUAL_UUID but the project configured index $EXPECTED_GPU ($EXPECTED_UUID)"
[[ "${USED_MIB:-0}" -gt 0 ]] || fail "diar-server holds 0 MiB of device memory"

if [[ $JSON -eq 1 ]]; then
    printf '{"status":"pass","container":"%s","pid":%s,"gpu_index":%s,"used_mib":%s,"restart_count":%s}\n' \
        "$CONTAINER" "$PID" "$EXPECTED_GPU" "$USED_MIB" "$RESTART_COUNT"
else
    echo "✅ diar-native CUDA execution provider is active"
    echo "   container    : $CONTAINER (pid $PID, restarts: $RESTART_COUNT)"
    echo "   GPU          : index $EXPECTED_GPU  $EXPECTED_UUID"
    echo "   device memory: ${USED_MIB} MiB resident"
fi
