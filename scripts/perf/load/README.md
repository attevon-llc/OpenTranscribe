# Concurrent-user load test

Simulates many signed-in people using OpenTranscribe at once and records what the web tier,
the database pool and Postgres do under that load. It complements the single-request query
review in `../query-review/` (issue #1337): that tool measures what ONE request costs at data
scale; this one measures what HUNDREDS of concurrent requests do to each other.

Background and the audit this test checks: `docs/design/concurrency_foundations_plan.md`.
Method page: `docs-site/docs/developer-guide/performance-testing.md` ("Concurrent users").

## Rules

- **Local, isolated `--fresh` stacks only.** `locustfile.py` refuses anything else: it needs
  `--fresh NAME`, takes the port from `.fresh/NAME.offset`, and refuses offset 0 and the shared
  dev stack's ports. There is no override and no remote mode.
- **Seed first** with `scripts/seed-query-review.sh --fresh NAME` (the same seed the query
  review uses). Identities: `perf-1` admin, `perf-2` heavy owner (20,000 files), `perf-3` ..
  `perf-<users>` ordinary users, password `loadtest-password`.
- **Quiet host.** Check `uptime` and `nvidia-smi` first; a concurrent build or test suite makes
  every number meaningless. Never run two load tests at once.
- Writes clean up after themselves (a comment is created then deleted; an upload is prepared,
  deleted and purged, never completed, so no transcription is dispatched to the GPU workers).
  Run with `--exclude-tags write` for a read-only run.

## Install (once, in its own venv)

Locust patches the standard library with gevent when it is imported, so it lives in its own
venv, never in `backend/venv`:

```bash
python3 -m venv ~/ot-cleanup-work/concurrency/locust-venv
~/ot-cleanup-work/concurrency/locust-venv/bin/pip install -r scripts/perf/load/requirements.txt
LOCUST=~/ot-cleanup-work/concurrency/locust-venv/bin/locust
$LOCUST -f scripts/perf/load/locustfile.py --list      # import check: lists the 3 user classes
```

## Prepare the stack

The development overlay's per-IP limits suit a few people, not hundreds of simulated users
sharing one client IP. Raise the ones a load test trips **when starting the fresh stack** (the
shell environment overrides `.env` for compose):

```bash
export DEV_RATE_LIMIT_AUTH_PER_MINUTE=100000          # every simulated user logs in once
export DEV_RATE_LIMIT_DIRECTORY_PER_MINUTE=100000      # GET /files/owners, per IP
export DEV_RATE_LIMIT_AUTH_METHODS_PER_MINUTE=100000
export DEV_RATE_LIMIT_SEARCH_PER_MINUTE=100000         # per user; think time keeps it low anyway
./opentr.sh start dev --fresh perf1337 --port-offset 300
```

Record what you changed in the run's `env.txt`. A run that hits 429s is measuring the rate
limiter; the scenario reports those as `rate limited (429)` failures so they are never mistaken
for slowness. Each simulated identity holds one session; the default per-user session cap is 5,
so keep `--users` at or below 5 x the seeded user count.

Restore the seed snapshot before each run so every run starts from identical data
(`docs-site/docs/developer-guide/performance-testing.md`, section 2), then `ANALYZE`.

## Run: 100, 500 and 1,000 users

Ramp slowly: every login is a deliberately expensive password hash (PBKDF2, hundreds of
milliseconds of CPU), so a fast spawn measures the login storm instead of steady state.

```bash
RUN=~/ot-cleanup-work/concurrency/runs/$(date +%Y%m%d-%H%M)-u100 && mkdir -p "$RUN"
$LOCUST -f scripts/perf/load/locustfile.py --fresh perf1337 --headless \
    --users 100  --spawn-rate 2 --run-time 15m --csv "$RUN/locust" --html "$RUN/report.html" \
    --csv-full-history 2>&1 | tee "$RUN/locust.log"
# 500 users:   --users 500  --spawn-rate 2 --run-time 20m
# 1,000 users: --users 1000 --spawn-rate 2 --run-time 25m   (seed --users 1000 or more)
```

One Locust process drives roughly a few hundred users before its own CPU becomes the limit.
If `locust.log` warns that the CPU is above 90%, rerun with `--processes 4` (identities are
then assigned per process, so a few repeat; the session cap allows it).

## What to record (per run, into `$RUN`)

| What | How |
|---|---|
| p50 / p95 / p99 and request count per endpoint | `locust_stats.csv` (columns `50%`, `95%`, `99%`); one row per route label |
| Error rate, by cause | `locust_failures.csv` (`rate limited (429)`, `session lost (401)`, `HTTP 5xx`, `no response`) |
| Latency over time | `locust_stats_history.csv` (shows when the curve bends) |
| Postgres connections in use | sample every 5 s: `docker exec "$PG" psql -U postgres -d opentranscribe -Atc "select now(), state, count(*) from pg_stat_activity where datname = current_database() group by 2"` |
| Long transactions / idle in transaction | same loop: `select count(*) from pg_stat_activity where state = 'idle in transaction' and now() - xact_start > interval '1 second'` |
| API pool use and waits | sample `curl -s localhost:<backend port>/metrics \| grep -E '^db_pool_'` every 5 s (`db_pool_checked_out` at `db_pool_capacity` means requests are queueing for a connection); after the run, `docker logs "$BE" 2>&1 | grep -c 'QueuePool limit'` (each one is a request that waited the full 30 s pool timeout and failed) |
| In-flight requests | `http_requests_in_flight` from the same `/metrics` sample |
| CPU and memory per container | `docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' $(docker ps -q --filter label=com.docker.compose.project=otfresh-perf1337)` every 5 s |
| Host load | `uptime` before and after |

`$PG` and `$BE` are the stack's Postgres and backend containers, found by compose project
label, never by a name grep:

```bash
P=label=com.docker.compose.project=otfresh-perf1337
PG=$(docker ps -q --filter "$P" --filter label=com.docker.compose.service=postgres)
BE=$(docker ps -q --filter "$P" --filter label=com.docker.compose.service=backend)
``` Stop the sampling loops when Locust exits.

## Reading the result

- A p95 that climbs with users while Postgres CPU stays low, and `db_pool_checked_out` sits at
  capacity, is pool or threadpool saturation, not slow SQL: see the plan's section on pool and
  threadpool sizing before tuning anything.
- A climbing p95 with the backend container at 100% of one core is the single API process
  (the plan's top recommendation).
- Compare runs only on the same snapshot, settings and host load, as the query review does.
