#!/bin/bash
# Prove the native diarizer works end to end, on the deployment shape a real operator gets.
#
# The fallback to in-process PyAnnote is SILENT by design, so "no error appeared" is not
# evidence of anything. Every check here answers "which engine actually served this job?"
# rather than "did the request succeed?".
#
# Scenarios:
#   fresh    - no models directory at all. What a `git clone` + `start` looks like.
#   upgrade  - a populated models directory with NO diar-provision.json marker. What every
#              install created before the marker existed looks like, and the case that
#              silently degrades: /readyz gates on `verified` exactly, so a marker-less
#              directory reads as not-ready.
#   current  - whatever is on disk now. A cheap re-check after restoring.
#
# ⚠️ `fresh` and `upgrade` MUTATE the models directory, and this script has already cost
# this repository a corrupted 1.4 GB model tree. Moving a directory that a RUNNING container
# holds as a bind-mount source does not detach the container: the mount follows the inode, so
# the sidecar carried on serving a directory no path pointed at any more, `docker inspect`
# reported the original source, and a restart would silently have swapped the export. Three
# distinct copies ended up on disk.
#
# So those two scenarios now REFUSE to touch the live export. Point MODELS_DIR at a throwaway
# directory, or run them against an isolated stack:
#
#     ./opentr.sh start dev --fresh diarcheck --port-offset 100
#     MODELS_DIR=.fresh/diarcheck/diar-native-models ./scripts/verify-diar-native-e2e.sh fresh
#
# `current` is read-only and always safe. Overriding with OK_TO_MUTATE_LIVE_MODELS=1 is
# possible but deliberately ugly — the repo's rule is not to develop against live data.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

SCENARIO="${1:-current}"
LIVE_MODELS_DIR="$REPO_ROOT/models/diar-native"
MODELS_DIR="${MODELS_DIR:-$LIVE_MODELS_DIR}"
# Anchor the stash beside the target, not beside the live tree — a stash written into
# models/ while MODELS_DIR points elsewhere is how an "isolated" run still litters live data.
STASH_DIR="$(dirname "$MODELS_DIR")/.$(basename "$MODELS_DIR")-stashed-by-verify"
SIDECAR="$(docker ps --filter "label=com.docker.compose.service=diar-native" \
                     --format '{{.Names}}' | head -1)"
BACKEND="opentranscribe-backend"

# Read the backend's diar-native log lines ONCE, into a variable.
#
# ⚠️ Never `docker logs ... | grep -q` here. This script runs under `set -o pipefail`, and
# `grep -q` exits the instant it matches, so `docker logs` dies of SIGPIPE (141) and the
# PIPELINE reports failure even though the pattern was found. That is not theoretical: it
# is what made three separate runs report "no provisioning line in the backend log" and
# then print the very line they claimed was absent, which was misdiagnosed as a race.
backend_diar_log() {
    docker logs "$BACKEND" --since 30m 2>&1 | grep -i 'diar-native' || true
}

pass() { printf '  \033[0;32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[0;31mFAIL\033[0m  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
info() { printf '  ....  %s\n' "$1"; }
FAILURES=0

restore_models() {
    if [ -d "$STASH_DIR" ]; then
        printf '\n\033[0;34mRestoring the stashed model set...\033[0m\n'
        if [ -d "$MODELS_DIR" ] && [ -n "$(ls -A "$MODELS_DIR" 2>/dev/null)" ]; then
            # Keep whatever provisioning produced — it is the artifact under test.
            mv "$MODELS_DIR" "${MODELS_DIR}.provisioned-$(date +%s)"
        fi
        # ⚠️ Re-check IMMEDIATELY before the move, not once above. A backend container is
        # running with this path as a bind-mount source, so dockerd recreates it — empty and
        # root-owned — the instant the directory is moved away. `mv stash "$MODELS_DIR"` onto
        # a directory that exists moves the stash INSIDE it, producing a nested
        # models/diar-native/diar-native and two orphaned 462 MB copies. Observed exactly
        # once, which is once more than a restore path should ever surprise anyone.
        if [ -d "$MODELS_DIR" ]; then
            rmdir "$MODELS_DIR" 2>/dev/null || {
                echo "  !!!  $MODELS_DIR reappeared and is not empty — refusing to nest."
                echo "  !!!  Your model set is SAFE at: $STASH_DIR"
                return
            }
        fi
        mv "$STASH_DIR" "$MODELS_DIR" && info "restored $MODELS_DIR ($(ls "$MODELS_DIR" | wc -l) files)"
    fi
}
trap restore_models EXIT INT TERM

# --- refuse to mutate the live export ---------------------------------------------
# Compare resolved paths: a relative MODELS_DIR, a symlink or a trailing slash must not be
# able to sneak the live tree past a string comparison.
_resolved() { readlink -f "$1" 2>/dev/null || printf '%s' "$1"; }
if [ "$SCENARIO" != "current" ] \
   && [ "$(_resolved "$MODELS_DIR")" = "$(_resolved "$LIVE_MODELS_DIR")" ] \
   && [ "${OK_TO_MUTATE_LIVE_MODELS:-0}" != "1" ]; then
    printf '\033[0;31mREFUSING\033[0m: scenario "%s" mutates the models directory, and\n' "$SCENARIO"
    printf '  %s\nis the LIVE export this deployment serves from.\n\n' "$(_resolved "$MODELS_DIR")"
    printf 'Run it against an isolated stack instead:\n'
    printf '  ./opentr.sh start dev --fresh diarcheck --port-offset 100\n'
    printf '  MODELS_DIR=.fresh/diarcheck/diar-native-models %s %s\n\n' "$0" "$SCENARIO"
    printf 'Or set OK_TO_MUTATE_LIVE_MODELS=1 if you genuinely mean the live one.\n'
    printf '⚠️  A running container holding this path as a bind-mount source will keep\n'
    printf '   serving the old inode after it is moved — recreate the sidecar afterwards.\n'
    exit 2
fi

# --- scenario setup ---------------------------------------------------------------
printf '\033[1mScenario: %s\033[0m\n' "$SCENARIO"
case "$SCENARIO" in
    fresh)
        [ -d "$MODELS_DIR" ] && mv "$MODELS_DIR" "$STASH_DIR"
        info "moved the model set aside — the backend must export a new one"
        ;;
    upgrade)
        [ -d "$MODELS_DIR" ] && cp -a "$MODELS_DIR" "$STASH_DIR"
        rm -f "$MODELS_DIR/diar-provision.json"
        info "removed the marker, kept the files — a pre-0.3.0 install's exact shape"
        ;;
    current) ;;
    *) echo "usage: $0 [fresh|upgrade|current]"; exit 2 ;;
esac

# --- 1. provisioning --------------------------------------------------------------
printf '\n\033[1m1. Provisioning\033[0m\n'
if [ "$SCENARIO" != "current" ]; then
    info "restarting the backend so the lifespan runs (this can take ~140s on a cold export)"
    ./opentr.sh restart-backend >/dev/null 2>&1
    # Wait for the PROVISIONING LINE, not for /health. Health returns the moment the
    # lifespan completes, and the grep below then races the log becoming visible — a
    # measured cold export lands at ~140s and the check reported "no provisioning line"
    # while the very next `docker logs` call showed it. Poll for the thing being asserted.
    for _ in $(seq 1 90); do
        case "$(backend_diar_log)" in
            *"models exported"*|*"already provisioned"*|*"provisioning failed"*|*"provisioning skipped"*)
                break ;;
        esac
        sleep 5
    done
fi

DIAR_LOG="$(backend_diar_log)"
case "$DIAR_LOG" in
    *"diar-native models exported"*)
        pass "the backend exported a model set" ;;
    *"already provisioned"*)
        pass "a valid marker short-circuited the export (idempotent)" ;;
    *)
        fail "no provisioning line in the backend log"
        printf '%s\n' "$DIAR_LOG" | tail -5 ;;
esac

# --- 2. the marker ----------------------------------------------------------------
printf '\n\033[1m2. Marker\033[0m\n'
if [ -f "$MODELS_DIR/diar-provision.json" ]; then
    PRECISION=$(python3 -c "import json;print(json.load(open('$MODELS_DIR/diar-provision.json'))['toolchain'].get('gender_precision'))" 2>/dev/null)
    # The one export defect that raises NO error: without onnxconverter-common the
    # classifier ships at fp32 — 379 MB instead of 189 MB, ~500 MiB more VRAM, exit 0.
    [ "$PRECISION" = "fp16" ] && pass "gender_precision=fp16" \
                             || fail "gender_precision=$PRECISION (expected fp16)"
    SETNAME=$(python3 -c "import json;print(json.load(open('$MODELS_DIR/diar-provision.json'))['model_set'])" 2>/dev/null)
    pass "model_set=$SETNAME, $(du -sh "$MODELS_DIR" | cut -f1) on disk"
else
    fail "no diar-provision.json at $MODELS_DIR"
fi

# --- 3. readiness -----------------------------------------------------------------
printf '\n\033[1m3. Sidecar readiness\033[0m\n'
if [ -z "$SIDECAR" ]; then
    fail "no diar-native container in this compose project"
else
    HEALTH=$(docker exec "$SIDECAR" curl -s -o /dev/null -w '%{http_code}' http://localhost:8701/healthz 2>/dev/null)
    READY=$(docker exec "$SIDECAR" curl -s -o /dev/null -w '%{http_code}' http://localhost:8701/readyz 2>/dev/null)
    [ "$HEALTH" = "200" ] && pass "/healthz $HEALTH" || fail "/healthz $HEALTH"
    # /healthz is 200 in EVERY model state by design; only /readyz distinguishes
    # "serving" from "serving something usable".
    [ "$READY" = "200" ] && pass "/readyz $READY (models verified)" \
                        || fail "/readyz $READY — the sidecar is up but would be bypassed"
    docker exec "$SIDECAR" curl -s http://localhost:8701/readyz 2>/dev/null \
        | python3 -c "import json,sys;d=json.load(sys.stdin);print('  ....  state=%s device=%s gender=%s' % (d.get('models_state'),d.get('default_device'),d.get('models_gender')))" 2>/dev/null
fi

printf '\n\033[1mResult:\033[0m '
if [ "$FAILURES" -eq 0 ]; then
    printf '\033[0;32mall checks passed\033[0m\n'
else
    printf '\033[0;31m%d check(s) failed\033[0m\n' "$FAILURES"
fi
exit "$FAILURES"
