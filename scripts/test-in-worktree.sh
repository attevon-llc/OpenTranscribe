#!/bin/bash
# Run backend pytest from ANY git worktree without the dev stack.
#
#   scripts/test-in-worktree.sh [--services pg] [--keep] -- <pytest args>
#
# conftest defaults POSTGRES_PORT to 5176 (the dev stack, which runs the MAIN checkout's
# schema), so tests from a worktree error at setup when that stack is down and silently test
# against the wrong schema when it is up. This starts a throwaway postgres on a free loopback
# port, migrates it with THIS worktree's alembic, runs pytest against it, and tears down on
# every exit path. See backend/tests/CLAUDE.md "Running DB-backed tests without the dev stack".
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && git rev-parse --show-toplevel)" || {
    echo "error: not inside a git worktree" >&2; exit 2; }

# shellcheck source=lib/stock-port-owners.sh
source "$REPO_ROOT/scripts/lib/stock-port-owners.sh"
# shellcheck source=lib/throwaway-pg.sh
source "$REPO_ROOT/scripts/lib/throwaway-pg.sh"

SERVICES="pg"
KEEP=0
while (( $# )); do
    case "$1" in
        --services) SERVICES="${2:-}"; shift 2 || { echo "--services needs a value" >&2; exit 2; } ;;
        --keep) KEEP=1; shift ;;
        --) shift; break ;;
        -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1 (pytest args go after --)" >&2; exit 2 ;;
    esac
done

# Only postgres is provisioned: redis is already a per-worker throwaway fixture, and S3/OpenSearch
# tests need the real stack (`./opentr.sh start dev --fresh <name>`), so they are skipped here.
IFS=',' read -r -a _svcs <<< "$SERVICES"
for s in "${_svcs[@]}"; do
    case "$s" in
        pg|postgres) ;;
        redis|minio|opensearch)
            echo "error: --services ${s} is not provisioned by this script; use a --fresh stack (./opentr.sh start dev --fresh <name>) for it" >&2
            exit 2 ;;
        *) echo "error: unknown service '${s}'" >&2; exit 2 ;;
    esac
done

command -v docker >/dev/null 2>&1 || { echo "error: docker not found on PATH" >&2; exit 3; }

# Worktree venv first, else the main checkout's (the first entry of `git worktree list`).
PY=""
MAIN_ROOT="$(dirname "$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir)")"
for cand in "$REPO_ROOT/backend/venv/bin/python" "$MAIN_ROOT/backend/venv/bin/python"; do
    [[ -x "$cand" ]] && { PY="$cand"; break; }
done
[[ -n "$PY" ]] || {
    echo "error: no backend venv at $REPO_ROOT/backend/venv or $MAIN_ROOT/backend/venv (see root CLAUDE.md 'Backend / venv'; this script never installs)" >&2
    exit 3; }

WORK_DIR="$(mktemp -d -t ot-wt-test.XXXXXX)" || exit 3

cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    if (( KEEP )); then
        echo "--keep: left container '${TPG_NAME}' and ${WORK_DIR}. Remove with:" >&2
        echo "  docker rm -f ${TPG_NAME}; rm -rf ${WORK_DIR}" >&2
    else
        tpg_remove
        rm -rf "$WORK_DIR"
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

tpg_start || exit 3
tpg_wait_ready || exit 3

mkdir -p "$WORK_DIR"/{data,models,temp}
export DATA_DIR="$WORK_DIR/data" MODELS_DIR="$WORK_DIR/models" TEMP_DIR="$WORK_DIR/temp"
export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT="$TPG_PORT" POSTGRES_USER="$TPG_USER" \
       POSTGRES_PASSWORD="$TPG_PASSWORD" POSTGRES_DB="$TPG_DB"
export SKIP_S3=True SKIP_OPENSEARCH=True
export DATABASE_URL="postgresql://${TPG_USER}:${TPG_PASSWORD}@127.0.0.1:${TPG_PORT}/${TPG_DB}"
export MIGRATION_DATABASE_URL="$DATABASE_URL"

echo ">> ${TPG_NAME} on 127.0.0.1:${TPG_PORT}; venv ${PY}" >&2
cd "$REPO_ROOT/backend" || exit 3
nice -n 10 "$PY" -m alembic upgrade head || { echo "error: alembic upgrade head failed" >&2; exit 4; }
nice -n 10 "$PY" -m pytest "$@"
