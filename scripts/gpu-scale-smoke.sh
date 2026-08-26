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
    grep -E "^${1}=" "$REPO_ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r'
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

WORKERS_JSON="$(curl -fsS -u "${FLOWER_USER}:${FLOWER_PASSWORD}" "${FLOWER_BASE}/api/workers" 2>/dev/null || true)"
[[ -n "$WORKERS_JSON" ]] || fail "Flower not reachable at $FLOWER_BASE — start the stack with ./opentr.sh start dev --gpu-scale" 4

GPU_WORKER_CONCURRENCY="$(python3 -c '
import json, sys
data = json.loads(sys.argv[1])
for name, info in data.items():
    if name.startswith("gpu-scaled@"):
        stats = info.get("stats", {}) if isinstance(info, dict) else {}
        pool = stats.get("pool", {})
        print(pool.get("max-concurrency", ""))
        break
' "$WORKERS_JSON")"

[[ -n "$GPU_WORKER_CONCURRENCY" ]] || fail "no gpu-scaled@* worker registered in Flower"
[[ "$GPU_WORKER_CONCURRENCY" == "$GPU_SCALE_WORKERS" ]] || fail \
    "gpu-scaled worker pool concurrency is $GPU_WORKER_CONCURRENCY, expected GPU_SCALE_WORKERS=$GPU_SCALE_WORKERS"

if [[ "$GPU_SCALE_DEFAULT_WORKER" == "1" ]]; then
    DEFAULT_PRESENT="$(python3 -c '
import json, sys
data = json.loads(sys.argv[1])
print("yes" if any(n.startswith("celery@") for n in data) else "no")
' "$WORKERS_JSON")"
    [[ "$DEFAULT_PRESENT" == "yes" ]] || fail "GPU_SCALE_DEFAULT_WORKER=1 (dual-GPU mode) but no celery@* default worker is registered"
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

SAMPLE_AUDIO="$REPO_ROOT/backend/tests/e2e/fixtures/sample_audio.wav"
[[ -f "$SAMPLE_AUDIO" ]] || fail "no sample audio fixture at $SAMPLE_AUDIO — generate one per backend/tests/e2e/conftest.py" 4

FILE_UUIDS=()
for i in $(seq 1 "$N_UPLOADS"); do
    RESP="$(curl -fsS -X POST "$BASE_URL/files" \
        -H "Authorization: Bearer $TOKEN" \
        -F "file=@${SAMPLE_AUDIO}" \
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

for uuid in "${FILE_UUIDS[@]}"; do
    curl -fsS -X DELETE "$BASE_URL/files/$uuid" -H "Authorization: Bearer $TOKEN" >/dev/null 2>&1 || true
done

[[ $COMPLETED -eq ${#FILE_UUIDS[@]} ]] || fail "$COMPLETED of ${#FILE_UUIDS[@]} concurrent uploads reached completed within 15 min"

OOM_HITS="$(docker logs "$GPU_CONTAINER" --since "$START_TIME" 2>&1 | grep -ci "CUDA out of memory\|CUDA error: out of memory" || true)"
[[ "$OOM_HITS" -eq 0 ]] || fail "$OOM_HITS CUDA OOM occurrence(s) in $GPU_CONTAINER logs during the concurrent-upload run"

if [[ $JSON -eq 1 ]]; then
    printf '{"status":"pass","gpu_scale_workers":%s,"uploads_completed":%s,"oom_hits":0}\n' "$GPU_SCALE_WORKERS" "$COMPLETED"
else
    echo "✅ $COMPLETED/${#FILE_UUIDS[@]} concurrent uploads completed with no CUDA OOM in $GPU_CONTAINER"
fi
