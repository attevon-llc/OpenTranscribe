#!/bin/bash
# One recorded load run: reset pg_stat_statements, run scripts/perf/load/run.sh into
# OUT_ROOT/NAME, then keep the top statements and the backend log of the run window beside it.
#
# Usage: run_profile.sh FRESH_NAME OUT_ROOT NAME run.sh-args...
# Environment: LOCUST (path to the locust binary, optional). The stack must be the one run.sh
# asserts (`start dev --fresh NAME --no-bindmount --build` with the raised rate limits exported;
# docs-site/docs/developer-guide/performance-testing.md). pg_stat_statements is prepared here
# on first use (fresh stacks only; scripts/lib/stack_target.py prepare).
set -euo pipefail
FRESH="${1:?fresh deployment name}"; OUT_ROOT="${2:?runs directory}"; NAME="${3:?run name}"; shift 3
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
OUT="${OUT_ROOT}/${NAME}"
source "${REPO}/scripts/lib/stack-target.sh"
st_target --fresh "$FRESH"
# ext:...+prepare loads the library (ALTER SYSTEM + restart, once per stack), creates the extension
# and verifies it; the restart is why this runs before run.sh's own checks.
st_require healthy:postgres,backend ext:pg_stat_statements+prepare
PG=$(st_container postgres); B=$(st_container backend)
PSQL='psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-opentranscribe}"'
docker exec "$PG" sh -c "$PSQL -Atc 'select pg_stat_statements_reset()'" >/dev/null
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
mkdir -p "$OUT"
RC=0
"${REPO}/scripts/perf/load/run.sh" --fresh "$FRESH" --out "$OUT" "$@" > "$OUT/console.log" 2>&1 || RC=$?
echo "run.sh rc=$RC" >> "$OUT/console.log"
docker exec "$PG" sh -c "$PSQL --csv -c \"select calls, round(total_exec_time::numeric,1) total_ms, round(mean_exec_time::numeric,2) mean_ms, round(max_exec_time::numeric,1) max_ms, rows, shared_blks_hit, shared_blks_read, queryid, regexp_replace(query, '\\s+', ' ', 'g') query from pg_stat_statements order by total_exec_time desc limit 40\"" > "$OUT/pgss.csv"
docker logs --since "$START" "$B" > "$OUT/backend.log" 2>&1
gzip -f "$OUT/backend.log"
# Every service's log of the run window (tools/capture_logs.sh): a stall with nothing in the API
# log is attributed from the workers'.
source "$(dirname "${BASH_SOURCE[0]}")/capture_logs.sh"
capture_service_logs "$ST_PROJECT" "$OUT" "$START"
echo "$NAME rc=$RC"
exit "$RC"
