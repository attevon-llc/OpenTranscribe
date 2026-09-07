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
# THE PRECONDITION: WHY "IS IT ON GPU?" IS NOT THE FIRST QUESTION
# ---------------------------------------------------------------
# This script used to assert device residency without ever asking what mode the
# sidecar was CONFIGURED for, and that is wrong in both directions. A sidecar
# deliberately in `DIAR_MODE=cpu` — legitimate on a CPU-only host, under `--lite`,
# or under an explicit `DIAR_NATIVE_MODE=cpu` — was reported as "the CUDA execution
# provider did not register" about a container never asked to load one. And the
# obvious repair (skip whenever the mode is not `cuda`) would have buried the real
# 2026-09-07 defect, where the sidecar was on CPU *because
# docker-compose.diar-native-gpu.yml was never loaded on a GPU host* — a `grep -q`
# + `pipefail` SIGPIPE inversion in opentr.sh's runtime probe, see
# backend/tests/unit/test_opentr_docker_probe_sigpipe.py.
#
# So three signals decide, not one, and one of the outcomes is a real FAILURE:
#
#   DIAR_MODE=cuda                                    -> measure residency
#   not cuda, host has no nvidia runtime              -> NOT MEASURED (4)
#   not cuda, container HOLDS a device reservation    -> NOT MEASURED (4): the GPU
#                                                        overlay IS loaded and an
#                                                        operator forced CPU
#   not cuda, nvidia runtime, NO device reservation   -> FAIL (1): the GPU overlay
#                                                        was not in the chain
#
# `HostConfig.DeviceRequests` is what separates the last two: it is the only
# evidence, readable from the container itself, of whether
# docker-compose.diar-native-gpu.yml (the sole home of the sidecar's device
# reservation) made it into the compose chain. `FORCE_CPU_MODE=true` in .env and
# `OT_DIAR_NATIVE_EXPECT_CPU=1` are the two EXPLICIT opt-outs; nothing else
# downgrades the failure, because "could not check" and "deliberately CPU" must not
# be the same answer.
#
# Kept deliberately in step with the pytest port of this script,
# backend/tests/integration/test_diar_native_smoke_live.py — its
# `classify_gpu_residency_precondition` is the same table, and
# backend/tests/unit/test_diar_native_gpu_precondition.py pins it.
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

# "Examined nothing, legitimately" — a DIFFERENT outcome from "checked and failed",
# and the repo's standing rule is that the two must never share a channel
# (backend/tests/CLAUDE.md: "a NOT MEASURED phase is not a pass"; security-scan.sh's
# 1-vs-2). Exit 4 already carried that meaning here, but every path reaching it
# still emitted `"status":"fail"`, so the machine-readable half said the opposite of
# the exit code. Neither caller (test-fresh-install.sh, test-upgrade.sh) parses the
# status field — both dispatch on the exit code and embed this text verbatim — so
# the distinct value is purely additional information, not a contract change.
not_measured() {
    if [[ $JSON -eq 1 ]]; then
        printf '{"status":"not-measured","reason":%s}\n' \
            "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
    else
        echo "⊘ NOT MEASURED: $1" >&2
    fi
    exit 4
}

command -v nvidia-smi >/dev/null 2>&1 || \
    not_measured "nvidia-smi not available — cannot verify GPU residency"

# shellcheck source=lib/compose-project.sh
source "$REPO_ROOT/scripts/lib/compose-project.sh"

# The container name is not fixed: compose derives it from the project name, which
# --fresh deployments change. Resolve it by compose PROJECT+SERVICE label, never a bare
# name filter — an unscoped `name=diar-native` reads whatever stack happens to be up on
# this host (e.g. the live dev one) instead of the one this check is meant to examine.
CONTAINER="$(overlay_container_name diar-native)"
[[ -n "$CONTAINER" ]] || not_measured "no running diar-native container in compose project $(compose_project_name) (start it with ./opentr.sh start dev --with-diar-native)"

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

# ---------------------------------------------------------------- precondition
# See the header block: decide whether a residency claim is meaningful here at all,
# BEFORE measuring anything. Every reader below captures into a variable first and
# post-processes — never `<docker …> | grep -q`/`awk … exit`, whose early-exiting
# reader can SIGPIPE the producer and, under this script's `set -o pipefail`, turn a
# match into a non-match. That inversion is the very defect this precondition exists
# to detect, so reproducing it here would be its own punchline.

# DIAR_MODE as the RUNNING container was configured with — the only thing that
# reflects which compose files were actually merged. `awk` with no `exit` reads to
# EOF, so nothing can take SIGPIPE.
CONTAINER_ENV="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER")"
CONFIGURED_MODE="$(printf '%s\n' "$CONTAINER_ENV" | awk -F= '$1 == "DIAR_MODE" { print $2 }')"

if [[ "$CONFIGURED_MODE" != "cuda" ]]; then
    DESCRIBED_MODE="${CONFIGURED_MODE:-<unset>}"

    # Direct evidence of whether docker-compose.diar-native-gpu.yml was in the chain:
    # it is the ONLY file declaring the sidecar's device reservation. `docker inspect`
    # renders an absent one as the JSON literal `null`.
    DEVICE_REQUESTS="$(docker inspect --format '{{json .HostConfig.DeviceRequests}}' "$CONTAINER")"
    HAS_RESERVATION=0
    case "$DEVICE_REQUESTS" in
        ""|null|"[]") ;;
        *) HAS_RESERVATION=1 ;;
    esac

    # `--format '{{json .Runtimes}}'` rather than scanning `docker info`'s prose: no
    # pipe, no substring ambiguity. A daemon that cannot answer reads as "no nvidia",
    # which only ever SOFTENS the verdict below (fail -> not measured) — the opposite
    # default would manufacture a defect report out of a docker outage.
    DOCKER_RUNTIMES="$(docker info --format '{{json .Runtimes}}' 2>/dev/null || true)"
    HOST_NVIDIA=0
    case "$DOCKER_RUNTIMES" in
        *'"nvidia"'*) HOST_NVIDIA=1 ;;
    esac

    FORCE_CPU="$(read_env FORCE_CPU_MODE)"

    if [[ "${FORCE_CPU,,}" == "true" ]]; then
        not_measured "sidecar configured DIAR_MODE=$DESCRIBED_MODE and FORCE_CPU_MODE=true in .env — this deployment opted out of GPU entirely"
    elif [[ -n "${OT_DIAR_NATIVE_EXPECT_CPU:-}" ]]; then
        not_measured "sidecar configured DIAR_MODE=$DESCRIBED_MODE and OT_DIAR_NATIVE_EXPECT_CPU is set — operator declared this deployment deliberately CPU-only"
    elif [[ $HOST_NVIDIA -eq 0 ]]; then
        not_measured "sidecar configured DIAR_MODE=$DESCRIBED_MODE and this host's Docker daemon exposes no nvidia runtime — CPU is the correct configuration here"
    elif [[ $HAS_RESERVATION -eq 1 ]]; then
        not_measured "sidecar configured DIAR_MODE=$DESCRIBED_MODE while holding an nvidia device reservation — docker-compose.diar-native-gpu.yml IS loaded and an explicit DIAR_NATIVE_MODE override chose CPU"
    else
        fail "sidecar is configured DIAR_MODE=$DESCRIBED_MODE with NO nvidia device reservation, on a host whose Docker daemon DOES expose the nvidia runtime. docker-compose.diar-native-gpu.yml was not in the compose chain that created $CONTAINER, so every /diarize call is being served on CPU. Re-start the stack with \`./opentr.sh start dev\` and check its output says 'diar-server on GPU <n>' rather than 'diar-server on CPU (no nvidia runtime detected)'. If this deployment is deliberately CPU-only (--lite / --cpu), set OT_DIAR_NATIVE_EXPECT_CPU=1."
    fi
fi

EXPECTED_GPU="${DIAR_NATIVE_GPU:-$(read_env DIAR_NATIVE_GPU)}"
[[ -n "$EXPECTED_GPU" ]] || EXPECTED_GPU="${GPU_DEVICE_ID:-$(read_env GPU_DEVICE_ID)}"
[[ -n "$EXPECTED_GPU" ]] || EXPECTED_GPU=0

EXPECTED_UUID="$(nvidia-smi --query-gpu=index,uuid --format=csv,noheader \
    | awk -F', *' -v idx="$EXPECTED_GPU" '$1 == idx {print $2}')"
[[ -n "$EXPECTED_UUID" ]] || not_measured "configured GPU index $EXPECTED_GPU does not exist on this host"

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
