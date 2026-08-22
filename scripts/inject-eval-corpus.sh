#!/bin/bash
# Inject a RAG-eval corpus into a running OpenTranscribe stack — no ASR.
#
# This is the "one command from a clean stack to an indexed corpus" entry point
# for issue #403 Stage 1. It resolves the target stack's host ports (including a
# --fresh deployment's port offset), then runs
# `python -m app.scripts.corpus_injection`, which parses the corpus into
# MediaFile / Speaker / TranscriptSegment rows and dispatches the PRODUCTION
# search-indexing task.
#
#   ./scripts/inject-eval-corpus.sh --fresh rag403 --corpus qmsum
#   ./scripts/inject-eval-corpus.sh --fresh rag403 --corpus qmsum --limit 10 --dry-run
#   ./scripts/inject-eval-corpus.sh --port-offset 100 --corpus synthetic
#
# Anything after the recognised flags is passed straight through to the Python
# CLI (`--help` for the full list).
#
# ⚠️ Dispatching the indexing task is not the same as indexing succeeding. By
# default this script blocks after injection and verifies the corpus actually
# SETTLED in OpenSearch (scripts/verify-eval-corpus-settled.py, reusing the eval
# harness's own settle rule) and exits non-zero if it didn't — measured once as
# the alternative: 232 meetings injected, every index_transcript_search task
# crashed, transcript_chunks was never created, and the injector still printed
# "Done" and exited 0. Skip the wait with --no-wait/--skip-settle (dispatch-only,
# e.g. for `--dispatch none` or `--dry-run` runs, which skip it automatically);
# tune the bound with --settle-timeout SECONDS [1800].
#
# Method, resolved design questions and manifest format: .rag-403/corpus-injection.md
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Base host ports from docker-compose.yml. A --fresh deployment shifts every one
# of them by its recorded offset.
BASE_POSTGRES_PORT=5176
BASE_REDIS_PORT=5177
BASE_MINIO_PORT=5178
BASE_OPENSEARCH_PORT=5180

FRESH_NAME=""
PORT_OFFSET=""
USER_EMAIL="admin@example.com"
DATA_DIR="${RAG_EVAL_DATA_DIR:-/mnt/nas/opentranscribe-benchmarks}"
NO_WAIT=false
SETTLE_TIMEOUT=1800
DRY_RUN=false
DISPATCH_MODE="celery"
PASSTHROUGH=()

usage() {
    sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fresh)          FRESH_NAME="$2"; shift 2 ;;
        --port-offset)    PORT_OFFSET="$2"; shift 2 ;;
        --user)           USER_EMAIL="$2"; shift 2 ;;
        --data-dir)       DATA_DIR="$2"; shift 2 ;;
        --no-wait)        NO_WAIT=true; shift ;;
        --skip-settle)    NO_WAIT=true; shift ;;
        --settle-timeout) SETTLE_TIMEOUT="$2"; shift 2 ;;
        --dry-run)        DRY_RUN=true; PASSTHROUGH+=("$1"); shift ;;
        --dispatch)       DISPATCH_MODE="$2"; PASSTHROUGH+=("$1" "$2"); shift 2 ;;
        -h|--help)        usage 0 ;;
        *)                PASSTHROUGH+=("$1"); shift ;;
    esac
done

# A --fresh stack records its offset when opentr.sh creates it; reading it back
# is what keeps this script from needing the number typed correctly twice.
if [[ -n "$FRESH_NAME" && -z "$PORT_OFFSET" ]]; then
    OFFSET_FILE="${REPO_ROOT}/.fresh/${FRESH_NAME}.offset"
    if [[ -f "$OFFSET_FILE" ]]; then
        PORT_OFFSET="$(tr -d '[:space:]' < "$OFFSET_FILE")"
    else
        echo "No offset recorded for fresh deployment '${FRESH_NAME}' (${OFFSET_FILE})." >&2
        echo "Pass --port-offset N explicitly, or check './opentr.sh fresh-list'." >&2
        exit 2
    fi
fi
PORT_OFFSET="${PORT_OFFSET:-0}"

if ! [[ "$PORT_OFFSET" =~ ^[0-9]+$ ]]; then
    echo "--port-offset must be a non-negative integer, got '${PORT_OFFSET}'." >&2
    exit 2
fi

export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export OPENSEARCH_HOST="${OPENSEARCH_HOST:-localhost}"
export MINIO_HOST="${MINIO_HOST:-localhost}"
export REDIS_HOST="${REDIS_HOST:-localhost}"
export POSTGRES_PORT=$((BASE_POSTGRES_PORT + PORT_OFFSET))
export REDIS_PORT=$((BASE_REDIS_PORT + PORT_OFFSET))
export MINIO_PORT=$((BASE_MINIO_PORT + PORT_OFFSET))
export OPENSEARCH_PORT=$((BASE_OPENSEARCH_PORT + PORT_OFFSET))

# A git worktree has no backend/venv of its own (it is gitignored and does not
# come along with the checkout), so walk up to find the main repo's.
VENV_PY="${OPENTRANSCRIBE_VENV_PYTHON:-}"
if [[ -z "$VENV_PY" ]]; then
    search="$REPO_ROOT"
    while [[ "$search" != "/" ]]; do
        if [[ -x "${search}/backend/venv/bin/python" ]]; then
            VENV_PY="${search}/backend/venv/bin/python"
            break
        fi
        search="$(dirname "$search")"
    done
fi
if [[ -z "$VENV_PY" ]]; then
    echo "No backend venv found. See backend/CLAUDE.md for the two-step install." >&2
    exit 2
fi

echo "Target stack : postgres :${POSTGRES_PORT}  opensearch :${OPENSEARCH_PORT}  redis :${REDIS_PORT}"
echo "Corpus data  : ${DATA_DIR}"
echo "Owner        : ${USER_EMAIL}"
echo

cd "${REPO_ROOT}/backend"

# Not `exec`: dispatching the indexing task is not the same as it succeeding
# (see the header comment), so this script needs to run something AFTER the
# injector to verify the corpus actually settled — which `exec` (replacing this
# process outright) would make impossible.
INJECT_LOG="$(mktemp "${TMPDIR:-/tmp}/inject-eval-corpus.XXXXXX.log")"
trap 'rm -f "$INJECT_LOG"' EXIT

set +e
PYTHONPATH="${REPO_ROOT}/backend" "$VENV_PY" -m app.scripts.corpus_injection \
    --user "$USER_EMAIL" \
    --data-dir "$DATA_DIR" \
    "${PASSTHROUGH[@]}" 2>&1 | tee "$INJECT_LOG"
INJECT_EXIT=${PIPESTATUS[0]}
set -e

if [[ "$INJECT_EXIT" -ne 0 ]]; then
    echo "Injection failed (exit ${INJECT_EXIT}) — not attempting a settle check." >&2
    exit "$INJECT_EXIT"
fi

if $DRY_RUN; then
    echo "Dry run — nothing was written, nothing to settle."
    exit 0
fi

if [[ "$DISPATCH_MODE" == "none" ]]; then
    echo "--dispatch none: rows written but nothing dispatched — nothing to settle."
    exit 0
fi

if $NO_WAIT; then
    echo "⚠️  --no-wait/--skip-settle: indexing was dispatched but NOT verified. The" >&2
    echo "   corpus may not actually be searchable yet — this is dispatch-only." >&2
    exit 0
fi

# The injector logs exactly where it wrote the manifest ("Manifest: <path>"); read
# that back rather than re-deriving the default `.rag-403/injections/<corpus>...`
# path ourselves, which would be a second copy of a formula that already lives in
# app/scripts/corpus_injection/__main__.py and could drift from it.
MANIFEST_PATH="$(grep -oE 'Manifest: .*/manifest\.json$' "$INJECT_LOG" | tail -1 | sed 's/^Manifest: //')"
if [[ -z "$MANIFEST_PATH" ]]; then
    echo "Injector exited 0 but logged no 'Manifest: ...' line — cannot locate what to" >&2
    echo "verify. Treating this as a failure rather than silently reporting success." >&2
    exit 1
fi
MANIFEST_DIR="$(dirname "$MANIFEST_PATH")"

echo
echo "Verifying the corpus actually settled in OpenSearch (timeout ${SETTLE_TIMEOUT}s)..."
"$VENV_PY" "${SCRIPT_DIR}/verify-eval-corpus-settled.py" \
    --manifest-dir "$MANIFEST_DIR" \
    --timeout "$SETTLE_TIMEOUT"
