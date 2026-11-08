---
sidebar_position: 5
---

# Scale and query performance testing

How to find out whether a database query is slow at scale, prove a fix made it faster, and keep it
fast. Everything here runs on a local, isolated `--fresh` stack seeded with synthetic data; it never
touches the shared dev stack or real data.

The method was built for the query review (issue #1337). The tools live in
`scripts/perf/query-review/`, `scripts/seed-query-review.sh`, `backend/app/scripts/scale_dataset/`
and `backend/tests/query_budget.py`.

## The method, in order

1. **Measure first (M1).** Seed a large synthetic dataset, drive the real endpoints, rank the
   statements, then replay the top ones under `EXPLAIN (ANALYZE, BUFFERS)`. A hypothesis only counts
   once a measurement confirms it; a guessed index is not a fix.
2. **Fix one thing.** A query rewrite, a loader change, or (only when the plan shows a scan the
   rewrite cannot remove) an index.
3. **Measure again (M2)** on the same snapshot with the same settings, and compare.
4. **Lock it in** with a regression check that fails if the problem comes back.

## 1. An isolated stack

```bash
./opentr.sh fresh-list                                   # pick an unused port offset
./opentr.sh start dev --fresh perf1337 --port-offset 300
```

Never run this against the dev stack: the seed tool and the driver refuse its ports. Check the host
is quiet first (`uptime`); concurrent builds or test suites make timings meaningless.

Enable the statistics extensions on this stack only. The library list is written **unquoted**; a
quoted `'a,b'` is stored as one library name and Postgres then fails to start.

```sql
ALTER SYSTEM SET shared_preload_libraries = pg_stat_statements, auto_explain;
ALTER SYSTEM SET pg_stat_statements.track = 'top';
ALTER SYSTEM SET track_io_timing = on;
ALTER SYSTEM SET auto_explain.log_min_duration = '100ms';
ALTER SYSTEM SET auto_explain.log_analyze = on;
ALTER SYSTEM SET auto_explain.log_format = 'json';
```

Restart with `./opentr.sh stop --fresh perf1337` and `./opentr.sh start dev --fresh perf1337`, then
`CREATE EXTENSION IF NOT EXISTS pg_stat_statements;` and confirm both libraries in `pg_settings`.
The full list of settings is in `docs/design/1337_query_review_plan.md` section 4.1.

## 2. Seed and snapshot

```bash
./scripts/seed-query-review.sh --fresh perf1337                        # the default shape
./scripts/seed-query-review.sh --fresh perf1337 --files 1000 --users 50 --segments-per-file 10
```

The default shape is 100,000 files, 1,000,000 speakers and 10,000,000 segments (about 30 minutes;
the segments are 80% of that, so `--segments-per-file` is the lever for a quick run). Data is derived
deterministically, so two seeds are identical. Identities: `perf-1@loadtest.invalid` (admin),
`perf-2@...` (heavy owner, 20,000 files), `perf-3@...` (ordinary user), password `loadtest-password`.

Snapshot straight after seeding so every comparison starts from identical data:

```bash
docker exec "$PG" pg_dump -U postgres -Fc -d opentranscribe > ~/ot-cleanup-work/1337/perf/seed-perf1337.dump
sha256sum ~/ot-cleanup-work/1337/perf/seed-perf1337.dump
# restore for a run, then ANALYZE:
docker exec -i "$PG" pg_restore -U postgres -d opentranscribe --clean --if-exists < ~/ot-cleanup-work/1337/perf/seed-perf1337.dump
```

(`PG` is the stack's Postgres container, found by compose project label, never by a name grep.)

## 3. Drive the endpoints

```bash
scripts/perf/query-review/drive.py run --check-routes --base-url http://localhost:5474
scripts/perf/query-review/drive.py run --base-url http://localhost:5474 --out <run dir> --repeat 20
scripts/perf/query-review/drive.py summarise --calls <run dir>/calls.csv \
    --access-log <run dir>/access.log > <run dir>/driver.csv
```

`calls.csv` is written row by row, a client timeout is recorded as status 599, and an endpoint slower
than `--slow-ms` is capped at `--slow-repeat` calls. The summary pairs each call with its own
access-log line, so the per-request statement count (`dbq`) is attributed per identity and per
variant. A statement count that grows with the number of rows is an N+1.

## 4. Replay and compare queries

Each `scripts/perf/query-review/queries/<id>.sql` is one hot statement; `<id>.after.sql` is its
rewritten form. `C1` to `C3` are controls that no change should affect.

```bash
PG=<fresh postgres container> scripts/perf/query-review/explain.sh <run-id> [id ...]
```

It runs one discarded warm-up and five measured executions inside `BEGIN ... ROLLBACK`, and writes
the median, buffer counts and plan node types. A query that needs data the seed leaves empty gets a
`queries/setup/<id>.sql`, run first in the same rolled-back transaction.

## 5. Compare before and after

- Same dump, `ANALYZE`d, same settings. Any settings difference voids the pair.
- Run the controls in both runs. If a control moves by more than 15% the pair is noise; re-run on a
  quiet host.
- For a rewrite, run `<id>.sql` and `<id>.after.sql` back to back on the same snapshot and compare
  results as well as time.
- For an index, measure with it absent, build it the way production does, `ANALYZE`, measure again,
  and check the plan uses it.
- Record the table (median, statement count, shared hits) with the change.

A fix is warranted when the median for a page read is above 100 ms at seed scale, or the statement
count grows with rows.

## 6. Regression checks

Statement counts that must not grow with rows use `backend/tests/query_budget.py`:

```python
from tests.query_budget import count_statements

with count_statements(db_session.connection()) as small:
    client.get("/api/tasks?page_size=100", headers=headers)
# seed more rows, repeat as `large`
assert large.count == small.count, large.describe()
```

Assert equality between two sizes (for example 2 and 12 rows), not a magic ceiling, and always add a
control that the response held both sizes' rows so an empty result cannot pass. For a rewrite,
compile the statement and assert plan node types (`Anti Join` present, no `SubPlan`), never costs.

## Adding a new hot path

1. Add the suspect endpoint to `USER_CALLS` or `ADMIN_ONLY` in `drive.py`.
2. Add its statement as `queries/<id>.sql` (and `<id>.after.sql` for the fix). The test
   `backend/tests/unit/test_query_review_sql_files.py` plans every file against the current schema.
3. Measure, fix, measure again as above.
4. Add a `count_statements` or plan-shape test beside the endpoint's other tests, and prove it red on
   a `git archive HEAD` tree before relying on it.
