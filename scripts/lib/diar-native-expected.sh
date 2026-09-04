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
_dne_repo_root="$(cd "$_dne_lib_dir/../.." && pwd)"

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
    if [ -n "$models_dir" ] && [ -d "$models_dir" ] && [ -n "$(ls -A "$models_dir" 2>/dev/null)" ]; then
        return 0
    fi

    token="${HUGGINGFACE_TOKEN:-$(_dne_read_env_var HUGGINGFACE_TOKEN)}"
    [ -n "$token" ]
}
