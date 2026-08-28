#!/bin/bash
#
# Smoke check for --gpu-scale dev deployments (full-test-matrix.md, Stage 2, Cycle 2B).
#
# WHAT THIS CHECKS AND WHY IT IS NOT "N WORKERS REGISTERED"
# -----------------------------------------------------------
# docker-compose.gpu-scale.yml runs exactly ONE celery process, `gpu-scaled@%h`,
# with `--pool=${GPU_SCALE_POOL:-threads} --concurrency=${GPU_SCALE_WORKERS:-4}`.
# So Flower's /api/workers never shows N processes — it shows one worker whose
# pool max-concurrency equals GPU_SCALE_WORKERS. Checking for N registered
# workers would fail against a correctly configured deployment; this checks the
# concurrency value on the one process instead, plus the optional default
# worker (`celery@%h`) when GPU_SCALE_DEFAULT_WORKER=1 (dual-GPU mode).
#
# Then it drives real concurrency: N+1 uploads dispatched together must all
# reach `completed`, and celery-worker-gpu-scaled's logs must show no CUDA OOM
# during the run.
#
# Usage:
#   scripts/gpu-scale-smoke.sh              # full check incl. concurrent uploads
#   scripts/gpu-scale-smoke.sh --json       # machine-readable verdict
#   scripts/gpu-scale-smoke.sh --check-only # Flower/topology check only, no uploads
#
# Exit codes: 0 pass · 1 check failed · 4 NOT MEASURED (no stack, no Flower, no fixtures).
#
# ⚠️ issue #609: /api/workers is NOT a live worker roster. flower 2.1.0
# populates it from a SINGLE `celery inspect` broadcast issued once in
# Flower.start() (flower/app.py:98) with a 1 s reply timeout, and never
# re-inspects on a timer — grep the installed package for update_workers /
# PeriodicCallback and there is no periodic inspection call at all. A GPU
# worker that is still importing torch/whisperx, or running
# @worker_ready preload_models() synchronously in its own main thread
# (backend/app/core/celery.py, PRELOAD_GPU_MODELS), is not ready to answer a
# broadcast inside Flower's one-second boot window and is absent from the
# cached response FOREVER — waiting and re-checking cannot help, because
# nothing ever asks Flower to look again. `?refresh=1` is Flower's own
# documented query parameter (it awaits a fresh inspect broadcast for THIS
# request) and is what makes this check meaningful; without it, this script
# was asserting on a snapshot taken before the GPU worker had a chance to
# answer, which is exactly why "no gpu-scaled@* worker registered in Flower"
# could fire against a demonstrably healthy, correctly running deployment.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

JSON=0
CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --json) JSON=1 ;;
        --check-only) CHECK_ONLY=1 ;;
    esac
done

fail() {
    if [[ $JSON -eq 1 ]]; then
        printf '{"status":"fail","reason":%s}\n' "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
    else
        echo "❌ $1" >&2
    fi
    exit "${2:-1}"
}

read_env() {
    [[ -f "$REPO_ROOT/.env" ]] || return 0
    # Strip a trailing ` # comment` before the quote/whitespace cleanup — .env lines
    # like `FLOWER_PORT=5175  # Celery Task Monitor` otherwise corrupt the value
    # (the comment text survives tr -d's space-stripping and gets glued onto it).
    grep -E "^${1}=" "$REPO_ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2- | sed -E 's/[[:space:]]+#.*$//' | tr -d '"'"'"' \r'
}

GPU_SCALE_WORKERS="${GPU_SCALE_WORKERS:-$(read_env GPU_SCALE_WORKERS)}"
[[ -n "$GPU_SCALE_WORKERS" ]] || GPU_SCALE_WORKERS=4
GPU_SCALE_DEFAULT_WORKER="${GPU_SCALE_DEFAULT_WORKER:-$(read_env GPU_SCALE_DEFAULT_WORKER)}"
[[ -n "$GPU_SCALE_DEFAULT_WORKER" ]] || GPU_SCALE_DEFAULT_WORKER=0

FLOWER_PORT="${FLOWER_PORT:-$(read_env FLOWER_PORT)}"
[[ -n "$FLOWER_PORT" ]] || FLOWER_PORT=5175
FLOWER_URL_PREFIX="${FLOWER_URL_PREFIX:-$(read_env FLOWER_URL_PREFIX)}"
[[ -n "$FLOWER_URL_PREFIX" ]] || FLOWER_URL_PREFIX=flower
FLOWER_USER="${FLOWER_USER:-$(read_env FLOWER_USER)}"
[[ -n "$FLOWER_USER" ]] || FLOWER_USER="admin"
FLOWER_PASSWORD="${FLOWER_PASSWORD:-$(read_env FLOWER_PASSWORD)}"
[[ -n "$FLOWER_PASSWORD" ]] || FLOWER_PASSWORD=flower

FLOWER_BASE="http://127.0.0.1:${FLOWER_PORT}/${FLOWER_URL_PREFIX}"

command -v curl >/dev/null 2>&1 || fail "curl not available" 4
command -v python3 >/dev/null 2>&1 || fail "python3 not available" 4

# flower's /api/workers is a boot-time snapshot cache (see header). Ask it to
# actually broadcast with ?refresh=1 (Flower's own documented parameter,
# awaited server-side — flower/api/workers.py) and retry: a refresh landing
# while the boot-time inspect is still in flight de-duplicates onto that
# stale task (Inspector.inspect()), and a threads-pool GPU worker mid
# preload_models()/mid-transcription can hold the GIL long enough to miss
# even a widened reply deadline.
FLOWER_REFRESH_ATTEMPTS="${FLOWER_REFRESH_ATTEMPTS:-6}"
FLOWER_REFRESH_INTERVAL="${FLOWER_REFRESH_INTERVAL:-10}"
WORKERS_JSON=""
for _attempt in $(seq 1 "$FLOWER_REFRESH_ATTEMPTS"); do
    # --max-time must exceed the container's --inspect_timeout (10 s), or a
    # curl abort reintroduces #609 in a new shape.
    WORKERS_JSON="$(curl -fsS --max-time 40 -u "${FLOWER_USER}:${FLOWER_PASSWORD}" \
        "${FLOWER_BASE}/api/workers?refresh=1" 2>/dev/null || true)"
    if [[ -n "$WORKERS_JSON" ]] && grep -q '"gpu-scaled@' <<<"$WORKERS_JSON"; then
        break
    fi
    sleep "$FLOWER_REFRESH_INTERVAL"
done
[[ -n "$WORKERS_JSON" ]] || fail "Flower not reachable at $FLOWER_BASE — start the stack with ./opentr.sh start dev --gpu-scale" 4

# WORKERS_JSON can exceed ARG_MAX with a full worker roster's stats blob
# (observed with 8 registered workers) — pipe via stdin, never sys.argv.
#
# Entry-freshness matters because Inspector.workers (flower/inspector.py) is a
# defaultdict(dict) that is NEVER pruned — purge_offline_workers only touches
# state.metrics, not this dict — so a worker seen once in an earlier
# ?refresh=1 stays in the response forever, even after its container is gone.
# A cached entry older than FLOWER_MAX_ENTRY_AGE seconds is not proof the
# worker is alive right now (issue #609 edge case: a stale gpu-transcription@
# entry could false-pass the GPU_SCALE_DEFAULT_WORKER=1 check below after that
# worker was scaled to 0 without restarting Flower).
FLOWER_MAX_ENTRY_AGE="${FLOWER_MAX_ENTRY_AGE:-120}"
# `IFS='|' read`, not the default whitespace-split `read`: `print(a, b)` with `a` empty
# (a worker present but with no `stats`/`pool` yet — issue #609's "inspect broadcast
# timed out" case) prints " fresh", and a plain `read -r A B <<< " fresh"` strips the
# leading space and treats it as ONE field, silently shifting "fresh" into
# GPU_WORKER_CONCURRENCY and leaving GPU_WORKER_FRESH empty — the wrong field held the
# wrong value, and the failure two branches down then misreported "stale" for a case
# that was never about age at all.
IFS='|' read -r GPU_WORKER_CONCURRENCY GPU_WORKER_FRESH <<<"$(python3 -c '
import json, sys, time
data = json.loads(sys.stdin.read())
max_age = '"$FLOWER_MAX_ENTRY_AGE"'
for name, info in data.items():
    if name.startswith("gpu-scaled@"):
        stats = info.get("stats", {}) if isinstance(info, dict) else {}
        pool = stats.get("pool", {})
        age = time.time() - float(info.get("timestamp", 0) or 0)
        fresh = "fresh" if age <= max_age else "stale"
        print("|".join([str(pool.get("max-concurrency", "")), fresh]))
        break
else:
    print("|")
' <<<"$WORKERS_JSON")"

if [[ -z "$GPU_WORKER_CONCURRENCY" && -z "$GPU_WORKER_FRESH" ]]; then
    KNOWN_WORKERS="$(python3 -c 'import json, sys
data = json.loads(sys.stdin.read())
print(", ".join(sorted(data)) or "(none)")' <<<"$WORKERS_JSON" 2>/dev/null || echo "(unparseable response)")"
    fail "no gpu-scaled@* worker registered in Flower after $FLOWER_REFRESH_ATTEMPTS refresh attempt(s).
Flower returned: $KNOWN_WORKERS
Cross-check with: ./opentr.sh shell celery-worker -> celery -A app.core.celery inspect ping
If inspect ping lists gpu-scaled@ but Flower does not, re-read issue #609."
fi
if [[ -n "$GPU_WORKER_FRESH" && -z "$GPU_WORKER_CONCURRENCY" ]]; then
    fail "gpu-scaled@* is registered but reported no stats — its inspect broadcast timed out (issue #609), which is not the same as a stale entry."
fi
[[ "$GPU_WORKER_FRESH" == "fresh" ]] || fail \
    "gpu-scaled@* worker's Flower entry is stale (older than ${FLOWER_MAX_ENTRY_AGE}s) — Inspector.workers is never pruned, so this is a leftover cached entry, not proof the worker is alive now (issue #609)."
[[ "$GPU_WORKER_CONCURRENCY" == "$GPU_SCALE_WORKERS" ]] || fail \
    "gpu-scaled worker pool concurrency is $GPU_WORKER_CONCURRENCY, expected GPU_SCALE_WORKERS=$GPU_SCALE_WORKERS"

if [[ "$GPU_SCALE_DEFAULT_WORKER" == "1" ]]; then
    # The default single-GPU worker's real Celery hostname is gpu-transcription@%h
    # (docker-compose.yml's celery-worker service, --hostname gpu-transcription@%h)
    # — it has never been "celery@%h"; that stale assumption made this check fail
    # unconditionally in dual-GPU mode even against a correctly running deployment.
    # Same IFS='|' separator as the gpu-scaled@ block above, for the same reason: a
    # whitespace-split `read` is one empty field away from silently shifting values
    # between GPU_WORKER_CONCURRENCY-style pairs. Both branches here always print two
    # non-empty tokens today, but the separator choice should not depend on that staying
    # true forever.
    IFS='|' read -r DEFAULT_PRESENT DEFAULT_FRESH <<<"$(python3 -c '
import json, sys, time
data = json.loads(sys.stdin.read())
max_age = '"$FLOWER_MAX_ENTRY_AGE"'
for name, info in data.items():
    if name.startswith("gpu-transcription@"):
        age = time.time() - float(info.get("timestamp", 0) or 0)
        fresh = "fresh" if age <= max_age else "stale"
        print("|".join(["yes", fresh]))
        break
else:
    print("|".join(["no", ""]))
' <<<"$WORKERS_JSON")"
    [[ "$DEFAULT_PRESENT" == "yes" ]] || fail "GPU_SCALE_DEFAULT_WORKER=1 (dual-GPU mode) but no gpu-transcription@* default worker is registered"
    [[ "$DEFAULT_FRESH" == "fresh" ]] || fail \
        "GPU_SCALE_DEFAULT_WORKER=1 (dual-GPU mode) but the gpu-transcription@* entry in Flower is stale (older than ${FLOWER_MAX_ENTRY_AGE}s) — Inspector.workers is never pruned, so this could be a leftover entry from before the worker was scaled to 0 (issue #609)."
fi

if [[ $CHECK_ONLY -eq 1 ]]; then
    if [[ $JSON -eq 1 ]]; then
        printf '{"status":"pass","gpu_scale_workers":%s,"default_worker":"%s"}\n' "$GPU_SCALE_WORKERS" "$GPU_SCALE_DEFAULT_WORKER"
    else
        echo "✅ gpu-scaled worker registered in Flower with concurrency $GPU_SCALE_WORKERS"
    fi
    exit 0
fi

# --- concurrent-upload leg ---------------------------------------------------
# Not implemented as a standalone uploader here: reuses the existing E2E upload
# helper so "a file reaches completed" is the SAME definition the rest of the
# suite uses, rather than a second one invented for this script.
UPLOAD_HELPER="$REPO_ROOT/backend/tests/fixtures/search_corpus_stack.py"
[[ -f "$UPLOAD_HELPER" ]] || fail "no upload fixture found to drive concurrent uploads" 4

GPU_CONTAINER="$(docker ps --filter "name=celery-worker-gpu-scaled" --format '{{.Names}}' | head -1)"
[[ -n "$GPU_CONTAINER" ]] || fail "no running celery-worker-gpu-scaled container" 4

N_UPLOADS=$((GPU_SCALE_WORKERS >= 3 ? GPU_SCALE_WORKERS : 3))
START_TIME="$(date -u +%Y-%m-%dT%H:%M:%S)"

echo "Dispatching $N_UPLOADS concurrent uploads against the live stack (see backend/tests/CLAUDE.md for the upload contract this reuses)..." >&2
BACKEND_PORT="${BACKEND_PORT:-$(read_env BACKEND_PORT)}"
[[ -n "$BACKEND_PORT" ]] || BACKEND_PORT=5174
BASE_URL="http://localhost:${BACKEND_PORT}/api"

TOKEN="$(curl -fsS -X POST "$BASE_URL/auth/login" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode "username=admin@example.com" \
    --data-urlencode "password=password" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])' 2>/dev/null || true)"
[[ -n "$TOKEN" ]] || fail "could not authenticate against $BASE_URL — is the dev stack up?" 4

# NOT e2e/fixtures/sample_audio.wav — that fixture is a deliberately silent
# 440 Hz sine tone ("passes magic-byte validation", per its own docstring in
# conftest.py), built for UI/upload-flow tests that never touch ASR. Fed
# through this script's real transcription pipeline it produces zero segments
# and every file lands in status=error, not completed — this script needs
# real speech content to prove the GPU pipeline actually completes work.
SAMPLE_AUDIO="$REPO_ROOT/backend/tests/fixtures/media/sample_short.wav"
[[ -f "$SAMPLE_AUDIO" ]] || fail "no sample audio fixture at $SAMPLE_AUDIO — see backend/tests/fixtures/media/README.md" 4

FILE_UUIDS=()

# Cleanup must fire on ANY exit (success, fail(), or an unhandled error under
# set -e) — this script must never leave test files behind in dev data, same
# rule as backend/tests/CLAUDE.md's E2E hygiene requirement. A prior manual
# run left 8 orphaned files that had to be found and deleted by hand; this
# trap is what makes that a one-time fix instead of a recurring chore.
cleanup_uploaded_files() {
    [[ ${#FILE_UUIDS[@]} -gt 0 ]] || return 0
    echo "Cleaning up ${#FILE_UUIDS[@]} smoke-test file(s)..." >&2
    for uuid in "${FILE_UUIDS[@]}"; do
        curl -fsS -X DELETE "$BASE_URL/files/$uuid" -H "Authorization: Bearer $TOKEN" >/dev/null 2>&1 || true
    done
}
trap cleanup_uploaded_files EXIT

for i in $(seq 1 "$N_UPLOADS"); do
    # `;type=audio/wav` is required, not decorative: curl's own MIME-type guess
    # for -F "file=@path" (mime.types-derived) can fall back to
    # application/octet-stream on hosts with no .wav mapping, and the backend's
    # validate_file_type() rejects anything not starting audio/ or video/.
    RESP="$(curl -fsS -X POST "$BASE_URL/files" \
        -H "Authorization: Bearer $TOKEN" \
        -F "file=@${SAMPLE_AUDIO};type=audio/wav" \
        -F "title=gpu-scale-smoke-${i}-$$" || true)"
    UUID="$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("uuid",""))' 2>/dev/null || true)"
    [[ -n "$UUID" ]] && FILE_UUIDS+=("$UUID")
done
[[ ${#FILE_UUIDS[@]} -eq $N_UPLOADS ]] || fail "only ${#FILE_UUIDS[@]} of $N_UPLOADS uploads were accepted"

DEADLINE=$((SECONDS + 900))
COMPLETED=0
while [[ $SECONDS -lt $DEADLINE && $COMPLETED -lt ${#FILE_UUIDS[@]} ]]; do
    COMPLETED=0
    for uuid in "${FILE_UUIDS[@]}"; do
        STATUS="$(curl -fsS "$BASE_URL/files/$uuid" -H "Authorization: Bearer $TOKEN" \
            | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || true)"
        [[ "$STATUS" == "completed" ]] && COMPLETED=$((COMPLETED + 1))
    done
    [[ $COMPLETED -eq ${#FILE_UUIDS[@]} ]] || sleep 10
done

[[ $COMPLETED -eq ${#FILE_UUIDS[@]} ]] || fail "$COMPLETED of ${#FILE_UUIDS[@]} concurrent uploads reached completed within 15 min"

OOM_HITS="$(docker logs "$GPU_CONTAINER" --since "$START_TIME" 2>&1 | grep -ci "CUDA out of memory\|CUDA error: out of memory" || true)"
[[ "$OOM_HITS" -eq 0 ]] || fail "$OOM_HITS CUDA OOM occurrence(s) in $GPU_CONTAINER logs during the concurrent-upload run"

if [[ $JSON -eq 1 ]]; then
    printf '{"status":"pass","gpu_scale_workers":%s,"uploads_completed":%s,"oom_hits":0}\n' "$GPU_SCALE_WORKERS" "$COMPLETED"
else
    echo "✅ $COMPLETED/${#FILE_UUIDS[@]} concurrent uploads completed with no CUDA OOM in $GPU_CONTAINER"
fi
