#!/bin/bash
# Seed the #1337 synthetic dataset (Postgres only) into an ISOLATED stack.
#
#   ./scripts/seed-query-review.sh --fresh perf1337                      # full default shape
#   ./scripts/seed-query-review.sh --fresh perf1337 --files 1000 --users 50
#
# Resolves the --fresh stack's offset Postgres port the way inject-eval-corpus.sh does, then runs
# `python -m app.scripts.scale_dataset`, which refuses the dev stack's ports (5176) and any target
# that is not declared --fresh/--throwaway-db. Plan: docs/design/1337_query_review_plan.md 4.2.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BASE_POSTGRES_PORT=5176

FRESH_NAME=""
PORT_OFFSET=""
PASSTHROUGH=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fresh)       FRESH_NAME="$2"; shift 2 ;;
        --port-offset) PORT_OFFSET="$2"; shift 2 ;;
        -h|--help)     sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)             PASSTHROUGH+=("$1"); shift ;;
    esac
done

if [[ -z "$FRESH_NAME" ]]; then
    echo "--fresh NAME is required: this tool only seeds an isolated stack." >&2
    exit 2
fi
if [[ -z "$PORT_OFFSET" ]]; then
    OFFSET_FILE="${REPO_ROOT}/.fresh/${FRESH_NAME}.offset"
    if [[ ! -f "$OFFSET_FILE" ]]; then
        echo "No offset recorded for '${FRESH_NAME}' (${OFFSET_FILE}); pass --port-offset N." >&2
        exit 2
    fi
    PORT_OFFSET="$(tr -d '[:space:]' < "$OFFSET_FILE")"
fi
if ! [[ "$PORT_OFFSET" =~ ^[0-9]+$ ]]; then
    echo "--port-offset must be a non-negative integer." >&2
    exit 2
fi

export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT=$((BASE_POSTGRES_PORT + PORT_OFFSET))

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
[[ -n "$VENV_PY" ]] || { echo "No backend venv found (backend/CLAUDE.md)." >&2; exit 2; }

cd "${REPO_ROOT}/backend"
PYTHONPATH="${REPO_ROOT}/backend" exec "$VENV_PY" -m app.scripts.scale_dataset \
    --fresh "$FRESH_NAME" "${PASSTHROUGH[@]}"
