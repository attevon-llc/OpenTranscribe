#!/bin/bash
# One recorded load run: reset pg_stat_statements, run scripts/perf/load/run.sh into
# OUT_ROOT/NAME, then keep the top statements and the backend log of the run window beside it.
#
# Usage: run_profile.sh FRESH_NAME OUT_ROOT NAME run.sh-args...
# Environment: LOCUST (path to the locust binary, optional). The rate limits the dev stack
# applies are raised for the run only (see docs-site/docs/developer-guide/performance-testing.md).
set -uo pipefail
FRESH="${1:?fresh deployment name}"; OUT_ROOT="${2:?runs directory}"; NAME="${3:?run name}"; shift 3
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
OUT="${OUT_ROOT}/${NAME}"
export DEV_RATE_LIMIT_AUTH_PER_MINUTE=100000 DEV_RATE_LIMIT_DIRECTORY_PER_MINUTE=100000 \
       DEV_RATE_LIMIT_AUTH_METHODS_PER_MINUTE=100000 DEV_RATE_LIMIT_SEARCH_PER_MINUTE=100000
c() { docker ps -q --filter "label=com.docker.compose.project=otfresh-${FRESH}" \
        --filter "label=com.docker.compose.service=$1"; }
PG=$(c postgres); B=$(c backend)
[[ -n "$PG" && -n "$B" ]] || { echo "run_profile: stack otfresh-${FRESH} is not up" >&2; exit 3; }
PSQL='psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-opentranscribe}"'
docker exec "$PG" sh -c "$PSQL -Atc 'select pg_stat_statements_reset()'" >/dev/null
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
mkdir -p "$OUT"
"${REPO}/scripts/perf/load/run.sh" --fresh "$FRESH" --out "$OUT" "$@" > "$OUT/console.log" 2>&1
RC=$?
echo "run.sh rc=$RC" >> "$OUT/console.log"
docker exec "$PG" sh -c "$PSQL --csv -c \"select calls, round(total_exec_time::numeric,1) total_ms, round(mean_exec_time::numeric,2) mean_ms, round(max_exec_time::numeric,1) max_ms, rows, shared_blks_hit, shared_blks_read, left(regexp_replace(query, '\\s+', ' ', 'g'), 400) query from pg_stat_statements order by total_exec_time desc limit 40\"" > "$OUT/pgss.csv"
docker logs --since "$START" "$B" > "$OUT/backend.log" 2>&1
gzip -f "$OUT/backend.log"
echo "$NAME rc=$RC"
exit "$RC"
