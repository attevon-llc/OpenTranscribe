#!/bin/bash
# Side probe: every 5 s, record the pg_stat_activity rows that are idle in a transaction for over
# 1 s or active for over 2 s, with the client address, so the sampler's counts can be attributed
# to the API (its container IP) or to a worker. Read-only.
#
# Usage: pg_probe.sh FRESH_NAME OUT_CSV DURATION_S
set -euo pipefail
FRESH="${1:?fresh deployment name}"; OUT="${2:?output csv}"; DUR="${3:?duration in seconds}"
PG=$(docker ps -q --filter "label=com.docker.compose.project=otfresh-${FRESH}" \
        --filter label=com.docker.compose.service=postgres)
[[ -n "$PG" ]] || { echo "pg_probe: no postgres container in otfresh-${FRESH}" >&2; exit 2; }
END=$(( $(date +%s) + DUR ))
echo "ts|pid|client|state|xact_s|state_s|wait|query" > "$OUT"
SQL="select extract(epoch from now())::int, pid, client_addr, state,
  round(extract(epoch from now()-xact_start)::numeric,1),
  round(extract(epoch from now()-state_change)::numeric,1),
  coalesce(wait_event_type||':'||wait_event,''),
  left(regexp_replace(query,'\s+',' ','g'),300)
  from pg_stat_activity where datname=current_database() and pid<>pg_backend_pid()
  and ((state like 'idle in transaction%' and now()-xact_start > interval '1 second')
    or (state='active' and now()-query_start > interval '2 seconds'))"
while [[ "$(date +%s)" -lt "$END" ]]; do
  docker exec "$PG" sh -c 'psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-opentranscribe}" -At -c "$0"' \
      "$SQL" >> "$OUT" 2>/dev/null || true
  sleep 5
done
