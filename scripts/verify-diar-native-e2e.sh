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
# The models directory is MOVED, never deleted (462 MB, and it is the only copy on this
# host). Restoration runs from a trap so an interrupted run still puts it back.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

SCENARIO="${1:-current}"
MODELS_DIR="${MODELS_DIR:-$REPO_ROOT/models/diar-native}"
STASH_DIR="$REPO_ROOT/models/.diar-native-stashed-by-verify"
SIDECAR="$(docker ps --filter "label=com.docker.compose.service=diar-native" \
                     --format '{{.Names}}' | head -1)"
BACKEND="opentranscribe-backend"

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
        if docker logs "$BACKEND" --since 30m 2>&1 \
             | grep -qE 'models exported|already provisioned|provisioning (failed|skipped)'; then
            break
        fi
        sleep 5
    done
fi

if docker logs "$BACKEND" --since 30m 2>&1 | grep -q "diar-native models exported"; then
    pass "the backend exported a model set"
elif docker logs "$BACKEND" --since 30m 2>&1 | grep -q "already provisioned"; then
    pass "a valid marker short-circuited the export (idempotent)"
else
    fail "no provisioning line in the backend log"
    docker logs "$BACKEND" --since 30m 2>&1 | grep -i "diar-native" | tail -5
fi

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
