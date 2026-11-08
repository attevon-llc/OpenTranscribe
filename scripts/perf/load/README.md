# Concurrent-user load test

Simulates many signed-in people using OpenTranscribe at once and records what the web tier,
the database pool, Postgres, the queues and the GPU workers do under that load. It complements
the single-request query review in `../query-review/` (issue #1337): that tool measures what ONE
request costs at data scale; this one measures what HUNDREDS of concurrent requests do to each
other.

Background and the audit this test checks: `docs/design/concurrency_foundations_plan.md`.
Method page: `docs-site/docs/developer-guide/performance-testing.md` ("Concurrent users").

## Rules

- **Local, isolated `--fresh` stacks only.** `locustfile.py`, `sampler.py` and `run.sh` refuse
  anything else: they need `--fresh NAME`, take the port from `.fresh/NAME.offset`, and refuse
  offset 0 and the shared dev stack's ports. There is no override and no remote mode.
- **Quiet host.** Check `uptime` and `nvidia-smi` first; a concurrent build or test suite makes
  every number meaningless. Never run two load tests at once.
- **GPUs.** The `pipeline` profile (and `--mix mixed-gpu`) sends real recordings to the fresh
  stack's transcription worker, which runs on GPU 1. GPU 2 may be used too when `nvidia-smi`
  shows it idle. **Never GPU 0**: the sampler refuses it.
- **Writes clean up after themselves**, and touch only the signed-in person's own files: a
  comment is created, edited and deleted; a tag is attached and deleted; a collection is created,
  filled, emptied and deleted; a segment is edited and restored; a temporary speaker is created,
  given a segment, renamed, handed the segment back and merged away; a file is hidden and shown
  again; a chat and the mock-LLM configurations are deleted; an upload is deleted and purged once
  the pipeline finishes it. `reprocess` re-runs only the analytics stage.

## Install (once, in its own venv)

Locust patches the standard library with gevent when it is imported, so it lives in its own
venv, never in `backend/venv`:

```bash
python3 -m venv ~/ot-cleanup-work/concurrency/locust-venv
~/ot-cleanup-work/concurrency/locust-venv/bin/pip install -r scripts/perf/load/requirements.txt
export LOCUST=~/ot-cleanup-work/concurrency/locust-venv/bin/locust
$LOCUST -f scripts/perf/load/locustfile.py --list      # import check: lists the 8 personas
```

`sampler.py` and `summarize.py` need only the system `python3` (3.11 or newer).

## Prepare the stack

The development overlay's per-IP limits suit a few people, not hundreds of simulated users
sharing one client IP. Raise the ones a load test trips **when starting the fresh stack** (the
shell environment overrides `.env` for compose), and start the mock LLM for the chat users:

```bash
export DEV_RATE_LIMIT_AUTH_PER_MINUTE=100000          # every simulated user logs in once
export DEV_RATE_LIMIT_DIRECTORY_PER_MINUTE=100000      # GET /files/owners, per IP
export DEV_RATE_LIMIT_AUTH_METHODS_PER_MINUTE=100000
export DEV_RATE_LIMIT_SEARCH_PER_MINUTE=100000         # per user; think time keeps it low anyway
./opentr.sh start dev --fresh perf1337 --port-offset 300 --with-mock-llm
```

Seed it with the flags the personas need (cached waveforms are off in the query review's
default shape), snapshot, and restore the snapshot before each run so every run starts from
identical data (`docs-site/docs/developer-guide/performance-testing.md`, section 2), then
`ANALYZE`:

```bash
./scripts/seed-query-review.sh --fresh perf1337 --waveform-pm 1000
# a quick shape for smoke runs:
./scripts/seed-query-review.sh --fresh perf1337 --waveform-pm 1000 --files 2000 --users 100 --segments-per-file 20
```

Identities: `perf-1` admin, `perf-2` heavy owner (20,000 files), `perf-3` the first
organization's admin, then `perf-4` .. `perf-<users>` split into blocks so personas never share a
person: viewers 55%, editors 15%, chat users 10%, socket listeners 10%, pipeline uploaders 10%.
Password `loadtest-password`. Pass the seed's `--users` to Locust as `-- --seeded-users N` when
it is not 1,000.

A run that hits 429s is measuring the rate limiter; the scenario reports those as
`rate limited (429)` failures so they are never mistaken for slowness. Chat turns are limited to
20 a minute **per client address** in the code (not configurable), so the chat users are a fixed
count (`--chat-users`, default 8, 1 in smoke) rather than a share of the crowd.

## Personas and what they cover

| Persona | Who | Tasks |
|---|---|---|
| `Viewer` (55%) | ordinary people | gallery and filters (search, tag, speaker, sort, legal hold, hidden, shared), collections, trash, a file's page (detail, transcript pages, comments, speakers, tags, collections, analytics, summary, suggestions, waveform, media presign), subtitles, exports and captions, search with suggestions and counts, speakers/profiles/clusters, tasks, settings pages |
| `HeavyOwner` (1) | perf-2 | the viewer's pages over the largest library |
| `Editor` (10%) | ordinary people | comments, tags, collections, segment text, speaker create/rename/reassign/merge, hide/unhide, analytics reprocess, an abandoned upload; with `gpu`, a real recording uploaded and followed to "completed" |
| `ChatUser` (fixed) | ordinary people | chat history; chat turns against `mock-gpt` and long streams against `mock-slow` (time to first token and to the end of the answer) |
| `Administrator` (1) | perf-1 | statistics, users, audit log, quarantine, legal holds, takedowns, stuck files, index health, every settings read |
| `OrgAdmin` (1) | perf-3 | org audit log, holds, quarantine, policy, erasures; only where the edition switches those pages on (the community edition does not, and it stops) |
| `WebSocketListener` (25%) | ordinary people | holds a `/api/ws` socket, pings it every 20-40 s (echo round trip) and counts notifications |
| `PipelineUploader` | pipeline block | `pipeline` profile only: upload, wait for "completed", purge, repeat |

`routes.py` maps every OpenAPI tag group to the personas that call it (`GROUP_PERSONAS`) or
gives the reason it is left out (`EXCLUDED_GROUPS`: health probes, SCIM provisioning, one-off
migrations, watch sources, directory sync, storage audit, support-access grants).
`backend/tests/unit/test_load_scenario_routes.py` derives the same map from a recorded run and
fails when they differ, when a new group appears, or when a request is not in `openapi.json`.

## Run: one command per profile

Each command creates a run directory (printed first; `--out DIR` to choose it), starts the
sampler, runs Locust, stops the sampler and writes the summary. `--mix` picks the task set:
`mixed` (default: reads and writes, no recordings to the GPU), `read-only`
(`--exclude-tags write gpu`), or `mixed-gpu` (editors also upload real recordings).

```bash
scripts/perf/load/run.sh --fresh perf1337 --profile smoke                       # 10 people, 1 minute
scripts/perf/load/run.sh --fresh perf1337 --profile steady -- --target-users 300 --hold 900
scripts/perf/load/run.sh --fresh perf1337 --profile ramp                        # 100, 250, 500, 1000, 10 min each
scripts/perf/load/run.sh --fresh perf1337 --profile spike -- --target-users 500 # 0 -> 500 in ~10 s -> 0
scripts/perf/load/run.sh --fresh perf1337 --profile soak                        # 200 people, 90 minutes
scripts/perf/load/run.sh --fresh perf1337 --profile pipeline --gpus 1 -- --target-users 4
scripts/perf/load/run.sh --fresh perf1337 --profile ramp --mix read-only        # any profile, reads only
```

Profile options (after `--`): `--target-users N`, `--ramp-rate R` (users started per second),
`--hold S` (seconds per stage), `--steps 100,250,500,1000`, `--chat-users N`,
`--seeded-users N`, `--pipeline-timeout S`. Use `--gpus 1,2` for the pipeline profile only when
GPU 2 is idle and the stack runs a worker on it.

Ramp slowly: every login is a deliberately expensive password hash (PBKDF2, hundreds of
milliseconds of CPU), so a fast spawn measures the login storm instead of steady state (the
spike profile does this on purpose). One Locust process drives a few hundred users before its
own CPU becomes the limit; if `locust.log` warns that the CPU is above 90%, add
`-- --processes 4` (identities are then assigned per process, so a few repeat).

The sampler and the summary can also be run by hand:

```bash
python3 scripts/perf/load/sampler.py --fresh perf1337 --out "$RUN" --gpus 1      # Ctrl-C to stop
python3 scripts/perf/load/summarize.py "$RUN"                                     # exit 1 = a limit broken
```

## What a run directory holds

| File | What |
|---|---|
| `env.txt` | date, commit, profile, mix, raised `DEV_*` limits, `uptime` |
| `locust_stats.csv` | per endpoint (route name + label): count, failures, p50 / p95 / p99. Streams are `SSE` rows (`mock-gpt first token`, `... whole answer`), the socket `WS` rows (`connect`, `echo round trip`, `notification <type>`), the pipeline a `PIPELINE upload->completed` row |
| `locust_failures.csv` | failures by cause: `rate limited (429)`, `session lost (401)`, `HTTP <code>`, `no response` |
| `locust_stats_history.csv` | percentiles over time (`--csv-full-history`) |
| `samples.csv` | every 5 s: `pg_stat_activity` by state, connections idle in a transaction for over 1 s, the oldest transaction, `db_pool_*` and `http_requests_in_flight`, every Celery queue's depth, CPU and memory of each container of this stack (by compose project label), memory and utilisation of the sampled GPUs |
| `summary.md` | the report: verdict, per-endpoint table with limits, failures by cause, percentile history, sampler min/mean/max, pool saturation, memory growth |
| `report.html`, `locust.log`, `sampler.log` | Locust's own report and both logs |

The limits are in `thresholds.toml` (p95 under 1 s for reads, error rate under 0.5%, no
connection idle in a transaction for over a second, the pool saturated in at most 5% of
samples, memory growth under 25% across a soak). Change them there, in review.

## Reading the result

- A p95 that climbs with users while Postgres CPU stays low, and `db_pool_checked_out` sits at
  capacity, is pool or threadpool saturation, not slow SQL: see the plan's section on pool and
  threadpool sizing before tuning anything.
- A climbing p95 with the backend container at 100% of one core is the single API process
  (the plan's top recommendation).
- A slow `WS echo round trip` with fast HTTP reads points at the event loop, not the database.
- In a soak, container memory that keeps rising, or connections that keep growing in
  `pg_stat_activity`, is a leak; the summary flags memory growth.
- Compare runs only on the same snapshot, settings and host load, as the query review does.
