#!/bin/bash
#
# scripts/lib/dev-test-overlays.sh — the declarative overlay table + detection/setup/teardown
# logic scripts/run-dev-tests.sh drives (issue #630). Split out to keep run-dev-tests.sh under
# this repo's ~300-line convention (root CLAUDE.md) — sourced, not a standalone entry point.
#
# Expects the caller to have already set: REPO_ROOT, VENV_PY, AUTH_CONFIG_CLI, RED/GREEN/
# YELLOW/NC, EXIT_PRECONDITION, and the mode booleans (RUN_BACKEND/RUN_E2E/ALL_OVERLAYS/
# NO_OVERLAYS/WITH_GPU_SCALE).

# ---------------------------------------------------------------- overlay table (issue #630)
#
# Model: opentr.sh's own leg-table convention (scripts/test-matrix.sh's LEGS array) — one
# declarative source of truth instead of a hardcoded per-overlay if-block. Every overlay this
# script manages gets: which compose SERVICE to detect it by (empty string for the one overlay
# that has no dedicated service — see watch_overlay_active below), which auto_config.py KEY (if
# any) needs DB reconciliation, which tier it belongs to, and a human description for reporting.
#
# TIER auto        -> started under plain --full (cheap, safe, absence produces confusing false
#                      failures rather than real ones — mock-llm's original justification,
#                      generalized to keycloak-test/ldap-test per the Opus plan's B1).
# TIER all-overlays -> only under --all-overlays (recreates app containers / needs a dedicated
#                      mocked-provider container; costlier, not needed for a bare --full).
declare -A OVERLAY_SERVICE=(
    [mock-llm]=mock-llm
    [keycloak-test]=keycloak
    [ldap-test]=lldap
    [watch]=""
    [mock-asr]=mock-asr
)
declare -A OVERLAY_AUTH_KEY=(
    [keycloak-test]=oidc_enabled
    [ldap-test]=ldap_enabled
)
declare -A OVERLAY_TIER=(
    [mock-llm]=auto
    [keycloak-test]=auto
    [ldap-test]=auto
    [watch]=all
    [mock-asr]=all
)
# Which RUN_* phase(s) gate each overlay — "either" (backend or e2e), "e2e", or "backend".
# resolve_needed_overlays() below drives entirely off this + OVERLAY_TIER, in a fixed order,
# rather than one hardcoded if-block per overlay.
declare -A OVERLAY_PHASE=(
    [mock-llm]=either
    [keycloak-test]=e2e
    [ldap-test]=e2e
    [watch]=e2e
    [mock-asr]=backend
)
#: Fixed iteration order for resolve_needed_overlays/print_overlay_plan — associative-array key
#: order is unspecified, and a stable order makes the printed plan/report reproducible.
OVERLAY_ORDER=(mock-llm keycloak-test ldap-test watch mock-asr)
declare -A OVERLAY_DESC=(
    [mock-llm]="mock LLM — chat/summarization/topic-extraction suites (backend + e2e chat)"
    [keycloak-test]="Keycloak/OIDC — test_auth_buttons.py::TestOIDCLogin, test_ldap_oidc.py (~60-90s to healthy)"
    [ldap-test]="LDAP — test_auth_buttons.py::TestLDAPLogin, test_ldap_oidc.py (fast, no healthcheck)"
    [watch]="watch-sources host-folder mount — test_watch_sources_e2e.py (recreates app containers)"
    [mock-asr]="mock cloud ASR — backend/tests/integration/test_lite_mode_mocked_providers.py"
)

# --with-* flags opentr.sh dispatches that this script deliberately does NOT manage, each with a
# written reason. backend/tests/unit/test_run_dev_tests_overlay_coverage.py enumerates opentr.sh's
# actual --with-* dispatch and fails if a flag is neither a key of OVERLAY_SERVICE above nor listed
# here — the same convention backend/tests/unit/test_opentr_fresh_aux_isolation.py uses for --fresh
# isolation, adopted so a newly-added opentr.sh overlay can't silently go unhandled here the way
# --with-llm-test once went unhandled there (see that test's docstring / root CLAUDE.md).
declare -A EXEMPT_WITH_FLAGS=(
    [authentik-test]="verified (Opus investigation behind issue #630): no e2e test in this repo actually exercises the running Authentik container — only Authentik-shaped string fixtures against pure functions"
    [backup]="binds a live host directory, declares no container_name or port; nothing this script drives needs it"
    [diar-native]="a separate GPU sidecar (Rust binary); no test selector this script drives needs it"
    [gpu-split]="a distinct GPU-worker-topology change like --gpu-scale, not a container overlay; no test selector this script drives needs it"
    [llm-test]="reserves a real GPU for 3-10 minutes and nothing in the e2e/backend suites this script drives needs it — opt-in only, never auto-started"
    [monitoring]="Prometheus/Grafana; no test selector this script drives needs it"
    [pki]="needs the prod+nginx overlay (PKI/mTLS), not the dev stack this script targets — test_pki.py is out of scope here"
    [smb-test]="no test selector this script drives needs it; test_watch_sources_e2e.py only needs the local-folder mount from --with-watch"
)

compose_project_name() {
    if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
        echo "$COMPOSE_PROJECT_NAME"
        return
    fi
    # basename "$REPO_ROOT" is wrong when this script runs from a git worktree
    # (.claude/worktrees/<name>): REPO_ROOT then resolves to the WORKTREE's own
    # directory name, not the main checkout's, and never matches the live stack's
    # actual compose project label — every overlay_container_name lookup silently
    # finds nothing. Detect the real project from a container that must already be
    # running for any of these lookups to matter (this file's callers all require
    # a live stack), falling back to the old directory-name guess only if none is up.
    local detected
    detected="$(docker ps \
        --filter "label=com.docker.compose.service=postgres" \
        --filter "status=running" \
        --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null | head -1)"
    echo "${detected:-$(basename "$REPO_ROOT")}"
}

# Generalizes the detection primitive the original mock-llm block used
# (`docker ps --filter name=^opentranscribe-mock-llm$`), which hardcoded a container name that
# is actually `${MOCK_LLM_CONTAINER_NAME:-opentranscribe-mock-llm}` in the compose file — a
# --fresh stack's differently-named container would never be found (issue #630 / B5). Filtering
# on the compose PROJECT+SERVICE labels instead is correct regardless of container_name, or
# whether the compose file sets one at all (keycloak.yml deliberately doesn't).
overlay_container_name() {
    local service="$1"
    docker ps \
        --filter "label=com.docker.compose.project=$(compose_project_name)" \
        --filter "label=com.docker.compose.service=${service}" \
        --filter "status=running" \
        --format '{{.Names}}' 2>/dev/null | head -1
}

# The watch overlay has no dedicated container — it mounts the host watch folder into existing
# app services (backend, celery-beat, celery-download-worker, celery-cpu-worker) and sets
# WATCH_FOLDER_PATH on them. Detected via the backend container's env instead.
watch_overlay_active() {
    local backend_c
    backend_c="$(overlay_container_name backend)"
    [[ -n "$backend_c" ]] || return 1
    docker exec "$backend_c" sh -c '[ -n "${WATCH_FOLDER_PATH:-}" ]' >/dev/null 2>&1
}

declare -a OVERLAYS_NEEDED=()
declare -a OVERLAYS_TO_START=()
declare -a OVERLAYS_STARTED_BY_US=()
declare -a OVERLAYS_ALREADY_UP=()
declare -A OVERLAY_CONTAINER=()
declare -A AUTH_PRIOR_VALUE=()
declare -A AUTH_KEYS_TOUCHED=()

resolve_needed_overlays() {
    $NO_OVERLAYS && return
    local flag phase tier phase_ok
    for flag in "${OVERLAY_ORDER[@]}"; do
        phase="${OVERLAY_PHASE[$flag]}"
        tier="${OVERLAY_TIER[$flag]}"
        phase_ok=false
        case "$phase" in
            either) { $RUN_BACKEND || $RUN_E2E; } && phase_ok=true ;;
            e2e)    $RUN_E2E && phase_ok=true ;;
            backend) $RUN_BACKEND && phase_ok=true ;;
        esac
        $phase_ok || continue
        # auto tier always runs when its phase is selected; all-overlays tier only under
        # --all-overlays (recreates app containers / needs a dedicated mock provider — costlier,
        # not needed for a bare --full).
        if [[ "$tier" == "all" ]] && ! $ALL_OVERLAYS; then
            continue
        fi
        OVERLAYS_NEEDED+=("$flag")
    done
}

# Read-only pass: for every needed overlay, is it already running? Populates
# OVERLAYS_ALREADY_UP / OVERLAYS_TO_START / OVERLAY_CONTAINER without starting or changing
# anything — used by both the real run and --list-overlays/--dry-run.
detect_overlay_state() {
    local flag svc container
    for flag in "${OVERLAYS_NEEDED[@]}"; do
        svc="${OVERLAY_SERVICE[$flag]}"
        if [[ -n "$svc" ]]; then
            container="$(overlay_container_name "$svc")"
        elif [[ "$flag" == "watch" ]]; then
            if watch_overlay_active; then container="(mounted on backend)"; else container=""; fi
        else
            container=""
        fi
        if [[ -n "$container" ]]; then
            OVERLAYS_ALREADY_UP+=("$flag")
            OVERLAY_CONTAINER[$flag]="$container"
        else
            OVERLAYS_TO_START+=("$flag")
        fi
    done
}

print_overlay_plan() {
    echo "=============================================================="
    echo " resolved overlay set"
    echo "=============================================================="
    if $NO_OVERLAYS; then
        echo "  --no-overlays given — assuming the stack is already configured, nothing resolved"
    elif [[ ${#OVERLAYS_NEEDED[@]} -eq 0 ]]; then
        echo "  (none needed for the phases selected)"
    else
        local flag
        for flag in "${OVERLAYS_NEEDED[@]}"; do
            local state="would start"
            for a in "${OVERLAYS_ALREADY_UP[@]}"; do [[ "$a" == "$flag" ]] && state="already up"; done
            printf "  %-16s [%-9s] %s\n" "--with-$flag" "$state" "${OVERLAY_DESC[$flag]}"
        done
    fi
    if [[ ${#OVERLAYS_TO_START[@]} -gt 0 ]]; then
        local with_flags=() f
        for f in "${OVERLAYS_TO_START[@]}"; do with_flags+=("--with-$f"); done
        echo ""
        echo "  command that would run (batched — one opentr.sh call, never one per overlay):"
        echo "    ./opentr.sh start dev ${with_flags[*]}"
    fi
    echo "=============================================================="
    echo " opentr.sh --with-* flags this script deliberately does NOT manage"
    echo "=============================================================="
    local ex
    for ex in $(printf '%s\n' "${!EXEMPT_WITH_FLAGS[@]}" | sort); do
        printf "  --with-%-15s %s\n" "$ex" "${EXEMPT_WITH_FLAGS[$ex]}"
    done
    echo "=============================================================="
}

# ---------------------------------------------------------------- --gpu-scale count detection
#
# "Project-scoped GPU count" per the repo owner's explicit requirement: NOT nvidia-smi's raw
# device count (this host has 3 physical GPUs, but only 1 is assigned to this project — GPU 0
# and 2 are reserved for unrelated work, see root CLAUDE.md). Instead: does this project's own
# .env configure GPU_SCALE_DEVICE_ID as a DIFFERENT device than GPU_DEVICE_ID? That is exactly
# docker-compose.gpu-scale.yml's own documented "Dual-GPU Mode" vs "Single-GPU Mode" distinction
# (backend/app/tasks/CLAUDE.md's gpu-scale section) — equal IDs means this project only ever
# reserves one physical card no matter how many the host has.
project_gpu_count() {
    REPO_ROOT="$REPO_ROOT" "$VENV_PY" - <<'PY'
import os
from pathlib import Path

from dotenv import dotenv_values

env = dotenv_values(Path(os.environ["REPO_ROOT"]) / ".env")


def val(key: str, default: str) -> str:
    raw = os.environ.get(key) or env.get(key) or default
    return str(raw).strip()


gpu_device = val("GPU_DEVICE_ID", "0")
scale_device = val("GPU_SCALE_DEVICE_ID", "2")
print(len({gpu_device, scale_device}))
PY
}

# ------------------------------------------------------------------ overlay setup / teardown
teardown_overlays() {
    local key flag
    # DB restore before container stop (B5) — a container gone but the DB flag still forced on
    # is the exact silent-failure-mode this run started by fixing.
    for key in "${!AUTH_KEYS_TOUCHED[@]}"; do
        echo -e "${YELLOW}==>${NC} restoring auth_config.${key} to ${AUTH_PRIOR_VALUE[$key]}"
        "$VENV_PY" "$AUTH_CONFIG_CLI" restore "$key" "${AUTH_PRIOR_VALUE[$key]}" >/dev/null 2>&1 || true
    done
    for flag in "${OVERLAYS_STARTED_BY_US[@]}"; do
        if [[ "$flag" == "watch" ]]; then
            echo -e "${YELLOW}==>${NC} leaving --with-watch mounted (no dedicated container to stop;" \
                 "recreating backend/celery to drop the mount is out of proportion to a bind mount of ./watch)"
            continue
        fi
        local c="${OVERLAY_CONTAINER[$flag]:-}"
        if [[ -n "$c" ]]; then
            echo -e "${YELLOW}==>${NC} stopping $flag (this run started it): $c"
            docker stop "$c" >/dev/null 2>&1 || true
        fi
    done
}
trap teardown_overlays EXIT

setup_overlays() {
    if $NO_OVERLAYS; then
        echo -e "${YELLOW}==>${NC} --no-overlays: assuming the stack is already configured, skipping auto-detection"
        return 0
    fi
    [[ ${#OVERLAYS_NEEDED[@]} -eq 0 ]] && return 0

    detect_overlay_state

    local flag
    for flag in "${OVERLAYS_ALREADY_UP[@]}"; do
        echo -e "${YELLOW}==>${NC} $flag overlay already up — leaving it (not ours to stop)"
    done

    if [[ ${#OVERLAYS_TO_START[@]} -gt 0 ]]; then
        local with_flags=() f
        for f in "${OVERLAYS_TO_START[@]}"; do with_flags+=("--with-$f"); done
        echo -e "${YELLOW}==>${NC} bringing up overlays in one batched call: ${with_flags[*]}"
        if ! "$REPO_ROOT/opentr.sh" start dev "${with_flags[@]}" >/dev/null 2>&1; then
            echo -e "${RED}error:${NC} failed to bring up overlays (${with_flags[*]}) — run" \
                 "'./opentr.sh start dev ${with_flags[*]}' manually to see why" >&2
            exit "$EXIT_PRECONDITION"
        fi
        for f in "${OVERLAYS_TO_START[@]}"; do
            OVERLAYS_STARTED_BY_US+=("$f")
            local svc="${OVERLAY_SERVICE[$f]}"
            if [[ -n "$svc" ]]; then
                OVERLAY_CONTAINER[$f]="$(overlay_container_name "$svc")"
            fi
        done
    fi

    # DB reconciliation (B3.1) — independent of whether the container was already up or just
    # started: a container running with the DB flag off is the exact failure mode this exists
    # to fix, and it happens regardless of who started the container.
    for flag in "${OVERLAYS_NEEDED[@]}"; do
        local key="${OVERLAY_AUTH_KEY[$flag]:-}"
        [[ -z "$key" ]] && continue
        local prior
        prior="$("$VENV_PY" "$AUTH_CONFIG_CLI" get "$key" 2>/dev/null)"
        if [[ -z "$prior" ]]; then
            echo -e "${RED}error:${NC} could not read auth_config.$key from the DB — is Postgres up?" >&2
            exit "$EXIT_PRECONDITION"
        fi
        AUTH_PRIOR_VALUE[$key]="$prior"
        if [[ "$prior" != "true" ]]; then
            echo -e "${YELLOW}==>${NC} enabling auth_config.$key (was $prior) so $flag's e2e tests don't silently skip"
            "$VENV_PY" "$AUTH_CONFIG_CLI" set "$key" true >/dev/null
            AUTH_KEYS_TOUCHED[$key]=1
        fi
    done

    # RUN_AUTH_E2E gate (B3.3) — needed by test_ldap_oidc.py and the LDAP half of
    # test_auth_buttons.py whenever either overlay is in play this run.
    for flag in "${OVERLAYS_NEEDED[@]}"; do
        if [[ "$flag" == "keycloak-test" || "$flag" == "ldap-test" ]]; then
            export RUN_AUTH_E2E=1
            break
        fi
    done
}
