#!/bin/bash
# Build the load test's seed snapshot on an isolated --fresh stack and record its sha256.
#
#   build_snapshot.sh FRESH_NAME OUT_DUMP [--from-snapshot OLD_DUMP]
#
# From empty (the default): seed the whole dataset (core + governance + activity + waveforms),
# ANALYZE, dump. From an older snapshot (the M1 dump, which predates activity rows, correct
# sequences and the super-admin perf-1): restore it, add only the activity rows
# (`--activity-only`, which also realigns every sequence), make perf-1 a super admin, ANALYZE, dump.
# The dump is `pg_dump -Fc` of the stack's database; OUT_DUMP.sha256 is written beside it.
#
# Needs the stack up: ./opentr.sh start dev --fresh FRESH_NAME --port-offset N  (see README).
# Never run it against anything that is not a --fresh stack: seed-query-review.sh refuses.
set -euo pipefail
FRESH="${1:?fresh deployment name}"; OUT="${2:?output dump path}"; shift 2
OLD=""
if [[ "${1:-}" == "--from-snapshot" ]]; then OLD="${2:?old dump path}"; fi
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
PG=$(docker ps -q --filter "label=com.docker.compose.project=otfresh-${FRESH}" \
        --filter label=com.docker.compose.service=postgres)
[[ -n "$PG" ]] || { echo "build_snapshot: stack otfresh-${FRESH} is not up" >&2; exit 3; }
BE=$(docker ps -q --filter "label=com.docker.compose.project=otfresh-${FRESH}" \
        --filter label=com.docker.compose.service=backend)
[[ -n "$BE" ]] || { echo "build_snapshot: backend of otfresh-${FRESH} is not up" >&2; exit 3; }
psql_in() { docker exec -i "$PG" sh -c 'psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-opentranscribe}" "$@"' sh "$@"; }

# pg_restore --clean swaps in the snapshot's (older) schema, so bring the database to head with the
# repo's one schema authority, the migrate step (`python -m app.startup.migrate`, which also runs the
# post-upgrade work a bare `alembic upgrade head` skips; backend/app/db/CLAUDE.md), in the stack's own
# backend container, then refuse to go on unless alembic_version is the head of this checkout.
migrate_to_head() {
  docker exec "$BE" python -m app.startup.migrate
  local want got
  want=$(python3 "$REPO/scripts/release-tests/lib/alembic-head.py" "$REPO/backend")
  got=$(psql_in -Atc 'SELECT version_num FROM alembic_version' | tr -d '[:space:]')
  [[ -n "$want" && "$got" == "$want" ]] || {
    echo "build_snapshot: schema is at '${got}', head is '${want}'; refusing to seed" >&2; exit 4; }
}

SEED_FLAGS=(--waveform-pm 1000)
if [[ -n "$OLD" ]]; then
  (cd "$(dirname "$OLD")" && sha256sum -c "$(basename "$OLD").sha256")
  docker exec -i "$PG" sh -c 'pg_restore -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-opentranscribe}" --clean --if-exists' < "$OLD"
  migrate_to_head
  "$REPO/scripts/seed-query-review.sh" --fresh "$FRESH" --activity-only "${SEED_FLAGS[@]}"
  psql_in -c "UPDATE \"user\" SET role='super_admin', is_superuser=true WHERE email='perf-1@loadtest.invalid'"
else
  migrate_to_head
  "$REPO/scripts/seed-query-review.sh" --fresh "$FRESH" "${SEED_FLAGS[@]}"
fi
psql_in -c ANALYZE
docker exec "$PG" sh -c 'pg_dump -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-opentranscribe}" -Fc' > "$OUT"
(cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" && cat "$(basename "$OUT").sha256")
