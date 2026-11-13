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
$LOCUST -f scripts/perf/load/locustfile.py --list      # import check: lists the personas
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

Load the statement statistics `run_profile.sh` reads. Nothing in a compose file preloads them
(production must not), so, once per stack and before seeding, run the `ALTER SYSTEM SET
shared_preload_libraries = pg_stat_statements, auto_explain;` block of
`docs-site/docs/developer-guide/performance-testing.md` section 1 (unquoted list), restart with
`./opentr.sh stop --fresh perf1337` then the same start command, and confirm
`show shared_preload_libraries`. `run_profile.sh` refuses to run without it and creates the extension.
The setting lives in the data volume's `postgresql.auto.conf`, so a `pg_restore` of a snapshot keeps it.

Seed it with the flags the personas need (cached waveforms are off in the query review's
default shape), snapshot, and restore the snapshot before each run so every run starts from
identical data (`docs-site/docs/developer-guide/performance-testing.md`, section 2), then
`ANALYZE`:

```bash
./scripts/seed-query-review.sh --fresh perf1337 --waveform-pm 1000
# a quick shape for smoke runs:
./scripts/seed-query-review.sh --fresh perf1337 --waveform-pm 1000 --files 2000 --users 100 --segments-per-file 20
```

To add the activity rows to a database that already holds the seed (a restored snapshot), use
`--activity-only`; it leaves every existing row alone and realigns the id sequences. To build a new
snapshot with its sha256: `scripts/perf/load/tools/build_snapshot.sh perf1337 ~/seed.dump`.

Identities: `perf-1` super admin (the audit-log page is super-admin only), `perf-2` heavy owner (20,000 files), `perf-3` the first
organization's admin, then `perf-4` .. `perf-<users>` split into blocks so personas never share a
person: viewers 55%, editors 15%, chat users 10%, socket listeners 10%, pipeline uploaders 10%.
Password `loadtest-password`. Pass the seed's `--users` to Locust as `-- --seeded-users N` when
it is not 1,000.

A run that hits 429s is measuring the rate limiter; the scenario reports those as
`rate limited (429)` failures so they are never mistaken for slowness. Chat turns are limited to
20 a minute **per client address** in the code (not configurable), so the chat users are a fixed
count (`--chat-users`, default 8, 1 in smoke) rather than a share of the crowd.

### Seeded files have no media objects (quiet the background repair first)

The seed writes rows only; no MinIO object exists behind a seeded file. Background tasks that
touch media (embedding repair, thumbnails, redaction backfill) therefore fail on those rows
forever and can make the stack noisy enough to void a run. They are not skipped by a marker in
the code (a seeded row is real data to every one of those tasks), so before a measured run:

```bash
c() { docker ps -q --filter label=com.docker.compose.project=otfresh-NAME --filter label=com.docker.compose.service=$1; }
# the consistency check and search maintenance skip while any reindex_lock:* key exists
docker exec "$(c redis)" sh -c 'redis-cli ${REDIS_PASSWORD:+-a "$REDIS_PASSWORD"} --no-auth-warning SET reindex_lock:loadtest-suppress 1 EX 43200'
```

then wait for the boot-time thumbnail backfill to finish (the cpu worker's CPU falls below
about 150%) and record `uptime` before each run. Fresh stacks only: never set that key on a live one.

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

## Who the people are (the realistic mix)

`personas_roles.py` models people by who is at the keyboard. The `knee`, `waves`, `egress`,
`leak` and `soak-*` profiles run only this mix. The weights are **reasoned assumptions, not
measurements** (there is no production telemetry to copy); change them in the class `weight`,
in review, when real usage says otherwise.

| Persona | Weight | Share | What they do, and why that share |
|---|---:|---:|---|
| `Researcher` | 40 | 44% | Searches (keyword 2 in 3, hybrid 1 in 3, with suggestions and counts), opens transcripts, finds files by phrase, browses a collection; a few chat over a collection. Searching and reading is the product, so they are the largest group. |
| `WatcherBot` | 18 | 20% | A background tab holding the `/api/ws` socket. Most open tabs have nobody looking at them. |
| `StatusPoller` | 12 | 13% | A tab left on the upload page: polls active tasks and file status every 5 to 10 s, the way the SPA does while its socket is down. |
| `TeamLead` | 12 | 13% | Gallery and collections, shares, summaries, exports; occasionally curates a collection. A few people per team. |
| `SpeakerCurator` | 8 | 9% | Works the speaker inbox: clusters, suggestions, identity history; names and merges speakers. The rarest role and the only heavy writer on speakers. |

Rules that keep it honest:

- **Phrasebook.** Search terms and chat questions come from `phrasebook.py`: words and two-word
  windows from the transcript pages the person has opened, plus speaker, tag and collection
  names. They are never random strings, and the choice is among phrases already seen. An empty
  phrasebook (the very first search) falls back to three common words.
- **Chat is budgeted.** Chat turns are limited to 20 a minute per client address, so only the
  first `--chat-users` researchers (default 8) chat. They scope a chat to a whole collection 7
  times in 10, else to a few files.
- **Cross-tenant canary.** Researchers, curators and team leads also ask for a file another
  identity listed and must be refused (403 or 404). A 200 shows as a failure of
  `files.detail cross-tenant canary`, which `thresholds.toml` holds to zero hits.
- **Sharing is read-only.** Team leads read a collection's shares but do not create them: a
  share needs a second real user and would write to someone else's account.
- Writes undo themselves in the same task, like every other persona (see Rules).

## Load shapes

Locust runs one `LoadTestShape` per run. `ProfileShape` in `locustfile.py` is that class; it asks
the `--load-profile` chosen in `profiles.py` for the user count, spawn rate and persona classes
on every tick, so each shape is a profile, and `profiles.py` stays importable without Locust for
the unit tests (Locust docs, "Custom load shapes").

| Profile | Shape | Personas | Defaults (override with `-- --target-users N --ramp-rate R --hold S --steps a,b,c`) |
|---|---|---|---|
| `smoke` | a few people, one minute | web | 10 users |
| `steady` | held | web | 100 users, 900 s |
| `ramp` | step up, hold each | web | 100, 250, 500, 1000; 600 s each |
| `spike` | 0 to N in ~10 s, hold, back to 0 | web | 500 users, 120 s |
| `soak` | moderate, 90 min | web | 200 users |
| `pipeline` | real uploads through the GPU workers | uploader | 4 users, 30 min |
| `knee` | small even steps to find the saturation point | people | 10, 25, 50, 100, 150, 200, 300, 400, 500; 180 s each |
| `waves` | one compressed business day: morning ramp, lunch dip, second peak, evening tail | people | 200 users at the peak; `--hold` is the day's length, 1800 s |
| `herd` | the login herd: N people sign in within about 5 s, then keep signing in for a minute | login herd | 300 users |
| `hot-file` | everyone on one file: many sessions of one account read and annotate its first file | hot file reader | 200 users, 300 s |
| `egress` | many people behind one client address (every simulated user already shares yours) | people | 500 users, 600 s |
| `leak` | the 20-minute leak run: memory and connection growth | people | 100 users, 1200 s |
| `soak-8h`, `soak-24h` | the long soaks | people | 200 users; `--hold` is the duration (8 h, 24 h) |

- **`egress` is only meaningful with the per-IP limits left at the stack's defaults.** Do not
  export the `DEV_RATE_LIMIT_*` variables from "Prepare the stack" for it: the `rate limited
  (429)` rows in `locust_failures.csv` are the result, and the question is whether sign-in,
  search and chat limits protect the server without locking out ordinary users.
- **`herd` and `hot-file`** need the raised sign-in limit, and `hot-file` pins the heavy owner
  (`perf-2`) and takes the oldest file of its first listing, so every session lands on the same
  rows. Its comment add/delete is where a deadlock between writers would show.
- A `waves` or `soak-*` run's length is a parameter, never a different shape.

### The long soaks are ready but not routinely run

`soak-8h` and `soak-24h` exist and are tested for their schedule, but they are **not part of any
routine or release run**; the fast, short profiles (`smoke`, `knee`, `leak`) are the default.
Prove the wiring with a short override instead of waiting hours:

```bash
scripts/perf/load/run.sh --fresh perf1337 --profile soak-8h -- --target-users 10 --ramp-rate 10 --hold 120
```

That is a two-minute run of the 8 h shape (the same users, personas and verdict). Fault
injection, the multi-GPU `--gpu-devices` overlay and Kubernetes are follow-ups, not built.

## Watch it: the exporters

Start the fresh stack with the monitoring overlays to see Postgres, Redis and every container
while the run goes:

```bash
COMPOSE_PROFILES=load-monitoring ./opentr.sh start dev --fresh perf1337 --port-offset 300 --with-mock-llm --with-monitoring
```

The `load-monitoring` profile of `docker-compose.monitoring.yml` adds the maintained exporters, pinned by tag and digest:
[postgres_exporter](https://github.com/prometheus-community/postgres_exporter) v0.20.1,
[redis_exporter](https://github.com/oliver006/redis_exporter) v1.97.0 and
[cAdvisor](https://github.com/google/cadvisor/blob/master/docs/running.md) v0.60.6. They publish
no host port; the `--with-monitoring` Prometheus scrapes them by service name
(`monitoring/prometheus/prometheus.yml`), and `--fresh` isolates them with the rest of the
project. The samples in `samples.csv` stay the pass/fail source; the exporters are for
looking around (Prometheus on the stack's `PROMETHEUS_PORT`). A load dashboard and OpenSearch
k-NN stats are not built yet.

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
| `samples.csv` | every 5 s: `pg_stat_activity` by state, connections idle in a transaction for over 1 s, the oldest transaction, the API's own connections against its pool capacity (`pgapi`, from Postgres), `db_pool_*` and `http_requests_in_flight` (from the API, so missing when it is saturated), every Celery queue's depth, CPU and memory of each container of this stack (by compose project label), memory and utilisation of the sampled GPUs |
| `summary.md` | the report: verdict, per-endpoint table with limits, failures by cause, percentile history, sampler min/mean/max, pool saturation, memory growth |
| `report.html`, `locust.log`, `sampler.log` | Locust's own report and both logs |

The limits are in `thresholds.toml` and are the SLOs of issue #1174: page reads p95 300 ms,
keyword search 800 ms, hybrid search 1.5 s, writes 500 ms, chat first token 5 s, errors 0.1%,
no deadlock (`pg_stat_database.deadlocks`, last sample minus first), no cross-tenant canary
hit (and the canary must have run), no connection idle in a transaction for over a second, the
pool saturated in at most 5% of samples, at least 80% of sampler sweeps producing data, memory
growth under 25% across a soak. Change them there, in review.

## Reading the result

- A p95 that climbs with users while Postgres CPU stays low, and the API's `pgapi` connections sit
  at capacity, is pool or threadpool saturation, not slow SQL: see the plan's section on pool and
  threadpool sizing before tuning anything.
- A climbing p95 with the backend container at 100% of one core is one API process saturated;
  `UVICORN_WORKERS` (default 2) adds processes. **Measure the process model that ships:** a plain
  `--fresh` dev stack runs one `--reload` process. Use `./opentr.sh start dev --fresh NAME
  --no-bindmount`, which starts the backend through the image's `scripts/api-start.sh` with
  `UVICORN_WORKERS` processes (default 2; export `UVICORN_WORKERS=1` for the A side of an A/B), and
  confirm `ps` in the container shows the supervisor plus N workers. The API's connections are
  `application_name = 'opentranscribe-api'` in `pg_stat_activity`.
- A slow `WS echo round trip` with fast HTTP reads points at the event loop, not the database.
- In a soak, container memory that keeps rising, or connections that keep growing in
  `pg_stat_activity`, is a leak; the summary flags memory growth.
- Compare runs only on the same snapshot, settings and host load, as the query review does.

## Side tools (`tools/`)

For a repeatable recorded pass (no host paths; each takes the `--fresh` name and an output
directory):

| Tool | What |
|---|---|
| `chain.sh FRESH OUT "NAME\|probe_s\|run.sh args"...` | runs profiles back to back with the probe beside each, `uptime` before and after |
| `run_profile.sh FRESH OUT NAME args...` | one run, plus `pg_stat_statements` top 40 and the backend log of the run window |
| `pg_probe.sh FRESH OUT.csv SECONDS` | idle-in-transaction and long-active rows with the client address |
| `report_table.py RUN...` | one markdown block per run (`BACKEND_IP=<api ip>` attributes the probe rows to the API) |
| `ramp_stages.py RUN` | per held user level: req/s, error share, p50/p95 |
| `access_stats.py RUN` | server-side time per route from the backend's access log |
| `build_snapshot.sh FRESH OUT.dump [--from-snapshot OLD.dump]` | seed or upgrade, `ANALYZE`, `pg_dump`, sha256 |
