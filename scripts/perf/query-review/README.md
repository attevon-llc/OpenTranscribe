# Query review run checklist (issue #1337)

Run checklist for `docs/design/1337_query_review_plan.md` section 4; the plan holds the
reasoning. All artifacts go to `~/ot-cleanup-work/1337/perf/<run-id>/`, never `/tmp`.

1. Quiet host, nobody editing the checkout (a `.py` edit under `backend/app/` hot-reloads the
   stack). Record `uptime`.
2. Isolated stack, never the dev stack:
   `./opentr.sh start dev --fresh perf1337 --port-offset 300`, then plan 4.1 step 4
   (`pg_stat_statements`, `auto_explain`, `track_io_timing`), restart, `CREATE EXTENSION`,
   and check `pg_settings`.
3. Seed (Postgres only) and snapshot:
   `./scripts/seed-query-review.sh --fresh perf1337` (flags: `--files`, `--users`,
   `--speakers-per-file`, `--segments-per-file`, `--seed`; defaults are the plan's shape),
   then `pg_dump -Fc` and record its sha256.
4. Reset counters: `SELECT pg_stat_statements_reset(); SELECT pg_stat_reset();`
5. Drive the endpoints (refuses the dev stack's ports):
   `scripts/perf/query-review/drive.py run --check-routes --base-url http://localhost:<backend port>`
   then `drive.py run --base-url ... --out <run dir> --repeat 20`.
   Save the backend log to `<run dir>/access.log` and
   `drive.py summarise --calls <run dir>/calls.csv --access-log <run dir>/access.log > <run dir>/driver.csv`.
6. Export `pg_stat_statements` and `pg_stat_user_tables` (plan 4.3 step 4).
7. Replay and time every query:
   `PG=<fresh postgres container> scripts/perf/query-review/explain.sh <run-id> [id ...]`.
   `C1`-`C3` are the controls; if one moves more than 15% between two runs the pair is noise.
8. For a before/after, restore the same dump, `ANALYZE`, change one thing, repeat 4-7.

`queries/<id>.sql` ids match plan section 3; `<id>.after.sql` is the rewritten form. Present:
C1-C3, G7, G8, G13 (+after), T1, T3, P3, K4, V13, A2, S1, T4, V7, P8, P9. The rest are added during the baseline from
`auto_explain.log` literals (plan 4.3 step 5). V13 and A2 filter on `completed_at`, which the
seed leaves NULL: set realistic values inside the same rolled-back transaction first.
