#!/usr/bin/env bash
# Scenario C's demo leg: the public-demo overlay started on the lite stack's OWN images.
#
# What it proves that no other step does: docker-compose.demo.yml starts and serves on the
# SHIPPED bytes. The static test (backend/tests/unit/test_nginx_upstreams_exist_in_every_mode.py)
# reads configs; only a start shows the frontend actually comes up. S6v2 found the demo frontend
# crash-looping because nginx could not resolve Flower, which the overlay scales to 0.
#
# Cost: no rebuild and no pull (`--no-build --pull never`), one extra `up -d`.
#
# Sourced by test-lite-mode.sh. Needs: as_record, gr_log, st_target/st_require
# (scripts/lib/stack-target.sh), and from the caller TEST_PROJECT_NAME, TEST_FRONTEND_PORT.
# as_fail is the assertion counter owned by lib/assertions.sh.
# The refusal code mirrors backend/app/middleware/demo_readonly.py (REFUSAL_CODE).

# shellcheck source=../../lib/stack-target.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/scripts/lib/stack-target.sh"

DEMO_REFUSAL_CODE="demo_read_only"
: "${as_fail:=0}"

# demo_leg_chain_args -> the lite chain plus the demo overlay, one arg per line.
# The front end owns the selection: ./opentranscribe.sh selects lite from DEPLOYMENT_MODE and the
# demo overlay from DEMO_MODE=true in .env (compose_overlay_lite / compose_overlay_demo). The diar-native
# sidecar (auto-selected only with models or a token) and the two mocks come in through its
# OPENTRANSCRIBE_EXTRA_COMPOSE_FILES test seam. The demo overlay therefore lands before the mocks
# rather than last; `docker compose config` resolves byte-identically in either order.
# Runs in the install dir, with .env already holding DEMO_MODE=true / DEPLOYMENT_MODE=lite
# (demo_leg_set_env_key): read_env_value takes the FIRST match, so appending after the template's
# own `DEMO_MODE=false` line would be ignored and the demo overlay would silently drop out.
demo_leg_chain_args() {
    local ov extra=""
    for ov in diar-native mock-asr mock-llm; do extra="$extra$(printf ' -f %s' "docker-compose.$ov.yml")"; done
    DEPLOYMENT_MODE=lite OPENTRANSCRIBE_EXTRA_COMPOSE_FILES="${extra# }" \
        ./opentranscribe.sh compose-files | tail -1 | tr ' ' '\n' | sed '/^$/d'
}

# demo_leg_set_env_key KEY VALUE -> replace KEY's line in ./.env, or append it when absent
demo_leg_set_env_key() {
    if grep -qE "^[[:space:]]*(export[[:space:]]+)?$1=" .env 2>/dev/null; then
        sed -i -E "s|^[[:space:]]*(export[[:space:]]+)?$1=.*|$1=$2|" .env
    else
        printf '%s=%s\n' "$1" "$2" >> .env
    fi
}

# demo_leg_chain_ok CHAIN_ARGS... -> 0 when lite, demo and the three seam overlays are all in it
demo_leg_chain_ok() {
    local want chain=" $* "
    for want in lite demo diar-native mock-asr mock-llm; do
        [[ "$chain" == *" docker-compose.$want.yml "* ]] || return 1
    done
}

# demo_leg_classify_write HTTP_CODE BODY -> PASS|FAIL (a write counts as refused only on 403 + the demo code)
demo_leg_classify_write() {
    if [[ "$1" == "403" && "$2" == *"\"$DEMO_REFUSAL_CODE\""* ]]; then echo PASS; else echo FAIL; fi
}

# demo_leg_verdict pass|fail DETAIL -> the machine-readable line scripts/release/65-rehearse.sh records
demo_leg_verdict() {
    [[ -n "${DEMO_LEG_VERDICT_FILE:-}" ]] || return 0
    printf '%s\t%s\n' "$1" "$2" >> "$DEMO_LEG_VERDICT_FILE"
}

# demo_leg_restore_env -> put the install's .env back exactly as it was (RETURN trap of demo_leg_run)
demo_leg_restore_env() {
    trap - RETURN
    if [[ -n "$_DEMO_LEG_ENV_BACKUP" ]]; then
        cat "$_DEMO_LEG_ENV_BACKUP" > "$_DEMO_LEG_ENV_FILE"
        rm -f "$_DEMO_LEG_ENV_BACKUP"
    else
        rm -f "$_DEMO_LEG_ENV_FILE"
    fi
}

# demo_leg_run INSTALL_DIR
demo_leg_run() {
    local target="$1" args=() line
    pushd "$target" >/dev/null || return 1
    _DEMO_LEG_ENV_FILE="$(pwd)/.env"
    _DEMO_LEG_ENV_BACKUP=""
    if [[ -f .env ]]; then _DEMO_LEG_ENV_BACKUP="$(mktemp)"; cp .env "$_DEMO_LEG_ENV_BACKUP"; fi
    trap demo_leg_restore_env RETURN
    demo_leg_set_env_key DEMO_MODE true
    demo_leg_set_env_key DEPLOYMENT_MODE lite
    while IFS= read -r line; do args+=("$line"); done < <(demo_leg_chain_args)
    if ! demo_leg_chain_ok "${args[@]}"; then
        as_record FAIL "demo leg: the resolved chain lacks lite, demo or a seam overlay" "chain: ${args[*]:-<empty>}"
        demo_leg_verdict fail "chain incomplete: ${args[*]:-<empty>}"
        popd >/dev/null || true
        return 0
    fi
    # The overlay bind-mounts ./demo-status.json read-only; a missing file becomes a directory.
    [[ -f demo-status.json ]] || : > demo-status.json
    gr_log "demo leg: up -d with docker-compose.demo.yml on the already-built images (no build, no pull)"
    if ! docker compose -p "$TEST_PROJECT_NAME" "${args[@]}" up -d --no-build --pull never --wait --wait-timeout 300; then
        as_record FAIL "demo leg: stack with the demo overlay did not come up"
        demo_leg_verdict fail "demo overlay did not come up"
        popd >/dev/null || true
        return 0
    fi
    popd >/dev/null || return 1
    local fails_before=$as_fail

    if st_target --project "$TEST_PROJECT_NAME" && st_require healthy:frontend,backend; then
        as_record PASS "demo leg: frontend and backend healthy with the demo overlay"
    else
        as_record FAIL "demo leg: frontend or backend not healthy with the demo overlay"
    fi

    local base="http://localhost:${TEST_FRONTEND_PORT}" code body
    code=$(curl -s -o /dev/null -m 20 -w '%{http_code}' "$base/api/health" || echo 000)
    if [[ "$code" == "200" ]]; then
        as_record PASS "demo leg: /api/health is 200 through the frontend"
    else
        as_record FAIL "demo leg: /api/health through the frontend" "got HTTP $code"
    fi

    # Writes are refused before routing, so no credential is needed and nothing can be mutated.
    local out
    out=$(curl -s -m 20 -X POST -H 'Content-Type: application/json' -d '{}' \
        -w '\n%{http_code}' "$base/api/tags/" || printf '\n000')
    code="${out##*$'\n'}"; body="${out%$'\n'*}"
    if [[ "$(demo_leg_classify_write "$code" "$body")" == PASS ]]; then
        as_record PASS "demo leg: a write is refused with $DEMO_REFUSAL_CODE"
    else
        as_record FAIL "demo leg: a write is refused with $DEMO_REFUSAL_CODE" "got HTTP $code: ${body:0:120}"
    fi
    if (( as_fail == fails_before )); then
        demo_leg_verdict pass "frontend healthy, /api/health 200, write refused with $DEMO_REFUSAL_CODE"
    else
        demo_leg_verdict fail "$(( as_fail - fails_before )) demo-leg assertion(s) failed"
    fi
}

# demo_leg_phase -> the phase body for test-lite-mode.sh (install dir as phase 03/04 find it)
demo_leg_phase() {
    local target="$TEST_ROOT/install/opentranscribe"
    [[ -d "$target" ]] || target="$TEST_ROOT/install"
    TEST_REPORT_FILE="${TEST_REPORT_FILE:-$TEST_ROOT/REPORT.md}"
    export TEST_REPORT_FILE
    cp "$REPO_ROOT/docker-compose.demo.yml" "$target/docker-compose.demo.yml"
    demo_leg_run "$target"
}
