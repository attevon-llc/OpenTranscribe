#!/bin/bash
#
# scripts/lib/diar-native-expected.sh — "SHOULD this deployment be running the native
# diarization sidecar?", as one implementation.
#
# Sourced, not a standalone entry point. Requires no caller globals (it derives the repo
# root from its own location), installs no trap, and defines exactly one public function,
# so any dev script can source it — the same contract scripts/lib/compose-project.sh has.
#
# WHY THIS IS SHARED RATHER THAN COPIED
#
# The predicate mirrors opentr.sh's add_diar_native_overlay() start-mode ("CONFIGURATION")
# gate: engine.diarizer_backend resolves to native AND (an export already exists at
# DIAR_NATIVE_MODELS_DIR OR a HUGGINGFACE_TOKEN is configured to produce one on startup).
#
# It lived in run-integration-tests.sh, whose own comment said a second copy of "is native
# diarization configured?" is how this repo's env-var drift usually starts. Then
# run-dev-tests.sh needed the same question — to decide whether --with-diar-native belongs
# in its overlay set — which would have made a third. So it moved here instead: extracting
# it REMOVES a copy rather than adding one.
#
# ⚠️ opentr.sh still holds the fourth: it is a stateful CLI dispatcher (it mutates
# COMPOSE_FILES, exports globals) rather than an importable library, so its copy cannot be
# replaced by a source of this file without restructuring it. If opentr.sh's predicate
# changes, change this one in the same commit — see add_diar_native_overlay()'s comment
# block, around "start   - CONFIGURATION."

_dne_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Which .env to read. Defaults to the repo this library lives in, which is always right in
# production — both callers run from this checkout.
#
# ⚠️ The override exists because it is otherwise UNTESTABLE, and that was not theoretical:
# the predicate resolves each input as `${VAR:-$(_dne_read_env_var VAR)}`, and `:-` treats an
# EMPTY value as unset. So a test that sets `HUGGINGFACE_TOKEN=""` to simulate "no token
# configured" silently falls through to the developer's real .env — where a token IS set —
# and its "sidecar not expected" scenario becomes "expected". That is a test reading the
# machine it runs on rather than the case it describes; it is why
# test_run_integration_tests_diar_native_gate.py points this at a tmp dir.
_dne_repo_root="${DIAR_NATIVE_EXPECTED_REPO_ROOT:-$(cd "$_dne_lib_dir/../.." && pwd)}"

# _dne_read_env_var KEY
#   One value out of the repo's .env, or "" when there is no .env. Underscore-prefixed
#   because this file is SOURCED into scripts that have their own helper namespaces — a
#   bare `read_env_var` here would silently redefine or be redefined by a caller's.
_dne_read_env_var() {
    [ -f "$_dne_repo_root/.env" ] || return 0
    python3 "$_dne_lib_dir/env_reader.py" "$_dne_repo_root/.env" "$1"
}

# diar_native_sidecar_expected
#   0 when this deployment should have the sidecar running, 1 otherwise. An ambient
#   environment variable wins over .env, matching how compose itself resolves them.
diar_native_sidecar_expected() {
    local backend models_dir token
    backend="${ENGINE_DIARIZER_BACKEND:-$(_dne_read_env_var ENGINE_DIARIZER_BACKEND)}"
    backend="${backend:-native}"
    [ "$backend" = "native" ] || return 1

    models_dir="${DIAR_NATIVE_MODELS_DIR:-$(_dne_read_env_var DIAR_NATIVE_MODELS_DIR)}"
    if [ -z "$models_dir" ]; then
        # Mirror opentr.sh's resolve_diar_native_models_dir() cascade (~opentr.sh:269-296):
        # DIAR_NATIVE_MODELS_DIR unset falls through to ${MODEL_CACHE_DIR}/diar-native, then
        # a legacy path. Without this, an air-gapped install with a populated export under
        # the MODEL_CACHE_DIR cascade (never DIAR_NATIVE_MODELS_DIR itself, which
        # .env.example ships commented out) reads as "not expected" here while
        # opentr.sh:~687 auto-loads the sidecar anyway — this gate would report "not
        # expected" and exit 0 on a dead diarizer.
        local cache_dir standard legacy
        cache_dir="${MODEL_CACHE_DIR:-$(_dne_read_env_var MODEL_CACHE_DIR)}"
        standard="${cache_dir:-./models}/diar-native"
        # ⚠️ The legacy leg is a WORKSTATION path that exists on exactly one machine (the
        # maintainer's), which is precisely why it is overridable. A test asserting "no export
        # anywhere => sidecar not expected" passes in CI and FAILS on that machine, because the
        # legacy directory is really there and really populated — a test reporting on the host
        # it runs on rather than the case it describes. Same defect class as
        # DIAR_NATIVE_EXPECTED_REPO_ROOT above. Default is unchanged; opentr.sh:~276 is the
        # copy this mirrors.
        legacy="${DIAR_NATIVE_EXPECTED_LEGACY_DIR:-/mnt/nvm/repos/diar-native/models_folded}"
        if [ -d "$standard" ] && [ -n "$(ls -A "$standard" 2>/dev/null)" ]; then
            models_dir="$standard"
        elif [ -d "$legacy" ]; then
            models_dir="$legacy"
        else
            models_dir="$standard"
        fi
    fi
    if [ -n "$models_dir" ] && [ -d "$models_dir" ] && [ -n "$(ls -A "$models_dir" 2>/dev/null)" ]; then
        return 0
    fi

    token="${HUGGINGFACE_TOKEN:-$(_dne_read_env_var HUGGINGFACE_TOKEN)}"
    [ -n "$token" ]
}
