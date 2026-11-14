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
source "${REPO}/scripts/lib/stack-target.sh"
st_target --fresh "$FRESH"
# schema-head+migrate runs `python -m app.startup.migrate` in the stack's backend when the schema is
# behind (the repo's one schema authority; backend/app/db/CLAUDE.md), then re-checks it is at head.
# pg_stat_statements is prepared BEFORE seeding, so the snapshot's volume carries it.
st_require healthy:postgres,backend schema-head+migrate ext:pg_stat_statements+prepare
PG=$(st_container postgres)
psql_in() { docker exec -i "$PG" sh -c 'psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-opentranscribe}" "$@"' sh "$@"; }

SEED_FLAGS=(--waveform-pm 1000)
if [[ -n "$OLD" ]]; then
  (cd "$(dirname "$OLD")" && sha256sum -c "$(basename "$OLD").sha256")
  # App writers (the API and every Celery worker/beat) stop first: a live write between the
  # restore's DROP and CREATE statements is what failed LOADRUN5 attempt 1 on a foreign key.
  # --single-transaction makes the restore all-or-nothing (PostgreSQL docs, pg_restore: it cannot be
  # combined with -j, which this does not use). Only the backend comes back up, because
  # schema-head+migrate below runs the migration inside it; the workers return after the dump.
  mapfile -t WRITERS < <(docker ps --filter "label=com.docker.compose.project=${ST_PROJECT:?}" \
    --filter "label=com.docker.compose.service" --format '{{.ID}} {{.Label "com.docker.compose.service"}}' \
    | awk '$2=="backend" || $2 ~ /^celery-/ {print $1}')
  [[ ${#WRITERS[@]} -eq 0 ]] || docker stop "${WRITERS[@]}" >/dev/null
  trap '[[ ${#WRITERS[@]} -eq 0 ]] || docker start "${WRITERS[@]}" >/dev/null' EXIT
  docker exec -i "$PG" sh -c 'pg_restore -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-opentranscribe}" --clean --if-exists --single-transaction' < "$OLD"
  BACKEND_ID=$(st_container backend)
  docker start "$BACKEND_ID" >/dev/null
  # pg_restore --clean swaps in the snapshot's older schema: bring it back to head before seeding.
  st_require schema-head+migrate
  "$REPO/scripts/seed-query-review.sh" --fresh "$FRESH" --activity-only "${SEED_FLAGS[@]}"
  psql_in -c "UPDATE \"user\" SET role='super_admin', is_superuser=true WHERE email='perf-1@loadtest.invalid'"
else
  "$REPO/scripts/seed-query-review.sh" --fresh "$FRESH" "${SEED_FLAGS[@]}"
fi
# A migrated install has every one-time pass flagged done; seeded rows have no media objects, so an
# unflagged snapshot makes the stack re-run the imohash/thumbnail/normalization passes during the load
# run (loadrun2 R1: 34k NoSuchKey failures, Redis 77% CPU). Keys are the code's own constants:
# tasks/imohash_recompute.py RECOMPUTE_FLAG_KEY, core/constants.py THUMBNAIL_*_DONE_KEY,
# tasks/speaker_embedding_migration.py NORMALIZATION_DONE_KEY.
psql_in -c "INSERT INTO system_settings (key, value, description) VALUES
  ('imohash_package_recompute_complete','true','load seed: one-time pass marked done'),
  ('thumbnail_webp_migration_done','true','load seed: one-time pass marked done'),
  ('thumbnail_backfill_done','true','load seed: one-time pass marked done'),
  ('embedding_normalization_done','true','load seed: one-time pass marked done')
  ON CONFLICT (key) DO UPDATE SET value='true'"
psql_in -c ANALYZE
docker exec "$PG" sh -c 'pg_dump -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-opentranscribe}" -Fc' > "$OUT"
(cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" && cat "$(basename "$OUT").sha256")
