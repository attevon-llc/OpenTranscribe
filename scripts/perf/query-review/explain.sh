#!/bin/bash
# Replay queries/<id>.sql under EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON) against the
# isolated perf Postgres container: one discarded warm-up and five measured executions per id,
# each in its own BEGIN ... ROLLBACK (EXPLAIN ANALYZE really executes; nothing is kept).
#
#   PG=otfresh-perf1337-postgres-1 ./scripts/perf/query-review/explain.sh <run-id> [id ...]
#
# Output: ~/ot-cleanup-work/1337/perf/<run-id>/explain/<id>.json (the last measured plan) and
# explain/results.tsv: id, median Execution Time ms, shared hit, shared read, top plan node types.
# Plan: docs/design/1337_query_review_plan.md section 4.3 step 5.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ID="${1:?usage: PG=container explain.sh run-id [id ...]}"
shift || true
PG="${PG:?set PG to the fresh postgres container name}"
PG_USER="${PG_USER:-postgres}"
PG_DB="${PG_DB:-opentranscribe}"
OUT="${HOME}/ot-cleanup-work/1337/perf/${RUN_ID}/explain"

# Only ever an isolated deployment: a bare name grep once destroyed an unrelated container.
PROJECT="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$PG" 2>/dev/null || true)"
if [[ "$PROJECT" != otfresh-* ]]; then
    echo "Refusing '${PG}': compose project '${PROJECT}' is not an otfresh-* (--fresh) deployment." >&2
    exit 3
fi

mkdir -p "$OUT"
if [[ $# -gt 0 ]]; then IDS=("$@"); else
    IDS=()
    for f in "${SCRIPT_DIR}"/queries/*.sql; do
        base="$(basename "$f" .sql)"
        IDS+=("$base")
    done
fi

printf 'id\tmedian_ms\tshared_hit\tshared_read\tnodes\n' > "${OUT}/results.tsv"
for id in "${IDS[@]}"; do
    sql_file="${SCRIPT_DIR}/queries/${id}.sql"
    [[ -f "$sql_file" ]] || { echo "no such query: ${id}" >&2; exit 2; }
    body="$(sed 's/--.*$//' "$sql_file" | tr '\n' ' ' | sed 's/;[[:space:]]*$//')"
    for i in 0 1 2 3 4 5; do   # run 0 is the discarded warm-up
        printf 'BEGIN;\nEXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON) %s;\nROLLBACK;\n' "$body" \
            | docker exec -i "$PG" psql -X -At -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 \
            | grep -v -e '^BEGIN$' -e '^ROLLBACK$' > "${OUT}/${id}.run${i}.json"
    done
    python3 -I - "$OUT" "$id" >> "${OUT}/results.tsv" <<'PY'
import json, statistics, sys
out, qid = sys.argv[1:3]
times, last = [], None
for i in range(1, 6):
    last = json.load(open(f"{out}/{qid}.run{i}.json"))[0]
    times.append(last["Execution Time"])
def walk(node, acc):
    acc.append(node["Node Type"])
    for child in node.get("Plans", []):
        walk(child, acc)
nodes = []
walk(last["Plan"], nodes)
plan = last["Plan"]
json.dump(last, open(f"{out}/{qid}.json", "w"), indent=1)
print("\t".join([qid, f"{statistics.median(times):.2f}", str(plan.get("Shared Hit Blocks", 0)),
                 str(plan.get("Shared Read Blocks", 0)), ",".join(dict.fromkeys(nodes))]))
PY
    rm -f "${OUT}/${id}".run*.json
done
cat "${OUT}/results.tsv"
