# Concurrency foundations: audit, fixes and the concurrent-user load test

Status: **audit done; seven defects fixed on `v070/concurrency`; load test written, NOT run.**
Written 2026-11-07 for v0.7.0. Target: thousands of registered users, hundreds active at
once, on one deployment, locally and as the base a managed deployment builds on.

Issue #1337 fixed what ONE request costs at data scale (100k files, 1M speakers;
`docs/design/1337_query_review_plan.md`, measured baseline `~/ot-cleanup-work/1337/perf/m1/RESULTS.md`).
This document is about what MANY concurrent requests do to each other: pools, threads, the event
loop, Redis, WebSocket fan-out, Celery fairness and anything that serialises users.

The broader multi-user programme (personas, load shapes, SLOs, soak, Kubernetes) is
`docs/design/multi-user-scale-test_plan.md`. The Locust scenario in section 4 is the first,
scripted slice of that plan's P0/P1, at `scripts/perf/load/`.

How to read the severities: **High** = with hundreds of active users this degrades every user,
not just the one who triggered it; **Medium** = degrades many users under a plausible burst or a
common configuration; **Low** = waste or a latent hazard with a bounded blast radius.

Line references are to the tree at the commit that adds this file.

---

## 1. Summary

| # | Finding | Severity | State |
|---|---|---|---|
| F1 | One API process: one event loop, one GIL, 60 worker threads for the whole deployment | High | Recommendation R1 (measure first, section 4) |
| F2 | A streamed chat reply held a pooled DB connection, idle in a transaction, for the whole generation | High | **Fixed** (C3) |
| F3 | No per-user fairness in the transcription queues: one bulk upload delays everyone | High | Recommendation R2 |
| F4 | `/api/search`, `/search/count`, `/search/suggestions` hold the request transaction across OpenSearch; the session auditor cannot see it | Medium | **Fixed** on `v070/concurrency2` (also `/search/filters`; auditor now sees search-service calls) |
| F5 | Async handlers run synchronous SQLAlchemy on the event loop (pool checkout can block the loop up to 30 s) | Medium | 3 hot ones **fixed** (C2, C4); the other 12 **fixed** on `v070/concurrency2`, guarded by `tests/api/test_async_handler_session_use.py` |
| F6 | WebSocket fan-out iterated live collections across awaits: lost notifications for everyone | Medium | **Fixed** (C5) |
| F7 | WebSocket handshake authenticated on the event loop | Medium | **Fixed** (C4) |
| F8 | `/llm/status` (polled by every tab) and `/files/{uuid}/summary-status` held the request transaction across the provider health check | Medium | **Fixed** (C2); caching the result is R5 |
| F9 | Connection budget is per PROCESS, documented as per container; defaults can reach 270 against `max_connections=200` | Medium | Recommendation R6 |
| F10 | A prefork child's `engine.dispose()` closed the parent's connections | Medium | **Fixed** (C1) |
| F11 | WebSocket fan-out is sequential with no send timeout (one slow client delays the subscriber for all) | Medium | Recommendation R7 |
| F12 | Per-IP rate limits on page-load endpoints throttle many users behind one NAT or an untrusted proxy | Medium (config) | Recommendation R8 (documentation) |
| F13 | No pool saturation metric | Low | **Added** (C6: `db_pool_*` gauges, `backend/app/core/db_metrics.py:146-189`) |
| F14 | Cache invalidation push opens a new Redis connection per call | Low | Recommendation R9 |
| F15 | WebSocket subscriber task created without a kept reference | Low | Recommendation R9 |
| F16 | Four `BaseHTTPMiddleware` layers per request | Low | Recommendation R10 (measure) |
| F17 | `pool_timeout` (30 s) is not configurable | Low | Recommendation R6 |
| F18 | Instance-month LLM budget row is a single hot row when that limit is set | Low | Note; measure under chat load |
| -- | Global Python locks, advisory locks, `FOR UPDATE` on shared rows | none found | Verified, section 2.7 |

Commits on `v070/concurrency` (each with a test that failed on a `git archive HEAD` tree and
passes after; evidence in section 3):

| | Commit | Fixes |
|---|---|---|
| C1 | `fix(celery): keep the parent's DB connections alive when a prefork child starts` | F10 |
| C2 | `fix(llm): release the request transaction before the provider health check` | F8, part of F5 |
| C3 | `fix(chat): release the request connection before streaming the reply` | F2 |
| C4 | `fix(websocket): authenticate the socket off the event loop` | F7, part of F5 |
| C5 | `fix(websocket): fan out over a snapshot of the connections` | F6 |
| C6 | `feat(metrics): expose the API's DB pool use as db_pool_* gauges` | F13 |
| C7 | `test(perf): concurrent-user Locust scenario, runbook and route smoke test` | section 4 |
| C8 | this document | |

---

## 2. Audit

### 2.1 Connection pool, threads, processes and `max_connections`

**What the code does.**

| Setting | Value | Where |
|---|---|---|
| Engine | one per process, `QueuePool` (SQLAlchemy default), `pool_pre_ping=True`, `pool_recycle=3600`, `pool_size=DB_POOL_SIZE`, `max_overflow=DB_MAX_OVERFLOW`, `pool_timeout` default 30 s | `backend/app/db/base.py:22-29` |
| API pool | `DB_POOL_SIZE=20`, `DB_MAX_OVERFLOW=40` (capacity 60) | `backend/app/core/config.py:700-701`, `.env.example:185-186` |
| Worker pool | `WORKER_DB_POOL_SIZE=2`, `WORKER_DB_MAX_OVERFLOW=3` (capacity 5), injected as `DB_POOL_SIZE`/`DB_MAX_OVERFLOW` into every Celery service | `docker-compose.yml:433-434` (and the same pair in each worker service), `.env.example:198-199` |
| API threads | `API_THREADPOOL_SIZE=0` means `max(40, pool capacity)` = 60, applied to AnyIO's default limiter in the lifespan | `backend/app/core/threadpool.py:36-58`, `backend/app/main.py:91-93`, `config.py:709` |
| Idle-in-transaction backstop | `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS=300000` | `config.py:727`, `.env.example:195` |
| Postgres | `max_connections=${PG_MAX_CONNECTIONS:-200}` | `docker-compose.yml:29`, `.env.example:182` |
| API processes | ONE uvicorn process, no `--workers`, in dev (`docker-compose.override.yml:106`, `--reload`) and prod (`backend/Dockerfile.prod:550`) | |

Documentation the analysis rests on:

- SQLAlchemy, "Connection Pooling" (https://docs.sqlalchemy.org/en/20/core/pooling.html):
  `QueuePool` holds `pool_size` connections open and allows `max_overflow` more, which are closed
  on return; a checkout beyond capacity **blocks the calling thread** for up to `pool_timeout`
  (default 30 s) and then raises `TimeoutError`. `pool_pre_ping` issues a ping on every checkout
  ("Disconnect Handling - Pessimistic"). "Using Connection Pools with Multiprocessing or
  os.fork()": a child must call `engine.dispose(close=False)` so it does not touch the parent's
  connections.
- FastAPI, "Concurrency and async / await" (https://fastapi.tiangolo.com/async/): a path
  operation or dependency declared with plain `def` runs in an external threadpool; an
  `async def` one runs on the event loop and must not block.
- Starlette, "Thread Pool" (https://www.starlette.io/threadpool/) and AnyIO "Working with
  threads" (https://anyio.readthedocs.io/en/stable/threads.html): one default limiter of
  **40 tokens** per event loop shared by every sync endpoint, sync dependency and
  `run_in_threadpool`; raise it through `current_default_thread_limiter().total_tokens`.
- PostgreSQL, "Connections and Authentication" (https://www.postgresql.org/docs/17/runtime-config-connection.html):
  `max_connections` minus `superuser_reserved_connections` (3) is what application roles get;
  each connection is a backend process, and memory per connection grows with `work_mem` per
  sort/hash node (`PG_WORK_MEM=16MB` here).

**The API side is already sized correctly within one process** (#1169): threads = pool capacity,
so the documented thread/connection cross-wait (`threadpool.py` docstring) cannot occur below
120 concurrent requests. Requests above 60 in flight queue at the AnyIO limiter, holding nothing.

**F1 (High). One process is the deployment's ceiling.** Every API request is served by one Python
process: one event loop (the 26 `async def` handlers, all middleware, every WebSocket, every SSE
stream) and one GIL for 593 sync handlers on 60 threads. JSON/Pydantic serialisation, password
hashing at login (PBKDF2 600,000 iterations, `backend/app/core/security.py:21-33`; hashlib releases
the GIL, the rest does not) and Presidio snippet masking in search (about 2 s CPU per call, per the
`/search` docstring, `backend/app/api/endpoints/search.py:142-147`) all compete for one core. The
codebase records the assumption: `backend/app/core/CLAUDE.md` ("The single-process Prometheus
registry assumes one uvicorn worker"). See R1.

**F9 (Medium). The connection budget is per process, and the comments say per container.**
`docker-compose.yml:430-434` and `.env.example:184,196-199` describe `WORKER_DB_*` as a
per-worker-container pool. A Celery prefork worker is one MainProcess plus `--concurrency` child
processes, and each child builds its own pool after fork (`backend/app/core/celery_worker_lifecycle.py:149-154`),
so the limit applies per child. Worst case with the defaults (capacity x processes):

| Service | Pool | Processes | Max connections |
|---|---|---|---|
| backend | 60 | 1 | 60 |
| celery-worker (gpu, threads, 1) | 5 | 1 | 5 |
| celery-download-worker (prefork 5) | 5 | 1 + 5 | 30 |
| celery-cpu-worker (prefork 8) | 5 | 1 + 8 | 45 |
| celery-redaction (threads) | 5 | 1 | 5 |
| celery-cloud-asr-worker (prefork 16) | 5 | 1 + 16 | 85 |
| celery-nlp-worker (prefork 4) | 5 | 1 + 4 | 25 |
| celery-embedding-worker (prefork 1) | 5 | 1 + 1 | 10 |
| celery-beat | 5 | 1 | 5 |
| **total** | | | **270** vs `max_connections` 200 (197 usable) |

Steady state is lower (a QueuePool keeps `pool_size` = 2 idle per child, and the API's overflow is
closed on return): roughly 20 + 2 x 42 = 104. The worst case needs every child in a nested
session at once while the API is at capacity, which is unlikely but not impossible during a burst
of uploads while hundreds of users browse; the failure is `FATAL: sorry, too many clients already`
for whichever process connects last, including the API. Adding `--workers` (R1) multiplies the API
row. See R6.

**F10 (Medium, fixed in C1).** `init_worker_process` called `engine.dispose()`
(`backend/app/core/celery_worker_lifecycle.py:154`, `init_worker_process` at `:114`). The MainProcess uses the engine in
`worker_init` (`verify_database_role`, `celery_worker_lifecycle.py:163`), so each child
inherited a pooled connection whose socket it shares with the parent; `dispose()` closes it, and
psycopg2's close sends the server a Terminate over that shared socket, ending the parent's
session. It ran on every child start and every `--max-tasks-per-child` respawn (`cpu-processor`
respawns every 20 tasks). Now `dispose(close=False)`, as the SQLAlchemy docs prescribe.

**F17 (Low).** `pool_timeout` is SQLAlchemy's default 30 s and not configurable. Under overload a
request waits 30 s for a connection before failing; a load test or an operator who prefers fast
failure cannot shorten it. Part of R6.

`pool_pre_ping=True` costs one round trip per checkout; `pool_recycle=3600` is well under any
server or proxy idle timeout here. Both are the documented defaults for long-lived pools and stay.

### 2.2 The threadpool and sync endpoints

- Route handlers: **593 `def`, 26 `async def`** (AST count over `backend/app`, decorators on a
  router or the app). Sync handlers are capped by the 60-token limiter per process
  (`threadpool.py:36-44`); the limiter is the real cap on concurrent sync requests.
- Every request's transaction starts in the auth dependency (`get_current_user`,
  `backend/app/api/endpoints/auth/dependencies.py:118-202`: JWT decode, revocation check, user
  load) and, because `get_db` is a dependency with `yield` (`backend/app/db/base.py:55-60`), the
  session closes only **after the response is sent** (FastAPI, "Dependencies with yield",
  https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/). So any slow
  non-database step inside any handler holds a pool connection, idle in a transaction, for its
  duration. That is the shape of F2, F4 and F8.

**F5 (Medium). Synchronous SQLAlchemy inside `async def` handlers runs on the event loop.** The
existing guard (`backend/tests/api/test_handler_blocking_io.py`) fails an `async def` handler with
no `await`; it cannot see an async handler that awaits something AND calls the session directly.
The dangerous part is the pool checkout: when the pool is exhausted it blocks the calling thread
for up to 30 s, and on the event loop that thread is every request, socket and stream in the
process. Handlers with direct session use on the loop at this commit:

| Handler | file:line | Frequency |
|---|---|---|
| `upload_media_file` | `backend/app/api/endpoints/files/__init__.py:165` | per upload |
| `prepare_upload` (partly threadpooled) | `backend/app/api/endpoints/files/prepare_upload.py:316` | per upload |
| `sign_upload_parts` | `backend/app/api/endpoints/files/multipart.py:81` | per multipart batch |
| `send_message` / `regenerate` / `edit_message` (conversation lookup, supersede) | `backend/app/api/endpoints/chat/messages.py:434,467,516` | per chat turn |
| `logout`, `logout_all_sessions` | `backend/app/api/endpoints/auth/sessions.py:272,363` | per logout |
| `trigger_summarization`, `identify_speakers` | `backend/app/api/endpoints/summarization.py:76,519` | per action |
| `upload_profile_avatar` | `backend/app/api/endpoints/speaker_profiles_attributes.py:39` | rare |
| `get_openai_compatible_models`, `get_anthropic_models` | `backend/app/api/endpoints/llm_model_discovery.py:231,394` | admin, rare |
| `test_auth_connection` | `backend/app/api/endpoints/auth_config_connection_tests.py:42` | admin, rare |
| `oidc_login`, `oidc_callback`, `saml_sls` | `backend/app/api/endpoints/auth/oidc.py:65,165`, `auth/saml.py:331` | per SSO login |

Fixed here: `/llm/status` and `/summary-status` (C2) and the WebSocket handshake (C4), the three
reached on every page load or reconnect. See R4.

### 2.3 Sessions held across slow I/O

Ran `scripts/audit-session-lifetime.py backend/app`: **0 open, 2 backlog**, both already
allowlisted with written reasons (`services/opensearch_service/speaker_metadata.py::sync_speaker_profiles_to_opensearch`,
`services/watch_sources/processing.py::_finalize_media_ingest`, `scripts/session-lifetime-allowlist.txt`).
The tool covers Celery `session_scope()` bodies and functions that take a `Session` and call a
recognised slow API by name. Request handlers are covered behaviourally by
`backend/tests/unit/test_endpoint_session_lifetime.py`. Neither saw the following.

**F2 (High, fixed in C3).** `send_message` (`backend/app/api/endpoints/chat/messages.py:434`, release at `:461`): `_prepare_turn` commits,
then `db.refresh(user_message)` and `llm_reasoning.resolve_enable_thinking(db, ...)` reopen the
request transaction, and the handler returns a `StreamingResponse`. FastAPI closes the session
after the response is sent, i.e. when the model finishes. Every in-flight chat therefore pinned
one of the API's 60 connections, idle in a transaction, for the whole generation (tens of seconds
to minutes; capped only by the 5-minute idle-in-transaction backstop, which would then kill the
connection mid-stream). Sixty concurrent chats starve every other request for 30 s and then fail
them. Now the session is closed once the turn is prepared; the stream persists through its own
`session_scope`. (`regenerate`/`edit_message` already end with a commit, which releases the
connection.)

**F8 (Medium, fixed in C2).** `GET /llm/status` (`backend/app/api/endpoints/llm_status.py:29`, release at `:42`) is polled by every open SPA tab
every 120 s (`frontend/src/stores/llmStatus.ts:29`) and on focus; it awaited `is_llm_available`,
which reads settings through a second session and dials the provider's `/v1/models`
(`backend/app/services/llm_health.py:159-215`, 3 s timeout) while the request session sat in an
open transaction. `GET /files/{uuid}/summary-status` (`backend/app/api/endpoints/files/summary_status.py:34`, reads now in `_read_summary_state` at `:100`) did the same. Each call held two
connections, one idle for the dial; `create_from_settings` and the summary reads also ran on the
event loop.

**F4 (Medium, open).** `search_transcripts` (`backend/app/api/endpoints/search.py:46`, OpenSearch
call at `:197`), `search_match_count` (`:366`) and `search_suggestions` (`:432`) run the hybrid
OpenSearch query, and in `/search` the snippet masking, with the request transaction open. `/count`
and `/suggestions` are per-keystroke and deliberately not rate limited. The auditor misses them
because the slow call is a method on a service object (`HybridSearchService().search`), which its
name-based detectors do not know. See R3.

### 2.4 Redis

redis-py, "Connection Pools" (https://redis.readthedocs.io/en/stable/connections.html): a client
owns a connection pool and is meant to be created once and shared; a new client per call pays a
TCP (and TLS) handshake each time.

- Sync db 0: one process-wide client, `get_redis()` (`backend/app/core/redis.py:44-47`), plus the
  bounded probe client (`:50-57`). Correct.
- Auth stores (`auth/session.py:62`, `auth/lockout_store.py:37`), the db-1 cache
  (`services/redis_cache_service.py:97`) and the YouTube limiter (`services/youtube_rate_limiter.py:89`)
  each keep one lazily built client. Correct.
- **F14 (Low).** `RedisCacheService._push_invalidation` builds a new `redis.Redis` for every
  invalidation and closes it (`services/redis_cache_service.py:255-285`): one connect per cache
  write. See R9.
- SSE streams build one async client per stream and close it (`files/subtitles.py:490,563-572`,
  `files/media_delivery.py:373,431-433`). Each open stream holds one Redis connection for its
  life; acceptable, bounded by open downloads/exports.
- **Rate limiting** (slowapi over `limits`): Redis storage with in-memory fallback, fixed window
  (`backend/app/auth/rate_limit.py:146-161`). One Redis round trip per limited request; in sync
  handlers it runs in the threadpool, in the `async def` chat handlers on the loop (sub-millisecond
  on a local Redis). Correct.
- **F12 (Medium, configuration).** Several limits are per client IP: login 10/min, `/auth/methods`
  60/min, the directory and `/files/owners` 60/min (`config.py:313-338`). Every SPA page load calls
  `/auth/methods` and `/files/owners` (`docker-compose.override.yml:121-126` explains why dev raises
  them). Behind a NAT, or a reverse proxy not listed in `RATE_LIMIT_TRUSTED_PROXIES`, hundreds of
  users share one IP and are throttled together. See R8.

### 2.5 WebSockets and per-request work on the event loop

`/api/ws` (`backend/app/api/websockets.py`): one connection per tab, held for hours; one
`ConnectionManager` per process (`:88`) and one Redis pub/sub subscriber per process
(`:299-338`) that receives every notification and forwards it. Cost per connected user: one
socket, one coroutine waiting in `_receive_until`, and the per-event send. Idle cost is small; the
costs are in the handshake and the fan-out.

- **F7 (Medium, fixed in C4).** The handshake (`websocket_endpoint`, `:556`) ran `session_scope()`,
  the user query and the token checks synchronously on the loop; they now run in
  `_authenticate_in_session` (`:536`) through `run_in_threadpool`. After a backend restart every open tab reconnects at once.
- **F6 (Medium, fixed in C5).** `broadcast()` (`:208`) and `send_personal_message()` (`:198`): `broadcast()` iterated the live `active_connections` dict and
  `send_personal_message()` the live per-user list while awaiting each `send_text`. A connect
  during a broadcast raised `RuntimeError: dictionary changed size during iteration`;
  `redis_subscriber` caught it as an unexpected error, closed its subscription and slept with
  backoff (`:335-338`), and Redis pub/sub does not buffer, so every notification published
  meanwhile was lost for every user on that process. A tab closing mid-send skipped the user's
  next tab. With hundreds of users connecting and disconnecting, broadcasts (cache invalidation,
  `cache_invalidate`/migration progress) hit this routinely.
- **F11 (Medium, open).** The fan-out awaits each `send_text` in turn with no timeout. A client
  whose TCP window is full blocks the single subscriber task, and therefore delivery to every
  other user on the process, until the write drains. See R7.
- **F15 (Low).** `setup_redis()` starts the subscriber with `asyncio.create_task(...)` and keeps no
  reference (`:255`). The Python docs require keeping one ("Save a reference to the result of this
  function, to avoid a task disappearing mid-execution",
  https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task). In practice the task is
  reachable through the future it awaits, so this has not bitten; it is a one-line fix (R9).
- **F16 (Low).** Four `BaseHTTPMiddleware` layers wrap every request (`middleware/audit.py:33`,
  `csrf.py:85`, `robots.py:24`, `observability.py:55`); each runs `call_next` in a child task
  with memory streams (Starlette, "Middleware", https://www.starlette.io/middleware/, which lists
  the limitations and recommends pure ASGI middleware). None does blocking I/O. Worth measuring
  under load before changing (R10).

### 2.6 Celery: fairness, prefetch, visibility timeout

Celery docs: "Optimizing" (https://docs.celeryq.dev/en/stable/userguide/optimizing.html) on
prefetch and `-Ofair`; "Using Redis: Visibility Timeout"
(https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html#visibility-timeout);
"Routing Tasks: Redis Message Priorities" (https://docs.celeryq.dev/en/stable/userguide/routing.html#redis-message-priorities),
which emulates priorities with one list per priority step and offers no per-producer fairness.

- `worker_prefetch_multiplier=1` (`backend/app/core/celery.py:282`): a worker reserves one message
  per process, so a long task cannot hoard queued work. Correct for these task lengths.
- `visibility_timeout` 21,600 s (`celery.py:395`), above the 3 h 15 m hard limit (`:306-307`), with
  the orphan reaper covering dead workers (`backend/app/core/CLAUDE.md`). Correct; do not lower it.
- **F3 (High). No per-user fairness.** Every user import enters the GPU stage at
  `GPUPriority.USER_IMPORT` (`backend/app/tasks/transcription/dispatch.py:128,474`,
  `backend/app/core/queues.py:164-178`), and within one priority Redis lists are FIFO. A user who
  uploads 500 files (or a watch source that imports a folder) puts 500 runs ahead of every other
  user's single file; at 5-60 minutes per file that is days. CPU stages share the same shape
  (`CPUPriority.PIPELINE_CRITICAL`). See R2.

### 2.7 Things that serialise all users

Searched for process-wide locks, advisory locks and row locks on shared rows.

- Process-wide `threading.Lock`s in request paths (`core/settings_cache.py:51`,
  `legal/processing_gate.py:78`, `core/process_auth_settings.py:39`, `services/privacy/policy.py:508`,
  `auth/session.py:171`, `auth/lockout_store.py:96` and others) guard in-memory dict reads and
  writes only; none is held across a database or network call. On a cache miss many threads may
  each read the same row once (a small stampede, bounded by the TTL). No finding.
- Advisory locks: the migration lock (startup only, `backend/app/db/migrations.py`), and a
  per-user lock when hiding files (`services/hidden_media_service.py:152`). No finding.
- `FOR UPDATE`: every site locks rows of one user or one object (login locks the caller's own
  refresh-token rows, `api/endpoints/auth/login.py:316-326`; trash, holds, clustering, purge rows
  per file or cluster). No shared row is locked per request.
- **F18 (Low).** With an instance-wide monthly LLM token limit set, every LLM call reserves
  against the single `instance-month` row of `llm_token_budget` with a conditional `UPDATE`
  (`services/llm_budget.py:254-263`), so concurrent LLM calls serialise on that row for the
  length of the reserve transaction. The transaction is short (one statement per counter, then
  commit), so the cost is microseconds per call; measure under the chat storm profile before
  acting. With both limits at 0 (the default) nothing is written.
- No settings row is written per request. `user.last_login_at` is written per login only
  (`api/endpoints/auth/login_completion.py:58`).

---

## 3. Fixes made, with red/green evidence

Red = the new test run against a `git archive HEAD` tree (`mktemp -d -t ot-redcheck.XXXXXX`) with
only the test files copied in; green = the same test on the fixed tree. Runs used
`scripts/run-backend-tests.sh --throwaway-db ... -m "integration or not integration"`.

| Commit | Test | Red (old code) | Green |
|---|---|---|---|
| C1 | `tests/unit/test_celery_fork_init.py::TestForkedChildLeavesParentConnectionsAlone` | `OperationalError: server closed the connection unexpectedly`; with the dispose deleted instead: `the child kept the inherited connection in its pool` | pass |
| C2 | `tests/unit/test_endpoint_session_lifetime.py::test_llm_status_dials_the_provider_outside_the_transaction`, `::test_summary_status_dials_the_provider_outside_the_transaction` | `the request transaction was held across the network call (llm_health_check): [('llm_health_check', True)]` (both) | pass |
| C3 | `tests/test_chat_endpoints.py::test_send_releases_the_request_transaction_before_the_reply_streams` | `[('provider_stream', True)]` | pass |
| C4 | `tests/api/test_websocket_concurrency.py::test_cookie_handshake_authenticates_off_the_event_loop`, `::test_first_message_handshake_authenticates_off_the_event_loop`; `::test_the_mounted_ws_route_is_still_the_endpoint` | `auth ran on the event loop` (both); the route test was proved red against the first draft of this fix, where the `@router.websocket` decorator ended up on the new helper (the dead-code ratchet hook caught it at commit time; the in-process WebSocket tests mount the endpoint directly and could not) | pass |
| C5 | `tests/api/test_websocket_concurrency.py::test_a_broadcast_survives_a_user_connecting_mid_fan_out`, `::test_a_broadcast_survives_sockets_joining_and_leaving_mid_fan_out`, `::test_a_personal_message_reaches_every_tab_when_one_closes_mid_send` | `RuntimeError: dictionary changed size during iteration`; the departed user's slot walked into the newcomer; the next tab got nothing | pass |
| C6 | `tests/api/test_db_pool_metrics.py` | the `db_pool_*` series are absent from `/metrics` | pass |
| C7 | `tests/unit/test_load_scenario_routes.py` | new code, so no old-code red; while it was written it failed on two real scenario defects (writes lost the CSRF header after a re-login cleared the cookie jar; a declared route no task used) | pass |

Neighbouring suites re-run green with the fixes: the chat endpoint suite, the LLM status and
summary-status API suites, the WebSocket revocation suites, `test_handler_blocking_io.py`,
`test_handler_and_ws_hardening.py`, `test_llm_settings_unavailable.py` (its handler doubles now
offer `close()`), `test_celery_reliability.py` (its string check for `dispose()` was replaced by
the real fork test). `scripts/audit-tests.py tests`: 0 open findings.

---

## 4. The concurrent-user load test

Files: `scripts/perf/load/locustfile.py`, `scripts/perf/load/scenario.py` (route table and target
guard, no Locust import), `scripts/perf/load/requirements.txt` (Locust 2.47.0 and its dependencies,
exact pins, own venv), `scripts/perf/load/README.md` (the runbook), the "Concurrent users" section
of `docs-site/docs/developer-guide/performance-testing.md`, and the smoke test
`backend/tests/unit/test_load_scenario_routes.py`.

Locust docs: "Writing a locustfile" (https://docs.locust.io/en/stable/writing-a-locustfile.html),
"Configuration" and custom arguments (https://docs.locust.io/en/stable/configuration.html,
`events.init_command_line_parser`), "Increasing the request rate" / `--processes`
(https://docs.locust.io/en/stable/increase-performance.html), and "Retrieve test statistics in CSV
format" (`--csv`, `--csv-full-history`).

- **Isolation.** `--fresh NAME` is required; the port comes from `.fresh/NAME.offset` (as
  `scripts/seed-query-review.sh` resolves it), offset 0 and the dev stack's ports (5174-5180)
  are refused, and a `--host` that is not that stack's backend is refused. No override, no
  remote mode.
- **Identities.** The scale seed (`backend/app/scripts/scale_dataset`, `perf-N@loadtest.invalid`,
  password `loadtest-password`): one `Administrator` (perf-1) and one `HeavyOwner` (perf-2) user
  each, the rest `OrdinaryUser`s spread over perf-3..perf-<seeded users>. Login uses the SPA's
  own cookie flow and sends `X-CSRF-Token` on writes.
- **Mix** (weights; think time 3-10 s): gallery page 1 (10), later pages (4), a filtered gallery
  (search / tag / speaker / sort, 6), the filter panel (`metadata-filters`, `owners`, tags,
  collections, 2), open a file with transcript and comments (6), search (3), the speakers page
  (2), the task list (2), and the `write`-tagged comment (create, then delete) and upload (prepare,
  delete, purge, never complete, so no transcription reaches the GPU workers) at 1 each. Admins
  also open the statistics pages.
- **Failures are labelled by cause** (`rate limited (429)`, `session lost (401)`, `HTTP <code>`,
  `no response`) so a rate limiter or a lost session is never read as slowness.
- **What the smoke test does not prove.** It imports the locustfile against a stand-in for
  `locust` (the real package must not load in the backend test process). The real import was
  checked by hand: `locust -f scripts/perf/load/locustfile.py --list` lists the three user
  classes with the pinned Locust 2.47.0, and `--fresh nope` exits 2.
- **Not run.** The lead runs it on the `perf1337` stack after the other #1337 lanes merge. The
  exact commands are in the README.

What to look at first, given this audit: the chat and search paths (F4) under concurrency, the
`db_pool_checked_out` gauge against `db_pool_capacity` (F1/F9), WebSocket reconnect storms, and
the backend container pinned at one core (F1).

---

## 5. Recommendations (not implemented: larger, riskier, or behaviour-changing)

In order. Each names the documented pattern it follows.

**R1. Run more than one API process (F1), after the load test shows the single process is the
limit.** Uvicorn "Deployment" (https://www.uvicorn.org/deployment/) supports `--workers N`, or
Gunicorn as process manager with `uvicorn.workers.UvicornWorker` (Gunicorn "Design: How many
workers?", https://docs.gunicorn.org/en/stable/design.html, suggests 2 x cores + 1 as a start).
Prerequisites, each a separate change:
1. Prometheus multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`, a `MultiProcessCollector` on the
   scrape path; https://prometheus.github.io/client_python/multiprocess/), since gauges and
   histograms are per process today.
2. A connection budget (R6): N processes x 60 is N x the API row.
3. Accept per-process caches: `settings_cache` (30 s), `process_auth_settings` (30 s), the search
   result cache, and slowapi's in-memory fallback become per process. WebSocket fan-out already
   works across processes (every process subscribes to the same channel).
4. Lifespan work (`backend/app/main.py` startup: migrations under the advisory lock, the
   `search_index_maintenance` dispatch) runs once per worker; make the dispatches idempotent or
   leader-only.

**R2. Per-user fairness for transcription (F3).** Lowest-risk first step: at dispatch, demote a
run's priority when its owner already has more than K runs queued (count `in_progress`/queued
`task` rows for the user; `USER_IMPORT` for the first K, a lower step after), so a newcomer's
single file overtakes a bulk import. A fuller answer is a per-user admission queue (runs wait in
Postgres; a releaser hands them to Celery round-robin by owner). Both change ordering, so they need
a product decision and a test that a bulk import of N files cannot delay another user's file by
more than K run lengths.

**R3. Release the request transaction before OpenSearch in search (F4).** The phase split already
used by `repair_indices` (`backend/tests/unit/test_endpoint_session_lifetime.py`, section 2): read
the collection filter and hidden files as plain data, `db.close()`, run the OpenSearch query and
masking, then reopen for anything that needs Postgres. Teach `scripts/session_lifetime_audit` the
search service's slow methods (`HybridSearchService.search`, `.count`, `.get_suggestions`) so the
next one is caught statically.

**R4. Guard async handlers against direct session use (F5).** Extend
`test_handler_blocking_io.py` with an AST rule: inside an `async def` route handler, a call on
`db` (or any parameter annotated `Session`) is an error unless it is passed to
`run_in_threadpool`. Then convert the handlers in the 2.2 table: most should simply become `def`
(their awaits are threadpool hops already), the rest move their DB work into one sync helper run
through `run_in_threadpool`, as C2 did for `summary-status`.

**R5. Cache the provider health check (F8 follow-up).** `/llm/status` from every tab still dials
the provider once per poll per tab. A process-wide result cache keyed by the resolved provider
config (base URL, model), TTL about 30 s, cuts that to one dial per config per TTL. It changes how
quickly a provider outage shows in the UI, so it needs a decision on the TTL.

**R6. A connection budget that holds (F9, F17).**
- Correct the comments in `docker-compose.yml` and `.env.example`: the worker pool is per process,
  and the formula is `sum over services of (pool_size + max_overflow) x processes`.
- For prefork workers, use `WORKER_DB_MAX_OVERFLOW=0`-like sizing or SQLAlchemy's `NullPool`
  (the docs recommend `NullPool` when an external pooler does the pooling), and put PgBouncer in
  transaction mode (https://www.pgbouncer.org/features.html) in front of Postgres for multi-process
  APIs. Note the app's `SET` on connect (`install_idle_in_transaction_timeout`) and advisory locks
  need session pooling or a role-level setting under transaction pooling.
- Make `pool_timeout` configurable (`DB_POOL_TIMEOUT`, default 30), so overload can fail fast.
- A unit test that reads the compose files and fails when the worst-case budget exceeds
  `PG_MAX_CONNECTIONS` minus a reserve.

**R7. Bounded, concurrent WebSocket fan-out (F11).** Send to each socket with a timeout and
concurrently (`asyncio.TaskGroup` or `gather` per batch, with `asyncio.timeout` per send), or give
each connection a bounded queue drained by its own writer task and drop or close a socket that
falls behind. Either keeps one slow client from delaying everyone; the queue variant also bounds
memory.

**R8. Document per-IP limits for shared-IP deployments (F12).** In the deployment docs: set
`RATE_LIMIT_TRUSTED_PROXIES` to the reverse proxy so limits key on the real client, and raise
`RATE_LIMIT_AUTH_METHODS_PER_MINUTE` / `RATE_LIMIT_DIRECTORY_PER_MINUTE` when many users share one
egress IP.

**R9. Small Redis/asyncio hygiene (F14, F15).** Reuse one db-0 publish client in
`_push_invalidation` (or `get_redis()`); keep the subscriber task in a module-level reference.

**R10. Measure the middleware stack (F16).** If the load test shows per-request overhead in the
middleware, convert the four `BaseHTTPMiddleware` classes to pure ASGI middleware (the pattern
`UploadBodyLimitMiddleware` already uses, `backend/app/middleware/upload_limit.py:91`).

---

## 6. Verification for whoever picks up a recommendation

- Every change: a test that fails on a `git archive HEAD` tree and passes after (root `CLAUDE.md`).
- R1, R6: the load test at 100 / 500 / 1,000 users before and after, same snapshot and host load,
  recording the README's table; `pg_stat_activity` peak below `max_connections` minus reserve.
- R2: a test that dispatches N runs for user A then one for user B and asserts B's position.
- R3: extend `test_endpoint_session_lifetime.py` with a search case using `TrackedRequestSession`.
- R7: a test with one peer whose send never completes and a second peer that must still receive
  within the timeout.
