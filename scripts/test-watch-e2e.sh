#!/bin/bash
# Repeatable end-to-end test for Watch Sources (issue #26).
#
# Runs the full suite INSIDE the backend container against the live stack
# (Postgres, MinIO, Redis/Celery, ffmpeg, GPU worker). Requires the dev stack
# up with the watch overlay:  ./opentr.sh start dev --with-watch
#
# Usage: ./scripts/test-watch-e2e.sh
set -euo pipefail

CONTAINER="${WATCH_E2E_CONTAINER:-opentranscribe-backend}"

if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  echo "ERROR: container '${CONTAINER}' is not running. Start the stack with:"
  echo "  ./opentr.sh start dev --with-watch"
  exit 1
fi

exec docker exec -w /app -e PYTHONPATH=/app "${CONTAINER}" python scripts/e2e_watch_sources.py
