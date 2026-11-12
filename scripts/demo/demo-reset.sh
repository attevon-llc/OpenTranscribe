#!/usr/bin/env bash
# demo-reset.sh — put the public demo back to its seeded state (issue #628, decision D6).
#
# Runs nightly at 04:00 UTC (scripts/demo/systemd/opentranscribe-demo-reset.timer) and at
# the end of every upgrade (demo-upgrade.sh). Only `down -v` resets Postgres, MinIO,
# OpenSearch and Redis together; restoring one store alone leaves search results pointing at
# rows that no longer exist.
#
#   1. stop nginx: the public port closes and stays closed until step 5 (the fence);
#   2. down -v, scoped to this install's compose project;
#   3. seed phase: start everything except nginx with one of each worker
#      (DEMO_WORKER_REPLICAS=1) and run the seeder with DEMO_MODE=false in a one-off
#      container; it waits for the neural model, the index and the redaction status;
#   4. demo phase: `up -d` again with the overlay's defaults (no worker, no beat);
#   5. prove the demo backend is inert (python -m app.scripts.assert_inert: exit 0 only when
#      DEMO_MODE publish refusal is probed and the route model was measured; issue #898);
#   6. start nginx.
# Any failure exits non-zero with nginx still stopped: a half-seeded demo is never public.
#
# Usage: scripts/demo/demo-reset.sh [--dry-run]
# Environment:
#   DEMO_INSTALL_DIR  install directory (default: cwd)
#   DEMO_SEED_BUNDLE  host path of the seed bundle directory (required)
#   DEMO_UP_TIMEOUT   seconds for each `up --wait`, default 900
# Exit: 0 reset, 1 failed (nginx stopped), 2 misuse, 3 another reset/upgrade holds the lock.

set -euo pipefail

DRY_RUN=0
case "${1:-}" in
    --dry-run) DRY_RUN=1 ;;
    "") ;;
    -h | --help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

INSTALL_DIR="${DEMO_INSTALL_DIR:-$PWD}"
UP_TIMEOUT="${DEMO_UP_TIMEOUT:-900}"
SEED_BUNDLE="${DEMO_SEED_BUNDLE:-}"

cd "$INSTALL_DIR"
[ -x ./opentranscribe.sh ] || { echo "not an install directory: $INSTALL_DIR" >&2; exit 2; }
if [ -z "$SEED_BUNDLE" ] || [ ! -f "$SEED_BUNDLE/manifest.json" ]; then
    echo "DEMO_SEED_BUNDLE must name a seed bundle directory (with manifest.json)" >&2
    exit 2
fi

run() {
    echo "+ $*"
    [ "$DRY_RUN" = 1 ] || "$@"
}

# The upgrade script already holds the lock when it calls us; inherit it then.
if [ -z "${DEMO_LOCK_HELD:-}" ]; then
    exec 9>"$INSTALL_DIR/.demo.lock"
    flock -n 9 || { echo "another demo reset or upgrade is running" >&2; exit 3; }
fi

read -r -a FILES <<<"$(./opentranscribe.sh compose-files)"
case " ${FILES[*]} " in
    *" docker-compose.demo.yml "*) ;;
    *) echo "the demo overlay is not in the compose chain (is DEMO_MODE=true in .env?)" >&2; exit 2 ;;
esac

# The API reports the upgrade time from this file (docker-compose.demo.yml mounts it); it must
# exist before the first start or Docker creates a directory in its place.
[ "$DRY_RUN" = 1 ] || [ -e "$INSTALL_DIR/demo-status.json" ] || printf '{}' >"$INSTALL_DIR/demo-status.json"

run docker compose "${FILES[@]}" stop nginx
run docker compose "${FILES[@]}" down -v --remove-orphans

DEMO_WORKER_REPLICAS=1 run docker compose "${FILES[@]}" up -d --wait \
    --wait-timeout "$UP_TIMEOUT" --scale nginx=0
run docker compose "${FILES[@]}" run --rm -e DEMO_MODE=false \
    -v "$SEED_BUNDLE:/seed:ro" backend python -m app.scripts.demo_seed import --bundle /seed

run docker compose "${FILES[@]}" up -d --wait --wait-timeout "$UP_TIMEOUT" --scale nginx=0
# set -e: a non-zero exit (not inert, or NOT MEASURED) stops here with nginx still closed.
run docker compose "${FILES[@]}" exec -T backend python -m app.scripts.assert_inert
run docker compose "${FILES[@]}" up -d --wait --wait-timeout "$UP_TIMEOUT" nginx
echo "demo reset complete"
