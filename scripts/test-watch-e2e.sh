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

# `grep -c ... -eq 0`, never `! ... | grep -q`. This script runs under `set -euo pipefail`;
# `grep -q` exits at its first match, `docker ps` can then take SIGPIPE (141), and `pipefail`
# turns that MATCH into a non-match — so a running stack reads as absent and this refuses to
# run. `grep -c` reads the whole stream, so there is no early exit to race.
if [ "$(docker ps --format '{{.Names}}' | grep -c "^${CONTAINER}$")" -eq 0 ]; then
  echo "ERROR: container '${CONTAINER}' is not running. Start the stack with:"
  echo "  ./opentr.sh start dev --with-watch"
  exit 1
fi

exec docker exec -w /app -e PYTHONPATH=/app "${CONTAINER}" python scripts/e2e_watch_sources.py
