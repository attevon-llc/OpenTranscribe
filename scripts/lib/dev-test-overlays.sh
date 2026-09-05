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
    [diar-native]=diar-native
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
    # `auto`, deliberately, despite being one of the two overlays that RECREATES app
    # containers (it sets GLADIA_API_BASE_URL / ASR_ALLOW_PRIVATE_ENDPOINTS on backend and
    # celery-cloud-asr-worker, so those two are recreated once at setup).
    #
    # It was `all`, which contradicted its own OVERLAY_PHASE=backend: the backend phase runs
    # tests/integration/test_lite_mode_mocked_providers.py, so a plain --full ran those 6
    # tests and every one of them SKIPPED, every time, for want of an overlay the same table
    # said the backend phase needed. The whole --lite / cloud-ASR deployment shape was
    # therefore never exercised by the default gate. Measured: with the overlay up all 6
    # pass, in 5m44s.
    #
    # The cost is real (~6 min on a ~22 min run) and was weighed against it: this is the
    # only automated coverage the lite path has, and a permanently-skipping test is the
    # false confidence scripts/audit-tests.py exists to remove.
    [mock-asr]=auto
    [diar-native]=auto
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
    [diar-native]=backend
)
# Optional per-overlay CONDITIONAL-NEED predicate: a function name that must return 0 for the
# overlay to be considered needed at all, evaluated AFTER phase and tier already matched. An
# overlay with no entry here is unconditionally needed by its phase/tier, which is how all five
# original overlays behave.
#
# Only diar-native has one, and it needs one: unlike a mock provider (always safe and always
# useful), the sidecar is only wanted when this deployment is actually CONFIGURED for native
# diarization. Starting it on a deployment configured for PyAnnote would reserve a GPU for a
# service nothing will consult. The predicate is the same one run-integration-tests.sh's
# diar-native phase gates on, so the overlay is started exactly when that phase would otherwise
# fail for its absence — never more, never less.
declare -A OVERLAY_NEED_PREDICATE=(
    [diar-native]=diar_native_sidecar_expected
)
#: Fixed iteration order for resolve_needed_overlays/print_overlay_plan — associative-array key
#: order is unspecified, and a stable order makes the printed plan/report reproducible.
OVERLAY_ORDER=(mock-llm keycloak-test ldap-test watch mock-asr diar-native)
declare -A OVERLAY_DESC=(
    [mock-llm]="mock LLM — chat/summarization/topic-extraction suites (backend + e2e chat)"
    [keycloak-test]="Keycloak/OIDC — test_auth_buttons.py::TestOIDCLogin, test_ldap_oidc.py (~60-90s to healthy)"
    [ldap-test]="LDAP — test_auth_buttons.py::TestLDAPLogin, test_ldap_oidc.py (fast, no healthcheck)"
    [watch]="watch-sources host-folder mount — test_watch_sources_e2e.py (recreates app containers)"
    [mock-asr]="mock cloud ASR — backend/tests/integration/test_lite_mode_mocked_providers.py"
    [diar-native]="native diarization sidecar — run-integration-tests.sh's diar-native CUDA EP phase, which FAILS (not skips) when the sidecar is configured but absent"
)

# Overlays teardown_overlays() must NOT stop even when this run started them, each with a
# written reason. Replaces what used to be a hardcoded `watch` branch inside the loop, so a
# second exemption is a table row rather than a second special case.
declare -A OVERLAY_TEARDOWN_EXEMPT=(
    [watch]="no dedicated container to stop; recreating backend/celery to drop the mount is out of proportion to a bind mount of ./watch"
    [diar-native]="NOT a mock — when the engine is configured native, the sidecar is part of the deployment's normal operating configuration. Stopping it leaves a running stack that silently falls back to in-process PyAnnote, which is the exact failure this overlay exists to prevent. Leave it up; ./opentr.sh stop takes it down with everything else."
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
    [gpu-split]="a distinct GPU-worker-topology change like --gpu-scale, not a container overlay; no test selector this script drives needs it"
    [llm-test]="reserves a real GPU for minutes at a time, so it is handled by run-dev-tests.sh's own --with-pipeline-smoke block directly (bring-up/health-wait/teardown), not by this table's generic auto-detected tiers — opt-in only, never auto-started under --full/--all-overlays"
    [monitoring]="Prometheus/Grafana; no test selector this script drives needs it"
    [pki]="needs the prod+nginx overlay (PKI/mTLS), not the dev stack this script targets — test_pki.py is out of scope here"
    [scratch-tmpfs]="declares no services, container_name or ports at all — it only re-drivers the existing pipeline_scratch named volume to tmpfs, so there is nothing for this script to start, health-check or tear down. Deliberately opt-in per host RAM (issue #661 E5); no test selector this script drives depends on the handoff being RAM-backed rather than disk-backed"
    [smb-test]="no test selector this script drives needs it; test_watch_sources_e2e.py only needs the local-folder mount from --with-watch"
)

# compose_project_name / overlay_container_name moved to scripts/lib/compose-project.sh so
# scripts/run-auth-e2e.sh can use them too. They were the ONLY two functions here with no
# dependency on this file's caller globals (or its EXIT trap), and run-auth-e2e.sh was guessing
# both the project and container names on its own — see that lib's header for the two bugs each
# guess has already caused. Sourced, not duplicated.
# shellcheck source=compose-project.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/compose-project.sh"

# diar_native_sidecar_expected — OVERLAY_NEED_PREDICATE[diar-native]. Shared with
# run-integration-tests.sh's diar-native phase so the overlay is started exactly when that
# phase would fail for its absence; see that lib's header for why it is not copied.
# shellcheck source=diar-native-expected.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/diar-native-expected.sh"

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
        # Conditional-need predicate, if this overlay declares one (see OVERLAY_NEED_PREDICATE).
        local predicate="${OVERLAY_NEED_PREDICATE[$flag]:-}"
        if [[ -n "$predicate" ]] && ! "$predicate"; then
            echo -e "${YELLOW}==>${NC} --with-$flag not needed on this deployment ($predicate said no) — skipping"
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
        local exempt="${OVERLAY_TEARDOWN_EXEMPT[$flag]:-}"
        if [[ -n "$exempt" ]]; then
            echo -e "${YELLOW}==>${NC} leaving --with-$flag up: $exempt"
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

# LLDAP ships one bootstrap account (`admin`). The e2e suite authenticates as `ldap-admin`
# and `ldap-user`, which EXIST in the seed data but whose passwords must be set after the
# server starts — backend/tests/AUTH_TEST_SETUP.md documents the two commands, and until now
# nothing ran them. So `--full` enabled LDAP, exported RUN_AUTH_E2E, ran the tests, and they
# failed on a bind the fixture had never been prepared for.
#
# That is worse than a plain failure. `LDAP password verification failed for user:
# ldap-admin` then falls through to local auth, which increments the lockout counter on the
# resolved account — observed at `attempt 3/5` after three gate runs. Two more and
# `ldap-admin` is locked for 15 minutes, poisoning every later test that authenticates as it,
# which is exactly the escalation backend/tests/CLAUDE.md warns about.
#
# Idempotent (lldap_set_password just sets), so it runs whenever the overlay is needed
# rather than only when this run started the container. Non-fatal: a failure here should
# leave the LDAP tests to report their own problem, not abort the whole gate.
seed_ldap_fixture_users() {
    local container
    container="$(overlay_container_name lldap)"
    if [[ -z "$container" ]]; then
        echo -e "${YELLOW}==>${NC} lldap container not found — skipping LDAP fixture seeding" >&2
        return 0
    fi
    local user pass ok=true
    for user_pass in "ldap-admin:admin_password" "ldap-user:user_password"; do
        user="${user_pass%%:*}"
        pass="${user_pass##*:}"
        if ! docker exec "$container" /app/lldap_set_password \
                --base-url "http://localhost:17170" \
                --admin-username admin --admin-password admin_password \
                --username "$user" --password "$pass" >/dev/null 2>&1; then
            ok=false
        fi
    done
    if $ok; then
        echo -e "${YELLOW}==>${NC} seeded LDAP fixture passwords (ldap-admin, ldap-user)"
    else
        echo -e "${YELLOW}==>${NC} could not seed LDAP fixture passwords in $container —" \
             "test_auth_buttons.py's LDAP cases will fail on the bind; see" \
             "backend/tests/AUTH_TEST_SETUP.md" >&2
    fi
}

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
        # ⚠️ Flags for EVERY needed overlay, not just the missing ones — including those
        # already up.
        #
        # `opentr.sh start dev` recreates the app services, and an overlay's compose file is
        # only in that chain if its flag is passed. Several overlays do not merely ADD a
        # container, they PATCH existing services: mock-asr sets GLADIA_API_BASE_URL and
        # ASR_ALLOW_PRIVATE_ENDPOINTS on backend and celery-cloud-asr-worker, and watch sets
        # WATCH_FOLDER_PATH on four. Omitting an already-up overlay's flag therefore recreates
        # those services WITHOUT its env, silently un-configuring it while its container keeps
        # running and every container-based check keeps reporting it healthy.
        #
        # Measured: a run that needed mock-asr (already up) plus keycloak/ldap (not) issued
        # `start dev --with-keycloak-test --with-ldap-test`; GLADIA_API_BASE_URL came back
        # <UNSET> on both services, the app stopped routing ASR to the mock, and all 6
        # lite-mode tests failed with `status=error` — 40 minutes after the same 6 passed.
        # The mock-asr container was up and healthy throughout, which is exactly why
        # "container running => overlay effective" is the wrong test.
        #
        # OVERLAYS_STARTED_BY_US still records only the ones that were actually missing, so
        # teardown never stops a container this run did not start.
        local with_flags=() f
        for f in "${OVERLAYS_NEEDED[@]}"; do with_flags+=("--with-$f"); done
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

    # Fixture seeding — same "needed, not merely started by us" rule as the DB reconciliation
    # above, and for the same reason: an already-running container can be missing the seeding
    # just as easily as a fresh one.
    for flag in "${OVERLAYS_NEEDED[@]}"; do
        [[ "$flag" == "ldap-test" ]] && seed_ldap_fixture_users
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
