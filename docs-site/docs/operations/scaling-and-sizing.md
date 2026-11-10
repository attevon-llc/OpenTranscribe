---
sidebar_position: 6
title: Scaling and Sizing
description: How to measure your own deployment's capacity, read the knee, and size the database connection budget, API workers and memory
---

# Scaling and Sizing

This page is the method for answering "how many people can this deployment serve, and what do I
change when it cannot?". It tells you what to measure, how to read the result, and how to size
the database connection budget. The numbers in the tables are measurements from the project's own
host, shown **before** the v0.7.0 scale work; the "after" column is filled in once the final
measurement pass has run.

:::note
Measure on your own hardware. The knee below came from one API process on one 48-core host with a
1,000,000-speaker library. Your numbers move with CPU, library size and the share of people who
write or chat.
:::

## 1. Measure

Use the concurrent-user load test (`scripts/perf/load/`, a [Locust](https://docs.locust.io/en/stable/)
scenario). It runs only against an isolated `--fresh` stack and never against your live data. The
full runbook is [Scale and query performance testing](../developer-guide/performance-testing.md#concurrent-users).

1. Start an isolated stack and seed it (`./scripts/seed-query-review.sh --fresh NAME --waveform-pm 1000`),
   or restore a recorded snapshot and `ANALYZE`.
2. Run `smoke` first. It must pass before any other profile means anything.
3. Run `ramp` (100, 250, 500 and 1,000 people, each held) once with `--mix read-only` and once
   with `--mix mixed`. Add `--mix mixed-gpu` only when a GPU is idle.
4. Run on a quiet host: one minute of load under a quarter of the cores, no builds or other test
   suites. Contention invalidates timings.
5. Read `summary.md` in each run directory. A run **fails** when the sampler produced fewer than
   80% of the expected samples, so a saturated server cannot pass by going silent.

What to record for each run: requests per second, p50 and p95 by group (gallery, file page,
search, auth), the error share by cause, the API's database connections from `pg_stat_activity`,
Postgres and API CPU, and the container's memory.

## 2. Read the knee

Plot requests per second against people. Below the knee, throughput rises with people and p95
stays under a second. At the knee, throughput stops rising and latency climbs. Above it, requests
queue, then fail.

| Signal | What it means |
|---|---|
| p95 climbs, throughput flat, API container at one full core | The single API process is the limit. Add workers. |
| p95 climbs, throughput flat, Postgres CPU low, API connections at the pool capacity | Requests are waiting for a database connection. Do not raise the pool first: shorten what holds a connection, then add workers. |
| Postgres CPU high | Slow queries or missing indexes: use the [query review](../developer-guide/performance-testing.md). |
| `WS echo round trip` slow while HTTP reads are fast | The event loop is busy; same cause as the first row. |
| Errors are HTTP 500 with `QueuePool limit` in the API log | The pool wait timed out. This is saturation, not a bug in one endpoint. |
| Memory rises through a soak | A leak; check per-container memory in `samples.csv`. |

Pool saturation is judged from Postgres (`pg_stat_activity`, connections from the API's container),
not from the API's own `/metrics`: a saturated API cannot reliably serve its own metrics.

### Before v0.7.0 (one API process, pool 20 + 40)

| Load | Result |
|---|---|
| 25 to 50 people | p50 40 to 120 ms, p95 0.3 to 1.2 s |
| 100 people | p50 1.8 to 2.1 s, p95 4.4 to 8 s |
| 250 to 300 people | p50 15 to 23 s, p95 27 to 60 s, 21 to 38% of requests fail |
| 1,000 people | p50 reaches the 60 s client timeout, 66 to 69% fail |
| Throughput | about 14 to 20 requests per second, whatever the number of people |

The knee sat at 50 to 100 people. All 60 pool connections were idle in a transaction while
Postgres used 30 to 68% of one core: the limit was the single process, not the database.

### After v0.7.0

To be filled by the final measurement pass.

## 3. Size the database connection budget

PostgreSQL reserves 3 slots for superusers, so usable connections are `PG_MAX_CONNECTIONS - 3`
(default 200, so 197). Each API process holds up to `DB_POOL_SIZE + DB_MAX_OVERFLOW` connections
plus one for the readiness probe. Celery workers and the scheduler hold about 110 in the default
layout.

```
worst case = UVICORN_WORKERS x (DB_POOL_SIZE + DB_MAX_OVERFLOW + 1) + other services
worst case must stay at or under PG_MAX_CONNECTIONS - 3
```

| `UVICORN_WORKERS` | Per-process pool | API total | Others | Worst case | Fits 197? |
|---|---|---|---|---|---|
| 1 | 20 + 40 | 60 | about 110 | about 170 | yes |
| 2 | 15 + 15 (+1) | 62 | about 110 | about 172 | yes |
| 4 | 10 + 10 (+1) | 84 | about 110 | about 194 | barely; raise `PG_MAX_CONNECTIONS` to 300 |
| 8 | 8 + 8 (+1) | 136 | about 110 | about 246 | no; `PG_MAX_CONNECTIONS` 300 |

Rules of thumb:

- Raise `PG_MAX_CONNECTIONS` **before** raising workers past 2, by about 30 per extra worker at
  10 + 10.
- Keep `work_mem` times connections inside RAM; more connections cost memory in Postgres too.
- A smaller pool per process is right: the measured bottleneck was CPU, with 60 connections held
  while Postgres ran one to five statements at a time.
- A request holds its connection from authentication until it ends. Concurrency above the pool
  waits `DB_POOL_TIMEOUT_SECONDS` and then answers `503` with `Retry-After`.

## 4. Workers, replicas and memory

- **Workers** (`UVICORN_WORKERS`) multiply the CPU ceiling of one API process for the cost of
  RAM: each worker loads the embedding model, the redaction models and the reranker, measured at
  about 1 to 1.8 GB per process. Two workers roughly double the ceiling; four roughly quadruple
  it for about 3 to 5.4 GB extra.
- **Replicas** (more API containers behind a proxy) scale the same way and add failure isolation,
  at the cost of the same per-process memory and the same connection budget arithmetic: count
  every replica's workers in the formula above.
- Prefer workers inside one container on a single host, replicas when you need to survive one
  container failing or to spread across hosts.
- Per-process caches can differ for up to 30 seconds between workers; that is by design.

## 5. Probe settings

Liveness (`/health`) answers without touching a dependency, so it stays fast when every thread
is busy. Readiness (`/health/ready`) reports Postgres, Redis, OpenSearch and MinIO and serves a
short-lived cached result. Orchestrator settings that match this:

| Probe | Path | Period | Timeout | Notes |
|---|---|---|---|---|
| Liveness | `/health` | 10 s | 3 s | restart only when the process cannot answer at all |
| Readiness | `/health/ready` | 5 s | 3 s | removes the instance from rotation, never restarts it |
| Startup | `/health/ready` | 5 s | 3 s | `failureThreshold x period` at least 600 s (first-run migrations and model loads) |

Pool saturation is reported in the readiness checks as `db_pool` and is never fatal: a busy
instance should shed load, not be killed.

## 6. When to add PgBouncer

Add a connection pooler only when the arithmetic in section 3 cannot be satisfied by raising
`PG_MAX_CONNECTIONS` (for example many replicas, or a managed database with a low connection
cap), and use transaction pooling only after checking that nothing the API relies on needs a
session (advisory locks, `SET` for the rest of a session, prepared statements). Until a
measurement shows connection count as the limit, more workers and shorter-held connections come
first.

## Related

- [Performance Tuning](performance-tuning.md)
- [Scale and query performance testing](../developer-guide/performance-testing.md)
