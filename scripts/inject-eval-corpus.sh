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
PASSTHROUGH=()

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fresh)        FRESH_NAME="$2"; shift 2 ;;
        --port-offset)  PORT_OFFSET="$2"; shift 2 ;;
        --user)         USER_EMAIL="$2"; shift 2 ;;
        --data-dir)     DATA_DIR="$2"; shift 2 ;;
        -h|--help)      usage 0 ;;
        *)              PASSTHROUGH+=("$1"); shift ;;
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
PYTHONPATH="${REPO_ROOT}/backend" exec "$VENV_PY" -m app.scripts.corpus_injection \
    --user "$USER_EMAIL" \
    --data-dir "$DATA_DIR" \
    "${PASSTHROUGH[@]}"
