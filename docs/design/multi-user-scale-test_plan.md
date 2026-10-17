# Plan: multi-user scale and load testing

Status: **PLAN ONLY.** Nothing in this document is implemented yet. Written 2026-10-03.

## Goal

Before wider adoption we need evidence that OpenTranscribe stays fast and correct when many people
use one instance at once, on a large library. Without it we cannot tell self-hosters how to size
and tune a deployment for hundreds or thousands of users.

This plan builds three things and then uses them:

1. A **reproducible multi-user load framework**.
2. A **seeded large-dataset builder**.
3. An **observability overlay**.

With them we find where each tier breaks, tune it, and publish a **scaling and sizing guide**.

It is written so that an implementation agent with no other context can execute it. Every new
file has a path, and every phase has exit criteria and verification.

---

## 0. Summary

| Item | Decision |
|---|---|
| Load tool | **Locust** (Python), distributed master/worker, custom load shapes (§3) |
| Bots | Persona-driven simulated users. A deterministic **scripted** mode is used for every baseline. An optional **LLM brain** uses a small local model and pre-generates a seeded *phrasebook* so runs stay replayable (§5.5). |
| Data | A seeded **dataset builder** (`app.scripts.scale_dataset`). It has a fast path (Postgres COPY, OpenSearch bulk with precomputed vectors, object copy/reference, by cloning a *seed library*) and a realistic path (API + GPU pipeline + API history replay). A verifier and an A/B fidelity gate keep them consistent (§7). |
| Seed library | Public, reproducible: core's synthetic meeting generator. Large and realistic, internal only: a private long-form podcast corpus (~1,500 episodes) ingested through the real pipeline on a multi-GPU host. It is never committed or published; only aggregate numbers are. |
| Tiers | S (CI, 10 users), T1 100, T2 1,000, T3 10,000 registered users; peak concurrent bots 5 / 50 / 250 / 1,000; spikes ×3 (§6) |
| Profiles | steady, knee search, workday waves, spike, login herd, hot file, search storm, chat storm, soak 8 h / 24 h |
| SLOs (proposed) | Page reads p95 ≤ 300 ms. Keyword search p95 ≤ 800 ms. Hybrid p95 ≤ 1.5 s. Writes p95 ≤ 500 ms. Chat first token p95 ≤ 5 s. 5xx < 0.1 %. 0 deadlocks. 0 cross-tenant canary hits (§8.4). |
| Phases | P0 tooling → P1 small local → P2 corpus ingest (multi-GPU) → P3 waves/spikes/tuning → P4 docs → P5 distributed/Kubernetes rerun (optional, with the Helm chart #864) → P6 soak + failure injection |
| Effort | ≈ 40 engineer-days plus unattended GPU and soak time; ≈ 5–6 weeks calendar |

---

## 1. Scope

**In scope.** The API tier and its data stores under many concurrent users:

- FastAPI/uvicorn, Postgres, OpenSearch (keyword, semantic and hybrid), Redis, and the interactive
  Celery queues.
- Chat (LLM, SSE), transcript and speaker edits, comments, tags, collections and sharing.
- Speaker clustering and curation, uploads, status polling and WebSocket, and page views.

**Out of scope.**

- GPU transcription throughput tuning (existing GPU benchmarks; this plan only runs GPU load in the
  background).
- ASR/diarization accuracy.
- Any production instance. Tests run only on isolated `--fresh` stacks.

---

## 2. What exists and is reused

| Existing | Where | Used for |
|---|---|---|
| Fresh isolated stacks, port offsets, overlay isolation | `./opentr.sh start dev --fresh <name> --port-offset N` | Every run (never the live data paths) |
| Mock LLM (`mock-gpt`, `mock-slow`, `mock-error`, SSE) | `scripts/mock-llm-server.py`, `--with-mock-llm` | Deterministic chat load |
| Real local LLM (vLLM, OpenAI-compatible) | `--with-llm-test` | Realistic chat latency; bot brain |
| Multi-GPU workers | `--gpu-scale`, `docker-compose.gpu-split.yml` | Corpus ingest (extended to N GPUs, §11 P0) |
| Direct-row corpus injection with deterministic ids and run manifest | `backend/app/scripts/corpus_injection/` | Pattern and code for the builder |
| Seeded synthetic meeting generator (SplitMix64, byte-identical) | `backend/tests/eval/synthetic/` | Public seed library, deterministic RNG |
| E2E/Playwright stack | `backend/tests/e2e/` | Capturing real page call bundles (HAR) |
| Prometheus + Grafana overlay | `docker-compose.monitoring.yml`, `monitoring/` | Extended by the load overlay |
| Metrics | `app/core/metrics.py` (`http_request_duration_seconds`, `http_requests_in_flight`, `db_query_duration_seconds`, `db_queries_per_request`, `cache_operations_total`, `celery_queue_depth`, `celery_queue_reserved`, `celery_queue_oldest_unacked_age_seconds`, `celery_queue_orphaned`); `app/core/stage_timing.py` (`pipeline_stage_duration_seconds`, #1170) | Server-side truth |
| In flight | #1172: `celery_task_queue_wait_seconds`, `celery_queue_oldest_message_age_seconds` | Queue-wait SLOs. **Dependency for P3.** |
| Older benchmark scripts | `scripts/benchmark_concurrent_uploads.py`, `scripts/benchmark_upload_matrix.py`, `backend/scripts/benchmark_queries.py` | Kept; the new framework supersedes them for multi-user work |

---

## 3. Tool choice: Locust

| | Locust | k6 | Gatling | Artillery | Vegeta/wrk2 |
|---|---|---|---|---|---|
| Fit with the codebase | **Python**, same as the backend; reuses SplitMix64, the dataset roster, upload helpers | JS on Go; everything rewritten | Scala/Java | Node | CLI |
| Stateful personas / sessions | User classes, weighted tasks, think times, `on_start` | Good | Good | Fair | none |
| Waves / spikes / soak | `LoadTestShape` (any function of time, seeded); open-loop pacing via `constant_throughput` | Best open-model executors | Good | Phases | Constant rate |
| SSE (chat) | Streaming read + custom events (TTFT, total) | Extension | Plugin | Plugin | – |
| WebSocket | `websocket-client` under gevent | Built in | Built in | Built in | – |
| Distributed | master + workers (ZeroMQ), container-friendly | Operator | Enterprise | Paid | Manual |
| Prometheus | Event hooks → `prometheus_client` | Native | Plugin | Plugin | – |

**Decision:** Locust 2.x with `FastHttpUser`, pinned.

- **Why.** The hard parts are stateful sessions, seeded data rosters, uploads and an optional local
  model, and all of that is Python. Raw rps per core is lower than k6's. That is enough here: 1,000
  bots with 5–30 s think time is 100–400 rps, and spikes are 1–2k rps over a few worker processes.
- **Mitigations:**
  1. Coordinated omission: record scheduled vs actual start, and flag a saturated generator.
  2. Approximate built-in percentiles: our own raw per-request log is the source of truth.
  3. Worker saturation: a CPU guard on the workers.
- **Revisit trigger:** more than 16 worker processes needed at T3 → use k6 for the pure open-model
  segments only.

---

## 4. Files (all new unless noted)

| Piece | Path |
|---|---|
| Load framework | `loadtest/` (top level; own `requirements.txt` pinned; `Dockerfile` multi-stage, slim, non-root; `README.md`) |
| Package | `loadtest/otload/{config,client,recorder,prom,guards,shapes,report,compare,sampler,locustfile,sse,ws}.py` |
| Auth plugins | `loadtest/otload/auth/base.py` (interface: login, headers, refresh, WS auth frame), `loadtest/otload/auth/local.py` (local password + refresh). Other providers plug in through `OTLOAD_AUTH=<module:Class>`. |
| Personas / tasks | `loadtest/otload/personas/*.py`, `loadtest/otload/tasks/*.py` |
| Bot brain | `loadtest/otload/brain/{scripted,phrasebook,llm}.py` |
| Config | `loadtest/personas.yaml`, `loadtest/pages.yaml`, `loadtest/scenarios/*.yaml` (incl. `ingest.yaml`), `loadtest/slo.yaml` |
| HAR → page bundles | `loadtest/tools/har_to_pages.py` |
| Tests | `loadtest/tests/test_{shapes,personas_mix,recorder,report_slo,auth_local,brain_scripted,guards,sse,ws}.py` |
| Shared RNG | `backend/app/utils/splitmix64.py` (moved from `backend/tests/eval/synthetic/rng.py`, which re-exports it) |
| Dataset builder | `backend/app/scripts/scale_dataset/{__main__,population,seed_export,seed_synthetic,remap,vectors,manifest,verify,snapshot,clean}.py`, `writers/{pg_copy,opensearch_bulk,objects}.py`, `population.yaml` |
| Builder tests | `backend/tests/unit/scale_dataset/test_{population_determinism,remap,vectors}.py`, `backend/tests/integration/test_scale_dataset_build_verify.py` |
| Wrappers | `scripts/scale-dataset.sh`, `scripts/loadtest.sh` |
| Stack commands (existing script, extended) | `./opentr.sh scale-data …`, `./opentr.sh loadtest …`, `--with-loadtest-observability`, `--gpu-devices <list>` |
| Observability overlay | `docker-compose.loadtest.yml`, `monitoring/prometheus/prometheus-loadtest.yml`, `monitoring/grafana/dashboards/loadtest.json` |
| N-GPU workers | `docker-compose.gpu-multi.yml` (one GPU worker per listed host GPU) |
| Docs | `docs-site/docs/operations/scaling-and-sizing.md`, `docs-site/docs/developer-guide/load-testing.md`, links from `performance-tuning.md`, `multi-gpu-scaling.md`, `docs-site/sidebars.ts` |
| Published results (aggregates only) | `docs/benchmark-results/scale/<date>-<sha8>-<tier>/{summary.md,kpis.json}` |

Raw results go to the gitignored `loadtest-results/` and are never deleted. Results dirs are
write-once, and a re-render keeps the previous copy as `<file>.prev-<UTC>`.

---

## 5. Bot model

### 5.1 Session model

Each bot is one simulated person, pinned to one dataset user (from the roster, §7.4) and one
persona.

1. **Arrive** when the load shape says.
2. **Log in.** `POST /api/auth/token` returns access and refresh tokens; refresh goes through
   `POST /api/auth/token/refresh`. Bearer by default; a cookie mode exists for SPA parity.
   - Tokens are cached per bot (`<out>/sessions/<bot>.json`, 0600, gitignored).
   - Shipped limits that matter:
     - `RATE_LIMIT_AUTH_PER_MINUTE` = 10 **per client IP**. Distributed workers have distinct IPs;
       a test-only raise is allowed on fresh stacks and recorded in the run manifest.
     - `MAX_CONCURRENT_SESSIONS` = 5 (`terminate_oldest`). Bots reuse their session.
     - `SESSION_IDLE_TIMEOUT_MINUTES` = 15. Bots refresh or re-login.
   - The `login-herd` scenario measures login under the shipped limits on purpose, including
     password-hash CPU cost.
3. **First page.** The `app_shell` bundle. Then one WebSocket to `/api/ws`, authenticated with the
   first frame and kept for the session (status events and their lag are recorded).
4. **Loop.** Pick a task by persona weight, issue its page bundle, wait a lognormal think time.
   The session length is lognormal (median 20 min).
5. **Leave.** Log out, close the socket, go dormant until the shape brings the bot back.

**Page bundles.** A page view is the exact set of API calls the SPA makes for that route. They are
captured, not guessed:

- Playwright HAR recordings of each route are converted by `tools/har_to_pages.py` into
  `pages.yaml`.
- A drift check in CI re-records two routes and fails on added or removed calls.

### 5.2 Personas and task mix

The full tables live in `loadtest/personas.yaml` (seeded and versioned). Think times are lognormal
(median / p90).

| Persona | Share | Think | Main tasks (probability) |
|---|---|---|---|
| Reader / reviewer | 30 % | 12 s / 45 s | gallery + filters 20 %, open file (detail, first segments page, summary, comments) 25 %, scroll segments 20 %, media stream + range reads 10 %, comments 10 %, speakers 5 %, waveform 5 %, collections 5 % |
| Editor | 15 % | 20 s / 90 s | open 15 %, segment text edit 25 %, segment speaker reassign 10 %, speaker rename 10 %, speaker merge 3 %, tags 12 %, comments CRUD 15 %, collection add 5 %, summary reprocess 1 %, export 4 % |
| Researcher | 15 % | 15 s / 60 s | keyword search 30 %, hybrid search 25 %, filters/suggestions 15 %, open hit at timestamp 20 %, count 5 %, save to collection 5 % |
| Chat analyst | 10 % | 25 s / 120 s | new conversation 20 %, chat turn (SSE) 50 %, regenerate 5 %, list/open 15 %, context estimate 5 %, export 5 % |
| Uploader | 8 % | 30 s / 180 s | short clip upload 35 %, status poll 40 %, open new file when completed 25 % |
| Speaker curator | 5 % | 20 s / 90 s | clusters 20 %, unverified inbox 20 %, batch verify 15 %, profile assign/suggestions 20 %, merge/split 10 %, media preview 10 %, recluster 5 % |
| Team lead / org admin | 4 % | 30 s / 120 s | members 25 %, usage 25 %, share collection/tag 20 %, shared-with-me 20 %, groups 10 % |
| Watcher (open tab) | 12 % | 60 s / 300 s | WebSocket open, gallery refresh 50 %, status/notifications 50 % |
| Admin | 1 % (≤ 2 bots) | 60 s | admin stats, stuck files, tasks, index health |

**Correctness inside the load:**

- **Search canaries.** Every built file carries a unique seeded phrase. A bot searching its own
  canary must find the file (`search_canary_miss_total`). Searching another tenant's canary must
  return **nothing** (`tenant_canary_leak_total` must stay 0). This is a live tenant-isolation
  check under load.
- **Read-your-write.** After an edit or comment, the bot re-reads the object and searches the new
  text. The lag is recorded in `ryw_lag_seconds`.
- **Status consistency.** WebSocket events are cross-checked against `GET /api/files/{uuid}`
  (`ws_missed_event_total` = 0).

### 5.3 Pipeline in the background

- Uploader bots send short clips, remuxed so each upload is unique and not de-duplicated.
- An optional steady background upload stream keeps the GPU pipeline busy during a user test
  (`background_upload_rate` in the scenario).

### 5.4 Load shapes (`otload/shapes.py`, seeded)

| Scenario | Shape | Purpose |
|---|---|---|
| `smoke` | 5 bots, 5 min | Wiring; every task once |
| `steady` | ramp 10 min → hold 30 min → drain | Tier baseline (verdicts from the hold) |
| `knee` | +10 % bots every 5 min until an SLO breaks twice, step back, hold | Capacity per configuration |
| `workday` | Compressed 2 h day: two peaks, lunch dip, seeded noise | Waves |
| `spike` | ×3 within 60 s, hold 5 min, back | Recovery time |
| `login-herd` | N bots arrive within 60 s | Auth CPU, cold caches |
| `hot-file` | 30 % of bots on one shared collection | Hot rows and cache contention |
| `search-storm` | Open-loop search at R qps, ramped | OpenSearch knee (keyword vs hybrid) |
| `chat-storm` | Open-loop chat at R turns/min | Retrieval, context building, SSE, LLM provider |
| `shared-egress` | 200 bots from **one** source IP | Per-IP rate limits for users behind one NAT/proxy |
| `ingest` | Open-loop uploads from a local directory, bounded in-flight | Corpus ingest (P2) |
| `soak-8h` / `soak-24h` | `workday` repeated, or 70 % of the knee | Leaks, drift, autovacuum, index growth |

### 5.5 Bot brains

- **Scripted (all baselines).** Every choice comes from a per-bot SplitMix64 stream: task, target,
  text and think time. Text comes from the dataset's **phrasebook**. Same seed + same dataset =
  the same request sequence.
- **Phrasebook.** Built per seed library by `brain/phrasebook.py`:
  - **Template mode** for the synthetic seed (no model).
  - **LLM mode:** a small local model reads each seed file's summary and a sample of segments and
    emits keyword queries, natural-language questions, comments, plausible edits and expected hits
    as JSON. It is generated once and cached. It is never committed when the seed is private.
- **LLM-live (optional, never a baseline).** The bot sends its last observation to a local
  OpenAI-compatible model (`--with-llm-test`, or a 3–4 B instruct model that fits a 12 GB card)
  and gets a constrained JSON action. Timeout or invalid output → scripted fallback. Model latency
  counts as think time.

---

## 6. Data-scale tiers

The distributions are seeded and live in `scale_dataset/population.yaml`:

- Files per user: lognormal, median 8, mean ≈ 20, cap 1,000.
- Comments: 60 % of files have none, else Poisson 4.
- Edits: 70 % of files have none, else geometric, mean 6.
- Tags: 0–5 per file (Zipf vocabulary).
- Collections: Poisson 3 per user, 20 % shared.
- Chat: 40 % of users have none, else Poisson 5 conversations × mean 6 messages, with citations.
- Speakers: from the seed (recurring speakers → real clusters).

`scale_dataset plan --tier <T>` prints exact counts. The densities below are **estimates** to be
replaced with values measured after P2.

| Tier | Registered users | Peak concurrent bots | Spike | Files | Audio h | Segments (≈600/h) | Search chunks (≈100/h) | Comments | Chat messages |
|---|---|---|---|---|---|---|---|---|---|
| S (CI) | 10 | 5 | – | ~100 | ~100 | ~60 k | ~10 k | ~150 | ~500 |
| T1 | 100 | 50 | 150 | ~2 k | ~2.5 k | ~1.5 M | ~250 k | ~3 k | ~6 k |
| T2 | 1,000 | 250 | 750 | ~20 k | ~25 k | ~15 M | ~2.5 M | ~30 k | ~60 k |
| T3 | 10,000 | 1,000 | 3,000 | ~200 k | ~250 k | ~150 M | ~25 M | ~300 k | ~600 k |

- **Active vs dormant.** Only the active pool logs in. Dormant users only hold data, which shapes
  table and index size and per-user filter selectivity.
- **T3 footprint (estimate).** Postgres ~80–120 GB. OpenSearch: ~38 GB of raw 384-dim vectors plus
  the HNSW graph (~60 GB k-NN native memory). The preflight checks free disk against `plan`.

---

## 7. Dataset builder (`backend/app/scripts/scale_dataset/`)

It runs **inside the backend container** (app models, settings, storage client), like
`corpus_injection`:

```
python -m app.scripts.scale_dataset {plan,seed-synthetic,seed-export,build,verify,snapshot,restore,reset-run,clean}
./opentr.sh scale-data <subcommand> --fresh <name> [...]
```

### 7.1 Two paths, one data model

| | Realistic path | Fast path |
|---|---|---|
| How | Real uploads through the API and pipeline, then a **history replay** (a Locust `history` scenario at zero think time creates comments, edits, tags, collections, shares and chats through the API) | `build` clones a **seed library** into N users with deterministic remapping: Postgres via COPY, OpenSearch via bulk with precomputed vectors, objects by copy or reference |
| Speed | GPU-bound | T2 ≈ 30–60 min, T3 ≈ 2–4 h (estimates) |
| Use | Seed library; T1 fidelity reference | T2, T3, every repeat |

**Rule.** The fast path writes only row types that the realistic path produced in the seed, and
the verifier diffs them. Because the builder lives in core, a migration that changes a table breaks
its integration test until the builder is updated.

### 7.2 Seeds

- **`seed-synthetic`.** Generate with `backend/tests/eval/synthetic`, inject on a throwaway stack
  through `corpus_injection`'s synthetic adapter (the production indexer computes the vectors),
  then `seed-export`. Synthetic speakers get seeded embedding centroids plus per-occurrence noise,
  so clustering has structure. Public and reproducible; the CI default.
- **`seed-export --from-stack`.** Exports completed files (rows, search docs with vectors, speaker
  embeddings, object keys, phrasebook, `SHA256SUMS`) into a **seed bundle** under
  `loadtest-results/seed/<name>/`. Bundles from private corpora stay local and are never committed.
- **Media modes:**
  - `copy`: a server-side copy per clone.
  - `shared`: the clone references the seed object. Bots never delete built files, and `clean`
    removes rows only.
  - `stub`: a short stand-in, for API-only runs.

### 7.3 Remapping and realism

- **Ids.** `uuid5(namespace(dataset), "<table>:<seed_key>:<clone>")`. Integer ids come from
  reserved sequence blocks.
- **Tenant stamps** on every row and document that carries them. Per-tenant unique tag and
  collection names. `verify` asserts zero unstamped rows.
- **Clone de-duplication.**
  - Seeded entity-name substitution and light sentence jitter in text.
  - Seeded Gaussian vector noise (σ = 0.02), renormalised, so HNSW does not hold thousands of
    identical points.
- **Canaries.** One unique seeded canary phrase per file, recorded in the manifest.
- **Time.** Timestamps are spread over a seeded 12-month window with a workday rhythm.
- **Marks.** Every built row is selectable for `verify`/`clean`:
  - users `<dataset>-<n>@loadtest.invalid`;
  - `MediaFile.metadata_important.loadtest`;
  - a `loadtest_dataset` keyword field on search docs (test stacks only).

### 7.4 Writers, manifest, roster

- **Postgres:** COPY in FK order, commits every 50 k rows, `ANALYZE` at the end (indexes stay in
  place, as on a live system).
- **OpenSearch:** bulk 5–10 MB. `refresh_interval: -1` and replicas 0 during the build, then
  restored to the values read beforehand.
- **Manifest:** `loadtest-results/datasets/<id>/manifest.json` (tier, seed, seed-bundle hash, core
  SHA, migration head, counts, timings), `files.jsonl` and `roster.jsonl`.
- **Roster:** for each active bot: user, persona, group, owned files (sample), shared collections,
  own and foreign canaries.
- **Bot password:** from env `OTLOAD_BOT_PASSWORD` (never committed). Hashed once and reused.

### 7.5 Verification

1. DB vs search-doc counts per file and per user. Zero orphans.
2. 1 % object HEAD sample.
3. `GET /api/search/index-health` is healthy.
4. API spot check as 20 bots, including the cross-tenant canary.
5. JSON shape diff of the main GETs, cloned vs seed.
6. **Fidelity A/B at T1** (once, then on builder changes). Fast-path T1 vs realistic T1 under
   `steady` at 50 bots: per-endpoint p50/p95 within ±15 % and store metrics within ±20 %.
   Otherwise the builder is fixed before fast-path results count.

### 7.6 Tests

- Unit: population determinism, remap/stamps/canaries, vector jitter.
- Integration (`integration` marker): build tier S from the synthetic seed, verify, use the API as
  two bots (incl. the foreign canary), clean to zero.
- **Watch the integration test fail first** against a deliberately broken writer (e.g. skipped
  tenant stamps).

### 7.7 Snapshots

`snapshot`/`restore` = a Postgres template database copy + an OpenSearch snapshot to a filesystem
repository inside the fresh stack's volume. A/B tuning runs start from identical data.
`reset-run` deletes what a run's bots created (ids from the raw log) and reverts logged edits.

---

## 8. Measurements and SLOs

### 8.1 Client

`requests.jsonl.gz` per worker is the raw record and is never deleted. One row per request:
scheduled/actual start, route template name, persona, bot, status, latency, TTFB, bytes, error
kind, segment. Plus:

- chat TTFT and total;
- WebSocket connect, auth, event lag, drops;
- correctness counters.

The Locust master exports Prometheus metrics (`otload/prom.py`).

### 8.2 Server

- **API:** `http_request_duration_seconds` (server truth vs client), `http_requests_in_flight`,
  `db_query_duration_seconds`, `db_queries_per_request` (N+1 detector: p95 per route must not grow
  with data size), `cache_operations_total`.
- **Queues:** `celery_queue_*`, plus #1172's `celery_task_queue_wait_seconds` /
  `celery_queue_oldest_message_age_seconds`.
- **Pipeline:** `pipeline_stage_duration_seconds`.
- **Containers:** cAdvisor and `nvidia-smi`.

### 8.3 Data stores (overlay `--with-loadtest-observability`)

- **Postgres.**
  - Collection: `postgres-exporter` plus sampler queries:
    - `pg_stat_statements` top-N by total and mean time;
    - `pg_stat_activity` by state and wait event;
    - blocked/blocking `pg_locks`;
    - `pg_stat_database` (deadlocks, temp files, cache hit);
    - `pg_stat_user_tables` (dead tuples, autovacuum, seq vs index scans);
    - `pg_stat_progress_vacuum`.
  - Logging: `log_lock_waits` and `log_min_duration_statement` = 250 ms on test stacks.
  - P0 adds these to the base Postgres `command:`, defaulted and overridable:
    `shared_preload_libraries=${PG_SHARED_PRELOAD_LIBRARIES:-pg_stat_statements}`,
    `pg_stat_statements.track`, `log_lock_waits=on`,
    `log_min_duration_statement=${PG_LOG_MIN_DURATION_MS:--1}`.
    The overhead is small and the sizing guide documents it. The extension is created when the role
    permits; otherwise a warning is printed.
- **OpenSearch.** The sampler polls:
  - `_nodes/stats`: search/write thread-pool queue and **rejections**, heap, GC, query latency
    deltas, indexing rate;
  - `_plugins/_knn/stats`: graph memory, cache hits/misses;
  - index sizes;
  - a search slow log at 500 ms.
- **Redis.** `redis-exporter`: ops/s, clients, memory, evictions, latency.
- **Dashboard.** `monitoring/grafana/dashboards/loadtest.json`: Locust, API, Postgres, Redis,
  OpenSearch, queues and containers on one page.

### 8.4 SLOs (proposed; ratified in review)

Evaluated on the measured segment and on the server-side histogram unless noted.

| Class | Examples | p95 | p99 |
|---|---|---|---|
| A. Page reads | `/auth/me`, gallery, file detail, segments page, comments, tags, collections, speakers, status | ≤ 300 ms (client ≤ 500 ms) | ≤ 1 s |
| B1. Keyword search | `/search` keyword, count, suggestions, filters | ≤ 800 ms | ≤ 2 s |
| B2. Hybrid/semantic search | `/search` hybrid | ≤ 1.5 s | ≤ 3 s |
| C. Writes | segment edit, speaker rename/reassign, comment CRUD, tag, collection | ≤ 500 ms | ≤ 1.5 s |
| D. Chat | first SSE token | ≤ 5 s (mock LLM: app overhead ≤ 1.5 s) | ≤ 10 s |
| E. Heavy | recluster, export, reprocess | accepted ≤ 1 s; completion by queue SLO | – |
| F. WebSocket | connect + auth ≤ 1 s; event lag p95 ≤ 2 s; ≥ 99.9 % sessions without unexpected drop | | |

**Other SLOs:**

- **Errors.** 5xx < 0.1 %. 429 < 0.5 % and only on rate-limited routes. 0 deadlocks. 0 pool
  timeouts.
- **Correctness.** Tenant canary leaks = 0 (hard fail). Canary misses < 0.1 %. 0 missed WebSocket
  events. Read-your-write: API ≤ 1 s, search ≤ 10 s p95.
- **Resources.** DB connections < 80 % of max. 0 OpenSearch search rejections. Heap < 75 %. No
  Redis evictions of non-volatile keys. Interactive queues' oldest message age p95 < 30 s.
- **Dynamics.** Back within SLO ≤ 2 min after a spike. Soak: p95 drift < 10 % over 8 h; backend
  RSS growth < 5 %/h after warm-up; 0 restarts.

**Verdicts.** A tier **passes** when `steady` at its target concurrency meets every SLO. Its
**capacity** is the `knee` result.

### 8.5 Report

`otload report` writes `summary.md` and `kpis.json`:

- per-endpoint percentiles per segment, client vs server;
- the SLO verdicts;
- the knee;
- the correctness counters;
- top SQL and its change vs the baseline, locks and deadlocks, autovacuum and bloat;
- OpenSearch rejections and latency;
- queue waits;
- resource peaks;
- spike recovery and soak drift;
- charts.

`otload compare <runA> <runB>` flags regressions between runs and warns when the dataset, seed or
hardware differ.

---

## 9. Tuning loop and knobs

**Loop.** One change per run, on a restored snapshot:

1. Run `knee`.
2. Read the bottleneck section.
3. Change one knob or add one index.
4. Run `knee` again. Keep the change if capacity rises ≥ 5 % and no SLO class regresses.
5. Log it in `tuning-log.md` in the results dir.

Code fixes (indexes, N+1 queries, query shape) become normal issues and PRs.

| Layer | Knobs | Note |
|---|---|---|
| uvicorn | worker processes (`WEB_CONCURRENCY` / `--workers`) | The prod image runs **one** process today. Multi-process needs `PROMETHEUS_MULTIPROC_DIR` for `/metrics`, cross-process WebSocket fan-out, migrations under a lock, and Redis-backed rate-limit storage. P1 checks each and files issues. |
| FastAPI threadpool | `API_THREADPOOL_SIZE` (default max(40, pool + overflow)) | in-flight requests, saturation |
| SQLAlchemy | `DB_POOL_SIZE`/`DB_MAX_OVERFLOW` (API 20/40; workers `WORKER_DB_POOL_SIZE`/`WORKER_DB_MAX_OVERFLOW` 2/3) | Postgres `max_connections` budget |
| Postgres | `PG_SHARED_BUFFERS`, `PG_EFFECTIVE_CACHE_SIZE`, `PG_WORK_MEM`, `PG_MAINTENANCE_WORK_MEM`, `PG_MAX_CONNECTIONS`, `PG_RANDOM_PAGE_COST`, per-table autovacuum (`transcript_segment`), statistics target, indexes | cache hit, temp files, seq scans, dead tuples, lock waits |
| OpenSearch | `OPENSEARCH_JAVA_OPTS` heap, shards/replicas, `refresh_interval` (1 s → 5–30 s), k-NN `ef_search`, k-NN memory/cache | rejections, heap, latency |
| Redis | `maxmemory`, policy, pool | evictions, latency |
| Celery | per-queue concurrency, prefetch, pool, `max-tasks-per-child` | queue wait, oldest age |
| Rate limits | `RATE_LIMIT_*` (per IP by default; search per user), `RATE_LIMIT_TRUSTED_PROXIES` | **Shared-egress:** if one office's users behind one IP get throttled, propose per-user keys for authenticated routes (cf. #668) |
| Reverse proxy | nginx worker connections, keepalive, timeouts for SSE/WebSocket | 502/504, drops |

---

## 10. Safety and data rules

- **Fresh stacks only** (`--fresh`). The live data paths and their NAS overlay are never loaded.
  `./opentr.sh data-paths` is checked before any `clean`.
- **The builder writes only marked rows.** `verify` and `clean` select by the marks plus the
  manifest.
- **Kill switch:** Ctrl-C, a `STOP` file in the results dir, or the Locust master `/stop`.
- **Abort rules:** 5xx > 2 % for 2 min, any tenant canary leak, DB connections > 90 %, generator
  CPU > 90 %.
- **GPU safety.** Never SIGKILL a GPU worker. Stop containers gracefully only (a killed CUDA
  context can wedge the card). Check `nvidia-smi` before using a card.
- **Private corpus:**
  - Inventory, lock lists, seed bundles, phrasebooks and transcripts stay in gitignored
    `loadtest-results/` (and a private backup).
  - Committed results are aggregates only, and a scrubber check refuses any name from the private
    inventory (compared by hash).
  - Public docs call it "a private long-form podcast corpus".
- **Seeds are reproducible:** SplitMix64 everywhere, and seeds are recorded in every manifest.

---

## 11. Phases

Tests and builds run once per batch; targeted tests per PR.

### P0: tooling, builder, observability (≈ 18 d)

**Work:**

- Everything in §4.
- Move SplitMix64 to `app/utils/splitmix64.py`; the synthetic tier's byte-identity tests guard it.
- Add `docker-compose.gpu-multi.yml` + `--gpu-devices`, with the overlay isolated on `--fresh`
  (extend `backend/tests/unit/test_opentr_fresh_aux_isolation.py`).
- Dependency: #1172 merged.

**Verification:**

- `pytest loadtest/tests -v`.
- The builder unit tests, and its integration test seen failing first, then passing.
- `./opentr.sh start dev --fresh lt0 --with-loadtest-observability --dry-run` shows the overlay.
- `.venv`/backend venv `pre-commit` on the changed files.

**Exit criteria:**

- Tier S builds, verifies and cleans in CI.
- `smoke` runs on a fresh stack with 0 errors.
- The dashboard shows every panel.

### P1: small local scale (≈ 3 d)

**Work:**

1. Start `./opentr.sh start dev --fresh scale1 --port-offset 800 --with-loadtest-observability --with-mock-llm`.
2. Build tier S, then T1, from the synthetic seed.
3. Run `smoke`, `steady` (50), `spike`, `login-herd`, `hot-file` and `shared-egress`.
4. Run `steady` with uvicorn workers 1/2/4 (multi-process readiness).
5. Capture HAR → `pages.yaml`.
6. Run the realistic T1 history replay (for the §7.5 A/B).

**Exit criteria:** all persona tasks pass, the report is complete, and issues are filed for
defects found.

### P2: corpus ingest on multiple GPUs (≈ 3 d + ~1 day unattended GPU)

**Work:**

1. Stage the private corpus to local scratch. The source store is read-only and is never written.
2. Probe durations and hash.
3. Start a fresh stack with `--gpu-devices <free cards>`. Give fewer worker slots to a 12 GB card.
4. Run the `ingest` scenario (bounded in-flight), with `ENABLE_BENCHMARK_TIMING=true` and
   `pipeline_stage_duration_seconds` recorded. Estimate: ~2,000–2,500 audio-h at ~100+ audio-h/h on
   three cards ≈ 20–25 h. Re-estimate after 50 files.
5. Generate summaries with a local LLM pass.
6. `seed-export` → local seed bundle.
7. Build the phrasebook in LLM mode.

**Exit criteria:**

- ≥ 98 % of episodes completed (failures listed).
- `verify` passes.
- Measured per-hour densities written back into §6.
- An aggregate-only GPU throughput summary.

### P3: waves, spikes, tuning (≈ 9 d)

**Work:**

1. Build T1 and T2 from the corpus seed. Pass the §7.5 A/B before T2 results count.
2. Run baselines per tier: `steady`, `knee`, `workday`, `spike`, `login-herd`, `hot-file`,
   `search-storm`, `chat-storm`.
   - Use the mock LLM for repeatable numbers.
   - Do one real local-LLM chat-storm.
   - Do one `workday` with background uploads.
3. Run the tuning loop (§9). Likely first suspects:
   - the single uvicorn process;
   - per-IP limits under shared egress;
   - `transcript_segment` queries at 15–150 M rows;
   - OpenSearch refresh and heap at millions of chunks;
   - the per-user search limit for bursty researchers.
4. Build T3 and run `steady`/`knee` at 1,000 bots and a ×3 spike, with distributed Locust (8–16
   workers).

**Exit criteria:**

- T1 and T2 pass at target with tuned settings, or each failing SLO has an issue and a recorded
  waiver.
- T3 knee measured.
- `tuning-log.md` complete.
- Default changes filed.

### P4: documentation (≈ 3 d)

**Work:**

1. `docs-site/docs/operations/scaling-and-sizing.md`:
   - sizing tiers (S/M/L/XL by registered and concurrent users and library size);
   - Postgres env per tier;
   - uvicorn workers, threadpool and pools;
   - OpenSearch heap, shards and refresh;
   - Redis;
   - Celery;
   - GPU throughput per card class (links);
   - measured tables with SHA and date;
   - how to rerun.
2. `docs-site/docs/developer-guide/load-testing.md`.
3. Links and sidebar.
4. Aggregate results under `docs/benchmark-results/scale/`.

**Verification:** `docs-build` hook, `npm run build` in `docs-site/`, and the page opened in a
browser.

### P5: distributed / Kubernetes rerun (optional, ≈ 4 d)

When the Helm chart (#864) lands:

1. Run T1/T2 `steady`, `knee`, `workday` and `spike` against a multi-replica deployment with Locust
   workers as pods.
2. Measure horizontal API scaling (replicas vs capacity) and how a CPU-based autoscaler keeps up
   with `workday`.
3. Add a "multi-node" table to the sizing guide.

### P6: soak and failure injection (≈ 4 d + 24 h)

**Work:**

1. Run `soak-8h` at 70 % of the T2 knee with faults every 45 min:
   - restart Postgres, Redis, OpenSearch and the backend under load;
   - stop a CPU worker with tasks in flight;
   - graceful stop of a GPU worker (never a hard kill on a shared host).
2. Then run `soak-24h` without faults.

**Pass criteria:**

- API returns clean 503s, not hangs, and pools recover without restarts ≤ 1 min after the
  dependency returns.
- Tasks requeue (worker-loss recovery, #1159) with 0 stuck after 6 min.
- Search degrades cleanly and indexing catches up.
- Soak drift and restarts are within SLO.

---

## 12. Effort and ordering

| Phase | Effort | Depends on |
|---|---|---|
| P0 framework / builder / observability | 8 / 8 / 2 | – |
| P1 | 3 | P0 |
| P2 | 3 + GPU wall | P0 (ingest scenario, seed-export) |
| P3 | 9 | P1, P2, #1172 |
| P4 | 3 | P3 |
| P5 (optional) | 4 | #864 |
| P6 | 4 + 24 h | P3 |
| **Total** | **≈ 40–44 days** | |

**Ordering:**

1. P0 (three parallel tracks).
2. P1.
3. P2 (unattended GPU, overlapping with P3 prep).
4. P3.
5. P4 ∥ P6.
6. P5 when #864 lands.

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| Fast-path data unrealistic | §7.5 A/B gate; seeds come from the real pipeline; text/vector jitter |
| Load generator saturates | Scheduled-vs-actual lag check, worker CPU guard, more workers; k6 fallback for open-model segments |
| Schema drift breaks the builder | The builder lives in core; integration test on every migration |
| Private data leaks into the repo | Local-only bundles, hashed-name scrubber, aggregate-only results |
| Shared host noise | Record host load in every sample; rerun outliers; isolated fresh stacks |
| GPU wedge from killed CUDA processes | Graceful stops only; check `nvidia-smi` first |

---

## 14. Open questions

1. Ratify the SLO targets in §8.4.
2. Should authenticated API routes be rate-limited per user rather than per IP by default
   (depends on the `shared-egress` result)?
3. Should `pg_stat_statements` be on by default in the shipped compose (P0 proposes yes, with an
   env override)?
4. Is the uvicorn multi-process default worth changing for single-host installs after P1/P3, or is
   it documented as a tuning knob only?
