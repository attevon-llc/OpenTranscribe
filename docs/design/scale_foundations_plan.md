# Scale foundations: connection lifecycle, process model, probes, query cost (v0.7.0 Phase B)

Status: **PLAN ONLY.** Written 2026-11-08 on `v070/scale-design` (base `cleanup-wave-af19`,
`37b42d628`). No application code changes with this file. Each lane in section 9 can be run by
a fresh agent from this document alone.

Goal: the open-source app holds up with **hundreds of concurrent users on one machine**, scales out
by adding API processes and replicas, and is a sound base for a managed offering aimed at thousands
to 10,000 users. Every decision below follows a documented pattern (cited with URL and section). A
decision that needs custom code says why the documented pattern does not cover it.

Inputs read for this plan:

| Input | What it contributed |
|---|---|
| `~/ot-cleanup-work/1337/perf/m2-load/RESULTS.md` (M2) | Findings 1-15, tool bugs T1-T10, "Final-run plan". Code at `90b744bbf`. |
| `~/ot-cleanup-work/concurrency-audit/REPORT.md` (audit) | H1-H5, M1-M12, L1-L9, scaled notes. Code at `890b01fa5`. |
| `docs/design/concurrency_foundations_plan.md` | F1-F18, R1-R10 |
| `docs/design/migrate_job_plan.md` | One-shot migrate step; startup work elected (shipped) |
| `docs/design/1337_query_review_plan.md` + `~/ot-cleanup-work/1337/perf/m1/RESULTS.md` (M1) | Query inventory, L5 index candidates, decision rules 5.1-5.4 |
| `~/ot-cleanup-work/V070_COMPLETION_PLAN.md` | Phase B scope and lane names |
| `CLAUDE.md`, `backend/CLAUDE.md`, `backend/app/{core,db,api}/CLAUDE.md` | Conventions, gates, startup table |
| Code at `37b42d628` | Every `file:line` below, unless marked "(M2)" (the M2 tree) |

Line references to the M2 report are given as "M2 #N" (its finding number). Paths are relative to
the repo root.

---

## 0. Summary

### 0.1 What has changed since the inputs were written

Read this first: several input findings are already fixed, and one is mis-stated.

| Input item | State at `37b42d628` | Evidence |
|---|---|---|
| Audit H1 (chat streams on threads) | **Fixed**: async provider streaming, chat's own `CapacityLimiter`, Redis-backed global stream cap | `ac3363add`, `12e7fb753`; `backend/app/core/threadpool.py:104-158` |
| Audit H2 (pool/thread cross-wait) | **Fixed in shape**: `UVICORN_LIMIT_CONCURRENCY` 512, threads `max(40, C, L-C+1)`, `DB_POOL_TIMEOUT_SECONDS` 10 | `cf05aef7f`; `threadpool.py:56-67`; `backend/app/db/base.py:29`. M2 then showed the remaining failure mode (M2 #3). |
| Audit H3, H4, H5, M1-M12, L1-L9 | **Fixed** | merges `b2479d99a`, `ccbb7f1e1`, `d034bb81b`, `fccc9e35e` |
| M2 #1 (embedding repair floods Redis) | **Fixed** (P0) | merge `0c207e0a2` |
| M2 #2 prerequisite "migrations and startup elected" | **Done** | merge `9395004e2`; `backend/app/db/CLAUDE.md` "Startup work and replicas" |
| M2 #6 part 1 (unpooled `psycopg2.connect` at login) | **Fixed**: `engine.raw_connection()` | `backend/app/auth/direct_auth.py:28-38`. Parts 2 and 3 remain (section 5.6). |
| M2 #11 (thumbnail backfill on every boot) | **Half fixed**: now elected once per boot window, but still re-walks the library every boot and remembers no failures | `backend/app/startup/maintenance.py:378-395`, `backend/app/tasks/thumbnail_migration.py:231-271` |
| M2 #13 "G13 fix not landed" | **Mis-stated.** The `NOT EXISTS` rewrite landed in `938c9b726`, which is in the M2 tree. The `/api/tags` cost M2 saw is a different statement: `accessible_usage_counts` (`backend/app/services/tag_collisions.py:118-128`, `count(DISTINCT media_file_id) ... GROUP BY tag_id` over the accessible-file subquery). | `git merge-base --is-ancestor 938c9b726 90b744bbf` is true |
| M2 #14 (unfiltered `GET /api/speakers`) | **Fixed by #1337 L2** (read-only, paged) | merge `b737fa9c1`. Not driven by any persona, so the final pass must add it (lane G). |
| #1337 L1-L4 | **Merged** | `3c8e2ef11`, `b737fa9c1`, `3046deff2`, `fcdc5eca4` |
| #1337 L5 (measured indexes) | **Not done**: no `backend/app/db/perf_indexes.py`; last revision `v469` | `ls backend/alembic/versions` |
| M2 #12 location | `search_maintenance_task.py:254` now points at the `missing_facts_files` COUNT (`:252-263`), not the "latest task" correlated subquery, which lives in `backend/app/services/search/index_retry.py:89-101`. Both are measured in lane D. | |

### 0.2 What is still open, and the lane that owns it

| # | Problem | Evidence | Lane |
|---|---|---|---|
| 1 | A request holds its pooled connection, idle in a transaction, from authentication until the response is sent | M2 #3; `dependencies.py:62`, `lifecycle_gates.py:361-365` | A |
| 2 | Pool exhaustion answers 500 after a 10 s wait, logged as a ~200-line traceback | M2 #3, #7 | A |
| 3 | No `statement_timeout` and no request deadline: abandoned work keeps holding the pool | M2 #5 | A |
| 4 | One API process is the ceiling (14-20 req/s, ~1.1-1.4 cores) | M2 #2 | P (profile), B |
| 5 | Prometheus registry is single-process; the pool gauge is a custom collector | `backend/app/core/CLAUDE.md:287`; `backend/app/core/db_metrics.py:146-187` | B |
| 6 | `/health/ready` queues behind user traffic for a pool connection | M2 #9; `backend/app/main.py:461-470, 521-526` | C |
| 7 | `GET /api/speaker-profiles/profiles` N+1 over privacy scopes (~3,900 statements) | M2 #4 | D |
| 8 | Thumbnail backfill re-walks the library every boot | M2 #11 | D |
| 9 | Background statements top `pg_stat_statements`; `/api/tags` usage counts | M2 #12, #13 | D |
| 10 | #1337 L5 measured indexes not shipped | 1337 plan 6.2 | D2 |
| 11 | #920 caching rule not written down | issue #920 acceptance 1 | D |
| 12 | Login holds a pooled connection across PBKDF2, indexes two audit events synchronously, and turns infrastructure errors into 401/500 | M2 #6; `direct_auth.py:213-265`, `login.py:586-592` | E |
| 13 | WebSocket handshake fails as "Authentication error" on pool exhaustion; WS echo latency is event-loop lag | M2 #8; `backend/app/api/websockets.py:745-778` | F |
| 14 | One bad stored citation fails a whole conversation (500) | M2 #15; `backend/app/api/endpoints/chat/messages.py:160-167` (`MessageList` response; M2 cited `:192`) | F |
| 15 | The SPA treats 503 like any error; no bounded retry with `Retry-After` | `frontend/src/lib/axios.ts:239-260` handles 429 only | A2 |
| 16 | Load tool false-pass, persona bugs, seed path, sequences, no sizing guide | M2 T1-T10, #10; issue #1174 | G |

### 0.3 Lanes at a glance

| Lane | Branch | Wave | Depends on |
|---|---|---|---|
| P: profile the API under load (measurement only) | none (writes to `~/ot-cleanup-work/scale/profile/`) | 0 | nothing |
| A: connection lifecycle | `v070/scale-a-connection` | 1 | nothing |
| C: liveness and readiness | `v070/scale-c-probes` | 1 | nothing |
| D: query cost | `v070/scale-d-queries` | 1 | P output (to order its items) |
| G: load tool and sizing guide (#1174) | `v070/scale-g-loadtool` | 1 | nothing |
| B: API process model | `v070/scale-b-process` | 2 | A, C merged (budget counts A's and C's settings) |
| E: login path | `v070/scale-e-login` | 2 | A merged (503 mapping) |
| F: WebSocket and chat | `v070/scale-f-ws-chat` | 2 | A, B merged (metric conventions) |
| A2: SPA honours 503 + `Retry-After` | `v070/scale-a2-spa-retry` | 2 | A merged (the 503 contract) |
| D2: measured indexes (L5) | `v070/scale-d2-indexes` | 2 | D merged and its EXPLAIN evidence |
| Final load pass (task #37) | none | 3 | waves 1-2 swept and tagged |

---

## 1. Documentation consulted (cite these; do not paraphrase from memory)

Pinned versions (backend venv and `backend/requirements.txt`): FastAPI 0.136.3, Starlette 1.10.0,
uvicorn 0.54.0, SQLAlchemy 2.0.55, anyio 4.15.1, prometheus_client 0.26.0, PostgreSQL 17.5.

| Topic | Source (URL, section) | What it says that this plan relies on |
|---|---|---|
| Session lifecycle | SQLAlchemy 2.0, *Session Basics*, "When do I construct a Session, when do I commit it, and when do I close it?" https://docs.sqlalchemy.org/en/20/orm/session_basics.html#session-faq-whentocreate | Create a Session at the start of a web request and close it at the end; "keep transactions short"; manage the lifecycle outside the functions that deal with data. |
| `Session.close()` | same page, "Closing" https://docs.sqlalchemy.org/en/20/orm/session_basics.html#closing | Expunges all objects, releases connection resources, and the session "may be used again". |
| Expiry on commit and rollback | SQLAlchemy, *State Management*, "When to Expire or Refresh" https://docs.sqlalchemy.org/en/20/orm/session_state_management.html#when-to-expire-or-refresh | "whenever Session.commit() or Session.rollback() is called, all objects within the Session are expired." |
| Detached objects | same page, "Quickie Intro to Object States" | Detached objects "are safe to use normally, except that they have no ability to load unloaded attributes or attributes that were previously marked as expired". |
| Re-attaching | `Session.add()` API https://docs.sqlalchemy.org/en/20/orm/session_api.html#sqlalchemy.orm.Session.add and `Session.expunge()` | A detached instance passed to `add()` becomes persistent without SQL. |
| Pool timeout, pre-ping, events | SQLAlchemy, *Connection Pooling* https://docs.sqlalchemy.org/en/20/core/pooling.html: "Disconnect Handling - Pessimistic", "Pool Events", "Using Connection Pools with Multiprocessing or os.fork()", "Switching Pool Implementations" (NullPool with an external pooler); `create_engine(pool_timeout=)` https://docs.sqlalchemy.org/en/20/core/engines.html#sqlalchemy.create_engine.params.pool_timeout | A checkout beyond capacity blocks up to `pool_timeout`, then raises `sqlalchemy.exc.TimeoutError`; `checkout`/`checkin` events exist for instrumentation. |
| Dependencies with yield, scope | FastAPI, *Dependencies with yield*, "Early exit and scope" https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/ | Default `scope="request"` runs exit code **after** the response is sent; `scope="function"` runs it after the path operation returns, **before** the response is sent. A `"request"` dependency cannot depend on a `"function"` one. |
| Sync handlers in a threadpool | FastAPI, *Concurrency and async / await* https://fastapi.tiangolo.com/async/ | `def` path operations and dependencies run in an external threadpool. |
| Server workers | FastAPI, *Server Workers - Uvicorn with Workers* https://fastapi.tiangolo.com/deployment/server-workers/ | Use `uvicorn ... --workers 4` (or `fastapi run --workers`); "when running on Kubernetes you will probably not want to use workers and instead run a single Uvicorn process per container". Gunicorn is not mentioned any more; the FastAPI maintainer prefers `uvicorn --workers` ("fewer moving parts", fastapi/fastapi PR #11855). |
| Deployment concepts | FastAPI, *Deployments Concepts* https://fastapi.tiangolo.com/deployment/concepts/: "Replication - Processes and Memory", "Memory", "Previous Steps Before Starting" | Each worker process has its own memory; one-time steps (migrations) run once before the workers start. |
| One process per container | FastAPI, *FastAPI in Containers - Docker*, "Replication - Number of Processes" / "One Load Balancer - Multiple Worker Containers" https://fastapi.tiangolo.com/deployment/docker/ | In a cluster, replicate containers; inside one container use `--workers` only when there is no cluster. |
| uvicorn options | uvicorn deployment docs https://uvicorn.dev/deployment/ and the pinned CLI (`uvicorn/main.py:61` `auto_envvar_prefix="UVICORN"`; `--workers` defaults to `$WEB_CONCURRENCY`; `--limit-concurrency`, `--backlog`, `--timeout-keep-alive`, `--timeout-graceful-shutdown`, `--timeout-worker-healthcheck`) | `uvicorn.workers` is deprecated since 0.30 in favour of the `uvicorn-worker` package; the built-in supervisor restarts a dead or unresponsive worker (`uvicorn/supervisors/multiprocess.py:214-240`) and does a rolling restart on SIGHUP. `--reload` and `--workers` are mutually exclusive. Every CLI option can be set as `UVICORN_<OPTION>`. |
| Thread limiter | Starlette, *Thread Pool* https://www.starlette.io/threadpool/; anyio, *Working with threads* https://anyio.readthedocs.io/en/stable/threads.html ("Adjusting the default maximum worker thread count", "Context propagation"); anyio, *Synchronization*, "Capacity limiters" https://anyio.readthedocs.io/en/stable/synchronization.html#capacity-limiters | One default limiter (40 tokens) per event loop; a dedicated `CapacityLimiter` isolates a class of work; context variables are propagated into worker threads. |
| Middleware | Starlette, *Middleware*, "Pure ASGI Middleware" and the `BaseHTTPMiddleware` limitations https://www.starlette.io/middleware/#pure-asgi-middleware | Pure ASGI middleware is the recommended shape; `BaseHTTPMiddleware` runs the app in a child task. |
| Postgres timeouts | PostgreSQL 17, *Client Connection Defaults* https://www.postgresql.org/docs/17/runtime-config-client.html `#GUC-STATEMENT-TIMEOUT`, `#GUC-LOCK-TIMEOUT`, `#GUC-IDLE-IN-TRANSACTION-SESSION-TIMEOUT`, `#GUC-TRANSACTION-TIMEOUT` | `statement_timeout` aborts a statement after N ms; "Setting statement_timeout in postgresql.conf is not recommended because it would affect all sessions" (set it per role or per session instead). `transaction_timeout` exists in 17. |
| Connections | PostgreSQL 17, *Connections and Authentication* https://www.postgresql.org/docs/17/runtime-config-connection.html | `max_connections` minus `superuser_reserved_connections` (3). |
| `application_name` | libpq, *Parameter Key Words* https://www.postgresql.org/docs/17/libpq-connect.html#LIBPQ-CONNECT-APPLICATION-NAME | Shown in `pg_stat_activity.application_name`; lets a probe count one service's connections without asking the service. |
| SQLSTATE | PostgreSQL 17, *Appendix A. Error Codes* https://www.postgresql.org/docs/17/errcodes-appendix.html; psycopg2 `errors.QueryCanceled` https://www.psycopg.org/docs/errors.html | `57014 query_canceled` is what `statement_timeout` raises. |
| PgBouncer | PgBouncer, *Features*, "SQL feature map for pooling modes" https://www.pgbouncer.org/features.html | Transaction pooling "breaks client expectations of the server by design": `SET/RESET`, `LISTEN`, session advisory locks, `PREPARE`, `WITH HOLD` cursors: "Never". |
| Probes | Kubernetes, *Liveness, Readiness, and Startup Probes* https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/ | Liveness restarts the container; readiness removes it from Service endpoints; "Incorrect implementation of liveness probes can lead to cascading failures ... restarting of container under high load"; check back-end services in readiness, not liveness. |
| Compose health | Docker Compose, *Control startup order* https://docs.docker.com/compose/how-tos/startup-order/ and the `healthcheck` reference https://docs.docker.com/reference/compose-file/services/#healthcheck | `service_healthy` gates dependents; Compose does not restart an unhealthy container. |
| Prometheus multiprocess | prometheus_client, *Multiprocess Mode (e.g. Gunicorn)* https://prometheus.github.io/client_python/multiprocess/ | Set `PROMETHEUS_MULTIPROC_DIR` from a startup shell script; wipe it between runs; custom collectors and `set_function` do not work; gauge `multiprocess_mode` `all/min/max/sum/mostrecent` and `live*`; `mark_process_dead(pid)` clears `live*` gauges only. |
| 503 + Retry-After | RFC 9110 §15.6.4 "503 Service Unavailable" https://www.rfc-editor.org/rfc/rfc9110#section-15.6.4 and §10.2.3 "Retry-After" https://www.rfc-editor.org/rfc/rfc9110#section-10.2.3 | 503 means temporary overload; `Retry-After` tells the client when to try again. |
| Profiling | py-spy README https://github.com/benfred/py-spy: "How do I run py-spy in Docker?", "How can I profile subprocesses?", "How does GIL detection work?", "When do you need to run as sudo?"; Docker `run --pid` https://docs.docker.com/reference/cli/docker/container/run/#pid | Needs `SYS_PTRACE` in a container; `--subprocesses`, `--gil`, `--idle`; joining a container's PID namespace with `--pid=container:<id>` avoids host sudo. |
| File locks | Python `fcntl.flock` https://docs.python.org/3/library/fcntl.html#fcntl.flock | Advisory whole-file lock between processes on one host. |
| Outbox | microservices.io, "Pattern: Transactional outbox" (already the basis of `backend/app/services/audit_outbox.py:1-24`) | Record the event in the same transaction; a relay delivers it. |
| Load tool | Locust docs (already cited in `concurrency_foundations_plan.md` section 4) | |

Rule for every lane brief: before using or changing a dependency, read its docs for the pinned
version (user guide, configuration page, changelog) and cite the pages read in the commit body.

---

## 2. The request's connection lifecycle (Lane A)

### 2.1 Problem

A request checks out a pooled connection at its first statement (the user lookup in
authentication) and holds it, idle in a transaction, until FastAPI runs `get_db`'s `finally`
after the response has been sent. Under CPU saturation most of that time is spent waiting for
the GIL, not in SQL. When all 60 connections are held, the next request waits
`DB_POOL_TIMEOUT_SECONDS` (10 s) and fails with HTTP 500, and each 500 is logged as a ~200-line
chained traceback, which costs more CPU in the already saturated process.

### 2.2 Evidence

- M2 #3: 60/60 API connections idle in transaction, median 1.7 s (p90 10.8 s) since their last
  statement, while Postgres had 1-5 active backends (`runs/08/pgprobe.csv`); 1,900-13,800
  `QueuePool limit of size 20 overflow 40 reached` per run.
- Checkout points: `backend/app/api/endpoints/auth/dependencies.py:62` (user lookup),
  `:188` (`token_service.access_token_revoked(payload, db)` DB fallback),
  `backend/app/api/endpoints/auth/lifecycle_gates.py:361-365` (banner `auth_config` read on
  every request), `dependencies.py:234-246` (approval, proxy identity gates on the same `db`).
- Session close: `backend/app/db/base.py:56-61` (`get_db`, default `scope="request"`), so per the
  FastAPI docs the close runs after the response is sent.
- 500 path: `dependencies.py:94-115` logs `Error retrieving user` and re-raises; there is no
  handler for `sqlalchemy.exc.TimeoutError` in `backend/app/main.py:361-410`, so Starlette's
  `ServerErrorMiddleware` answers 500 and the exception travels through the four
  `BaseHTTPMiddleware` layers (`middleware/csrf.py`, `audit.py`, `robots.py`, `observability.py`),
  which is the ExceptionGroup traceback of M2 #7 (718,590 log lines in run 08).
- Abandoned work: M2 #5: `profiles.list` ran 108-266 s against a 60 s client timeout; the longest
  API transactions were 120-292 s of continuous short statements, so the 300 s
  `idle_in_transaction_session_timeout` backstop (`backend/app/db/migration_connection.py`,
  installed at `base.py:43`) never fired. No `statement_timeout` is set anywhere.

### 2.3 Standard pattern

- SQLAlchemy: a Session per request, **transactions kept short**; a Session with no transaction
  in progress holds no connection (session_basics, "When do I construct a Session..."). The fix
  is to end the authentication transaction as soon as authentication is decided, not to change
  the Session's lifetime.
- SQLAlchemy object states: `commit()` expires every object in the Session; an object
  **expunged first** is not expired and keeps its loaded attributes; `add()` re-attaches it
  without SQL.
- Overload: answer **503 with `Retry-After`** (RFC 9110 §15.6.4, §10.2.3), quickly. The pool's
  own bound (`pool_timeout`) is the signal; the Starlette/FastAPI exception-handler mechanism is
  the documented way to map an exception to a response (handled inside `ExceptionMiddleware`, so
  it never reaches the `BaseHTTPMiddleware` layers as an exception).
- Server-side bound per statement: `statement_timeout` **per session for the API role only**
  (PostgreSQL docs advise against setting it in `postgresql.conf`). Applied the same way the
  idle-in-transaction backstop already is: a pool `connect` event `SET`
  (`install_idle_in_transaction_timeout`, `base.py:40-43`), which survives proxies that reject
  libpq `options`.
- Request deadline: no library in the stack ships one (uvicorn has no request timeout; Starlette
  has none; gunicorn's `--timeout` is a worker-silence kill). A sync handler running in a thread
  cannot be cancelled from the event loop. The smallest correct design is a deadline in a
  context variable (anyio propagates context into worker threads: anyio threads docs, "Context
  propagation") checked at every statement by a SQLAlchemy `before_cursor_execute` event. This
  is custom code, justified because the documented mechanisms bound one statement
  (`statement_timeout`) or one transaction (`transaction_timeout`), not one request made of many
  short transactions.

Considered and rejected: switching `get_db` to `Depends(get_db, scope="function")`. The pinned
FastAPI (`fastapi/routing.py:115-121`) closes function-scoped dependencies after
`serialize_response` but before `await response(...)`, so the gain is only the send phase and
background tasks. The dependency cache key includes the scope (`fastapi/dependencies/models.py:63-71`),
so a mixed migration of 593 handlers would give some requests two Sessions, and a request-scoped
dependency cannot depend on a function-scoped one. Revisit only if lane B's hold-time histogram
(section 3) shows send-phase holds.

### 2.4 Design

1. **End the authentication transaction (keep the user).** New helper in
   `backend/app/db/session_utils.py`:

   ```python
   def end_read_transaction(db: Session, *keep: object) -> None:
       """Commit the current (read) transaction and return its connection to the pool.

       Objects in ``keep`` are expunged first, so the commit does not expire them, then
       re-attached with ``add()`` (no SQL). Their unloaded relationships lazy-load later
       in a new transaction, on the same Session.
       """
   ```

   Called once at the end of `get_current_active_user` (after the last gate,
   `dependencies.py:246`) with `current_user`, and at the end of `get_optional_current_user`
   when it returns a user. `get_current_user` alone (used by the session probe and a few
   routes) calls it too, at `dependencies.py:202`, because some routes depend on it directly.
   Commit, not rollback: it is a superset (anything a gate wrote is kept) and in the test
   harness a commit restarts the savepoint (`backend/tests/conftest.py:689-696`) instead of
   discarding test-seeded rows. Guard: `assert not db.dirty` for the kept objects before the
   expunge (an auth path that modifies the user must say so explicitly).

2. **Banner gate without a per-request query.** `_read_banner_state` (`lifecycle_gates.py:350-390`)
   reads the two `auth_config` rows through the existing process-level cache
   (`backend/app/core/process_auth_settings.py`, 30 s TTL, primed on write by
   `AuthConfigService.set_config`). This is the repo's established staleness contract for auth
   configuration across processes (`AUTH_CONFIG_CACHE_SECONDS`), not a new cache, and it is not
   one of the three policy planes issue #920 forbids caching (redaction, quarantine, share
   scope). Keep the existing "last successful read" fallback.

3. **503 for pool exhaustion.** New module `backend/app/core/db_overload.py`:
   - `async def handle_db_overload(request, exc)` for `sqlalchemy.exc.TimeoutError` (pool
     timeout), `RequestDeadlineExceeded` (below), and `sqlalchemy.exc.OperationalError` whose
     `orig` is `psycopg2.errors.QueryCanceled` (SQLSTATE 57014, `statement_timeout`).
     Everything else re-raises (stays a 500).
   - Response: 503, `{"detail": "Server busy, try again shortly", "code": "server_busy"}`,
     header `Retry-After: <DB_OVERLOAD_RETRY_AFTER_SECONDS>` (default 2).
   - Logs **one** WARNING line per occurrence: route template, cause class, pool
     `checkedout()`/capacity, no traceback. Increments a counter
     `api_overload_responses_total{cause}` (bounded label: `pool_timeout`, `deadline`,
     `statement_timeout`). Lane B gives the counter its multiprocess mode.
   - Registered in `backend/app/main.py` beside `handle_app_error` (`main.py:371-385`).
   - `dependencies.py:94-96`: do not log `Error retrieving user` for these three classes (the
     handler logs once).
4. **Shorter pool wait.** `DB_POOL_TIMEOUT_SECONDS` default 10 -> **5**
   (`backend/app/core/config.py`, `.env.example:212`). With a 503 answer, a long wait only holds
   a thread and a socket. The final pass may tune it; record the value used.
5. **`statement_timeout` for the API process only.** New `DB_STATEMENT_TIMEOUT_MS` (coded default
   `0` = off, so Celery workers, `python -m app.startup.migrate` and scripts are unaffected;
   `docker-compose.yml` backend `environment:` sets `${DB_STATEMENT_TIMEOUT_MS:-30000}`).
   `install_statement_timeout(engine, ms)` in `backend/app/db/migration_connection.py`, beside
   `install_idle_in_transaction_timeout`, called from `base.py:43`. A route that legitimately
   needs longer (none known; M2 found no long single API statement) uses
   `SET LOCAL statement_timeout` inside its own transaction and documents why.
6. **Request deadline.** New `backend/app/db/request_deadline.py`:
   - `_deadline: ContextVar[float | None]`, `RequestDeadlineExceeded(Exception)`.
   - A `before_cursor_execute` listener on the API engine: when a deadline is set and
     `time.monotonic()` is past it, raise `RequestDeadlineExceeded` (the statement never runs;
     the Session rolls back on close as usual).
   - New pure ASGI middleware `backend/app/middleware/request_deadline.py` sets the deadline
     to `now + REQUEST_DEADLINE_SECONDS` (default **55**: under Locust's and most clients' 60 s,
     far under nginx's `proxy_read_timeout 300s`, `frontend/nginx.conf:20`) for `http` scopes
     only. Exempt, by route template list in the module with a one-line reason each: the upload
     routes (`middleware/upload_limit.py` already names them), SSE/streaming routes (chat send,
     regenerate, edit, `download_stream`, `bulk_export_stream`; they already close the request
     session before streaming and use their own `session_scope`), and `/metrics`.
   - Added in `main.py` as the innermost `add_middleware` call before the routing, i.e. added
     **first** (Starlette: last added = outermost, see the comment at `main.py:349-357`), so the
     deadline covers only the app's own work. Pure ASGI, so it adds no child task.
   - Not a cancellation: a handler busy in Python with no statements runs on. That is accepted;
     the profile (lane P) shows whether any handler is CPU-bound for seconds without SQL.
7. **Docs.** `backend/app/db/CLAUDE.md` (new section "Request connection lifecycle": the three
   rules: auth ends its transaction; never hold a transaction across non-DB I/O; overload is a
   503), `backend/app/core/threadpool.py` docstring (pool wait now 5 s, the 503 path),
   `.env.example`, CHANGELOG "Changed"/"Fixed".

### 2.5 Risks

| Risk | Mitigation |
|---|---|
| A handler mutates `current_user` and relied on it being in the auth transaction | It is re-attached to the same Session; changes are tracked and committed by the handler as before. The `assert not db.dirty` guard catches an auth path that wrote to it. |
| A handler reads a relationship of `current_user` | Lazy load works (persistent again); it opens a new transaction, which is the point. |
| Objects other than the user loaded during auth (e.g. by `_authenticate_external_token`, `dependencies.py:146`) are expired by the commit | Only the user is returned to handlers; the cloud seam's external verifier returns a User, so pass that one to `end_read_transaction` too. Test both paths. |
| Test suite: a test that seeded rows without committing loses them at the auth commit | It does not: commit releases the savepoint into the outer transaction and the listener opens a new one. The full API suite in the wave sweep is the proof. |
| `statement_timeout` cancels a legitimately slow API statement | 30 s is 10x the slowest API statement M2/M1 saw after #1337; the 503 is logged with the route so it is found. |
| The deadline aborts work a client is still waiting for | 55 s is under every client's own timeout in this stack; the exempt list covers streams and uploads. |
| A pool timeout inside a broad `except Exception` becomes a 401/500 before reaching the handler | Lane A greps `backend/app/api` and `backend/app/auth` for `except Exception` around session use on the request path and re-raises the three overload classes; login is lane E (`direct_auth.py:256-258`, `login.py:586-592`). |

### 2.6 Test strategy (each must fail on a `git archive HEAD` tree first; root `CLAUDE.md`)

Use the `ot-redcheck.` pattern from root `CLAUDE.md` for every red run.

1. `backend/tests/unit/test_auth_releases_connection.py`: through `TestClient`, a route that
   depends on `get_current_active_user` and records `db.in_transaction()` and
   `engine.pool.checkedout()` at handler entry (a test-only router mounted in the test). Red:
   `in_transaction() is True`. Green: `False`, and `current_user.email` readable with zero
   statements (`backend/tests/query_budget.py:52` `count_statements`). Second case: the banner
   gate issues no `auth_config` statement on a warm cache, and does after
   `AuthConfigService.set_config` primes it with a new value.
2. `backend/tests/api/test_db_overload_503.py`: build an engine with `pool_size=1,
   max_overflow=0, pool_timeout=0.1`, override `get_db`, hold its only connection, call an
   authenticated route. Red: 500. Green: 503, `Retry-After: 2`, body code `server_busy`, exactly
   one log record (caplog) with no `exc_info`. Same for `QueryCanceled` (`SET LOCAL
   statement_timeout = 1; SELECT pg_sleep(1)` on the real test Postgres) and
   `RequestDeadlineExceeded`. Negative control: an unrelated `OperationalError` stays 500.
3. `backend/tests/unit/test_statement_timeout.py`: a connection from the API engine with
   `DB_STATEMENT_TIMEOUT_MS=1500` reports `SHOW statement_timeout` = `1500ms`; with 0 it is `0`.
4. `backend/tests/unit/test_request_deadline.py`: with the deadline in the past, the next
   statement raises `RequestDeadlineExceeded` and Postgres saw no statement
   (`count_statements` == 0); the contextvar is visible inside `run_in_threadpool`; exempt
   routes get no deadline (a table test over the exempt list, which fails if a listed route
   template no longer exists in `app.routes`, so the list cannot rot).
5. Guard `backend/tests/unit/test_overload_classes_not_swallowed.py`: AST scan of
   `backend/app/api` and `backend/app/auth` for `except Exception` blocks that contain a
   `db.`/`Session` call on the request path and do not re-raise the overload classes, with an
   allowlist file carrying a written reason per entry (same discipline as `scripts/audit-tests.py`).
   Needs a must-fire and a must-stay-clean case.

Targeted commands (lane runs only these, plus static checks on touched files):

```bash
cd backend
../scripts/run-backend-tests.sh --throwaway-db tests/unit/test_auth_releases_connection.py \
  tests/api/test_db_overload_503.py tests/unit/test_statement_timeout.py \
  tests/unit/test_request_deadline.py tests/unit/test_overload_classes_not_swallowed.py -n 1
venv/bin/ruff check <touched files> && venv/bin/mypy <touched files>
python3 ../scripts/audit-tests.py tests      # 0 open findings before reporting
```

### 2.7 Verification (final pass)

- F05 steady-300 mixed and F11 spike mixed: **0** responses with status 500 whose backend log
  line is a pool timeout (M2 run 07: 1,511-1,657; spike: 1,510). Overload appears as 503s with
  `Retry-After`; backend log lines per 503 = 1 (M2 run 08: ~50 lines per error).
- pg probe: API connections idle in transaction > 1 s, mean, lower than M2 run 07 (51/60) at the
  same load; longest API transaction <= 60 s (M2: 120-292 s).
- F07 ramp read-only: no request's server time above 60 s in `access_stats.csv`.

---

## 3. API process model (Lane P, then Lane B)

### 3.1 Problem

The whole API runs in one Python process: one event loop and one GIL serve 593 sync handlers,
26 async handlers, every WebSocket and every SSE stream. M2 found the database mostly idle while
the API process sat at about one core.

### 3.2 Evidence

- M2 #2: throughput flat at 14-20 req/s from 50 people up, in every mix; backend CPU 107-140%
  mean, Postgres 30-68%; requests with 2-5 statements took 5-6 s server-side at 300 people.
- One process in prod (`backend/Dockerfile.prod:555-556` `CMD ["uvicorn", "app.main:app", ...]`,
  no `--workers`) and dev (`docker-compose.override.yml:121`, `--reload`).
- Single-process metric assumption: `backend/app/core/CLAUDE.md:287`; custom pool collector
  `backend/app/core/db_metrics.py:146-187` (`REGISTRY.register(_PoolCollector(...))`), which
  multiprocess mode does not support; queue page uses a private registry
  (`backend/app/api/endpoints/metrics.py:44-50`).
- Precedent in this repo for multiprocess metrics: `backend/app/core/worker_metrics.py:26-40,
  200-225, 360-391` (Celery prefork, `livemax` gauges, `mark_process_dead`).
- Startup steps: all shared ones are elected (`backend/app/db/CLAUDE.md:358-386`). The
  per-process ones that matter with N workers in **one container**:
  `_provision_native_diarizer` (`main.py:153`) writes a shared models directory, "marker-
  idempotent" but not safe against N concurrent first-boot exports; PII and reranker warm-ups
  (`main.py:218-219`) cost memory per process.
- Memory: backend container RSS 1.04 GB at smoke, 1.80 GB at steady-100 (M2 `runs/02`, `runs/09`
  `samples.csv`, `mem_bytes:backend`).
- WebSocket fan-out across processes **already works**: every process runs one subscriber on
  the Redis channel `websocket_notifications` (`backend/app/api/websockets.py:430-470`,
  subscribe at `:443-444`); publishers go through Redis (`:473-485`,
  `backend/app/utils/websocket_notify.py`). Chat's global stream cap is Redis-backed
  (`12e7fb753`), slowapi limits are Redis-backed (audit "Verified correct"). Per-process state
  that stays per process: `settings_cache` and `process_auth_settings` (30 s TTL), the per-user
  WebSocket cap (`websockets.py:776`, `manager.open_sockets(user.id)`), and slowapi's in-memory
  fallback.

### 3.3 Standard pattern

- Inside one container with no orchestrator: `uvicorn --workers N` (FastAPI *Server Workers*;
  uvicorn's supervisor restarts dead workers and supports rolling restart on SIGHUP). Not
  gunicorn: `uvicorn.workers.UvicornWorker` is deprecated (`uvicorn/workers.py:17-21` warns),
  FastAPI's docs no longer describe gunicorn, and `--limit-concurrency` is not supported under
  gunicorn (uvicorn deployment docs).
- In Kubernetes or any orchestrator: **one process per container**, replicas for scale (FastAPI
  *Server Workers* and *Docker* pages). The image must support both: `UVICORN_WORKERS` unset/1
  gives one process.
- One-time work before the workers: the migrate step (already shipped, `migrate_job_plan.md`).
- Metrics: prometheus_client multiprocess mode, directory set and wiped by a startup shell
  script, `MultiProcessCollector` on the scrape path, gauges with an explicit `multiprocess_mode`.

### 3.4 Lane P: profile first (measurement only, before wave 1 code)

M2 could not profile (`ptrace_scope=1`, py-spy needed sudo). The documented container method
needs neither host sudo nor an image change: a throwaway container joins the backend's PID
namespace with `SYS_PTRACE`.

```bash
# On the perf1337 stack, restored and prepared exactly as M2 "Exact commands", quiet host.
B=$(docker ps -q --filter label=com.docker.compose.project=otfresh-perf1337 \
      --filter label=com.docker.compose.service=backend)
OUT=~/ot-cleanup-work/scale/profile; mkdir -p "$OUT"
# Load: steady 100 mixed for 15 min (M2 run 09's shape), started first:
scripts/perf/load/run.sh --fresh perf1337 --profile steady --mix mixed &   # 100 users, 900 s
sleep 300   # past ramp-up (this one sleep is a measurement offset, not a watcher)
docker run --rm --pid=container:"$B" --cap-add SYS_PTRACE -v "$OUT":/out python:3.13-slim \
  sh -c 'pip install --quiet py-spy==<pin current release> && py-spy --version > /out/version.txt &&
         py-spy record --pid 1 --subprocesses --duration 120 --rate 100 --format speedscope -o /out/p100-all.json &&
         py-spy record --pid 1 --subprocesses --gil --duration 120 --rate 100 --format speedscope -o /out/p100-gil.json'
```

(`--pid 1` is uvicorn inside the backend's namespace; with `--reload` in dev the server is a
child, hence `--subprocesses`.) Deliverable `~/ot-cleanup-work/scale/profile/SUMMARY.md`: the top
20 frames by GIL-holding share, grouped as JSON/Pydantic serialisation, SQLAlchemy ORM
hydration, middleware (`BaseHTTPMiddleware`), logging, privacy/redaction (Presidio), auth
(PBKDF2, JWT), our own Python. Decision rules for the lanes:

- If middleware frames are > 10% of GIL samples, add audit R10 (convert the four
  `BaseHTTPMiddleware` classes to pure ASGI) to wave 2 as its own lane.
- If ORM hydration on a few routes dominates, those routes go into lane D.
- If serialisation dominates, no code change; it is what more processes buy.

### 3.5 Lane B design

1. **Entrypoint.** New `backend/scripts/api-start.sh` (POSIX sh, `set -eu`):
   - if `PROMETHEUS_MULTIPROC_DIR` is set: `rm -rf "$PROMETHEUS_MULTIPROC_DIR"`, `mkdir -p` it
     (docs: wipe before startup, set from a shell script);
   - `exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips
     "<the existing list>"` with no `--workers` flag: uvicorn reads `UVICORN_WORKERS`,
     `UVICORN_LIMIT_CONCURRENCY`, `UVICORN_TIMEOUT_WORKER_HEALTHCHECK`,
     `UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN` from the environment (`auto_envvar_prefix`). The lane
     verifies `UVICORN_WORKERS` is honoured by counting processes in a built image (test 5).
   - `Dockerfile.prod`, `Dockerfile.lite`, `Dockerfile.blackwell`: `CMD ["/app/scripts/api-start.sh"]`
     and the `ENV` defaults below. Keep the `--forwarded-allow-ips` comment block.
2. **Defaults** (image `ENV`, mirrored in `docker-compose.yml` backend `environment:` with
   `${VAR:-default}` and in `.env.example`):

   | Variable | Default | Why |
   |---|---|---|
   | `UVICORN_WORKERS` | **2** (owner decision Q1, section 10) | doubles the CPU ceiling for ~+1-1.8 GB RAM |
   | `PROMETHEUS_MULTIPROC_DIR` | `/tmp/prometheus-multiproc` | inside the container, wiped by the script |
   | `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | **15 / 15 per process** (today 20/40 for the one process) | 2 x 30 = 60, today's total; section 3.6 |
   | `UVICORN_TIMEOUT_WORKER_HEALTHCHECK` | **30** | the supervisor's ping is answered by a Python thread (`multiprocess.py:57-75`); under GIL saturation the default 5 s risks the supervisor SIGKILLing a busy but healthy worker (`:219-221`), dropping its requests |
   | `UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN` | **30** | bounds the WebSocket drain (`main.py:76-87`) on SIGTERM |
   | `UVICORN_LIMIT_CONCURRENCY` | 512 (unchanged, **per worker**) | sockets per process; total = N x 512 |

   `--timeout-keep-alive` stays at uvicorn's default 5 s: the bundled nginx configs define no
   upstream keepalive pool (`frontend/nginx.conf`, `nginx/site.conf.template`), so there is no
   proxy idle timeout to order against. Document the rule for operators who add one (proxy
   idle timeout below the backend's keep-alive).
3. **Dev stays one process.** `docker-compose.override.yml` backend: `UVICORN_WORKERS=1` pinned
   (`--reload` and `--workers` are mutually exclusive), `PROMETHEUS_MULTIPROC_DIR` unset (plain
   registry). The final pass must measure the **prod** process model, and the M2 stacks were
   dev-overlay `--fresh` stacks. The lane settles how, in this order, and writes the answer into
   `scripts/perf/load/README.md`: (a) if `./opentr.sh start prod --build --fresh NAME
   --port-offset N` works (it re-pins baked services, `FRESH_BAKED_SERVICES`), the final pass
   uses it; (b) otherwise, add one `opentr.sh` option for `--fresh` dev stacks that replaces the
   backend's `--reload` command with `api-start.sh` (no bind-mount reload), and test it in
   `backend/tests/unit/test_opentr_fresh_aux_isolation.py`'s style. Do not hand-assemble `-f`
   lists (root `CLAUDE.md`).
   The lite and blackwell images' `CMD` lack `--proxy-headers`/`--forwarded-allow-ips` today
   (`Dockerfile.lite:384`, `Dockerfile.blackwell:252`, unlike `Dockerfile.prod:555-556`);
   `api-start.sh` gives all three the same flags. Call that out in the CHANGELOG as a fix.
4. **Metrics in multiprocess mode.** New `backend/app/core/metrics_multiprocess.py`:
   - `enabled() -> bool` = `PROMETHEUS_MULTIPROC_DIR` set.
   - `render_all() -> bytes`: `CollectorRegistry()` + `multiprocess.MultiProcessCollector(registry)`
     + `generate_latest(registry)` when enabled, else `generate_latest(REGISTRY)`.
   - At import in a worker: `mark_process_dead(pid)` for every pid named in the directory's
     `*_<pid>.db` files that is not alive (`os.kill(pid, 0)` raises `ProcessLookupError`). The
     uvicorn supervisor has no `child_exit` hook (gunicorn's documented place for this), so a
     worker that died leaves `live*` files until a living process clears them. `atexit` marks
     the process's own pid dead on clean exit.
   - `backend/app/core/metrics.py`: every `Gauge(...)` gets an explicit `multiprocess_mode`:
     per-process quantities (`http_requests_in_flight`, pool gauges) `livesum`; values the
     scraping process sets for the whole deployment (queue depth/age gauges, backup and outbox
     gauges refreshed at scrape) `mostrecent`. A unit test fails on any `Gauge` without one.
   - `backend/app/core/db_metrics.py`: replace `_PoolCollector` with gauges driven by pool
     events (`checkout`, `checkin`, `connect`, `close`; SQLAlchemy *Pool Events*):
     `db_pool_checked_out` (`livesum`), `db_pool_capacity` (`livesum`, set at registration),
     `db_pool_overflow` (`livesum`). Add histogram `db_connection_hold_seconds{route}` (checkout
     to checkin, route from the bounded resolver already used for HTTP metrics,
     `backend/app/core/CLAUDE.md:184,222`): this is the measurement that tells whether lane A
     shortened holds.
   - `backend/app/api/endpoints/metrics.py`: `/metrics` uses `render_all()`. `/metrics/queues`
     keeps rendering only the queue gauges: in multiprocess mode render from the values
     `update_queue_depths()` just computed in this process (a plain registry holding those
     gauge objects reads this process's mmap value, which is the one just written; test 3
     proves it), else as today.
   - Set `application_name` on API connections (`connect_args["application_name"] =
     "opentranscribe-api"` in `base.py`, only when `PROCESS_ROLE`/the existing API marker says
     this is the API; workers get `opentranscribe-worker:<queue>` if trivially available, else
     leave them). Lane G's sampler counts API connections from `pg_stat_activity` with it, so
     saturation is measured from Postgres, never through the saturated API.
5. **Native diarizer export under N workers.** `backend/app/transcription/native_provision.py`:
   take `fcntl.flock(LOCK_EX)` on `<models dir>/.provision.lock` around the export (blocking,
   bounded by the existing start period); the second worker then finds the marker. Python
   `fcntl.flock` docs.
6. **Connection budget.** `backend/tests/unit/test_db_connection_budget.py:80-83`: the backend row
   becomes `(pool_size + max_overflow + 1 readiness connection from lane C) x UVICORN_WORKERS`,
   all read from `docker-compose.yml`'s resolved defaults (not the coded `Settings` defaults,
   which no longer describe the container). `backend/tests/unit/test_api_capacity_relation.py`:
   the `L < C + T` relation is per process; assert it with the per-process pool.
7. **Docs.** `backend/app/core/CLAUDE.md:287` (multiprocess mode, the gauge-mode rule),
   `backend/app/core/threadpool.py` docstring (all numbers are per process), `.env.example`
   POSTGRES TUNING block (the formula with `x UVICORN_WORKERS`), `docs-site/docs/operations/performance-tuning.md`
   (workers vs replicas, memory per worker), CHANGELOG.

### 3.6 Connection budget (defaults)

Usable = `max_connections` 200 - 3 superuser slots = **197** (PostgreSQL *Connections and
Authentication*). Today's worst case is "about 170" (`.env.example:188-195`): API 60 + about 110
for Celery and beat. Per API process: `C = DB_POOL_SIZE + DB_MAX_OVERFLOW`, plus 1 readiness
connection (lane C).

| `UVICORN_WORKERS` | Per-process pool | API total | Others | Worst case | Fits 197? | Threads per process `max(40, C, L-C+1)` |
|---|---|---|---|---|---|---|
| 1 (today) | 20 + 40 | 60 | ~110 | ~170 | yes | 453 |
| **2 (default)** | 15 + 15 (+1) | 62 | ~110 | ~172 | yes | 483 |
| 4 | 10 + 10 (+1) | 84 | ~110 | ~194 | barely; raise `PG_MAX_CONNECTIONS` to 300 | 493 |
| 8 | 8 + 8 (+1) | 136 | ~110 | ~246 | no: `PG_MAX_CONNECTIONS` 300 | 497 |

Why a smaller pool per process is right: M2 saw 60 connections held while Postgres ran 1-5
statements at a time; the bottleneck was CPU, not connections. Lane A shortens holds; lane B adds
CPU. The sizing guide (lane G) states: raise `PG_MAX_CONNECTIONS` before raising workers past 2,
by 30 per extra worker at 10 + 10, and keep `work_mem` x connections inside RAM.

### 3.7 Risks

| Risk | Mitigation |
|---|---|
| Memory: each worker loads the embedding model, Presidio/spaCy and the reranker (1-1.8 GB per process measured) | Owner decision Q1 on the default; the sizing guide gives RAM per worker. |
| A dead worker's `live*` gauge files linger | startup sweep of dead pids (5.4); counters and histograms keep the dead process's counts by design (docs). |
| Per-process caches diverge for up to 30 s | Existing contract; documented in `backend/app/core/CLAUDE.md`. |
| Per-user WebSocket cap becomes N x cap | Documented as per process; the per-IP handshake limit is Redis-backed. A Redis-backed per-user cap is a managed-deployment item (section 7). |
| Kernel load-balances `accept()` unevenly across workers sharing one socket | Acceptable at N <= 8; verify per-worker request counts from the access log in the final pass. |
| A test asserts on `REGISTRY` contents directly | Lane B runs `rg -l "REGISTRY" backend/tests` and keeps those tests on the single-process path (env var unset under pytest). |

### 3.8 Test strategy

1. `backend/tests/unit/test_metrics_multiprocess.py`: in a temp `PROMETHEUS_MULTIPROC_DIR`, two
   subprocesses (`subprocess.run([sys.executable, "-c", ...])`) each increment the request
   counter and set `http_requests_in_flight`; `render_all()` in the parent shows the summed
   counter and a `livesum` of the two gauges; after one child exits without cleanup, a new
   process's import sweep removes its `livesum` contribution. Red on HEAD: `render_all` does not
   exist / the custom collector raises under multiprocess mode.
2. `backend/tests/unit/test_gauge_multiprocess_modes.py`: AST/introspection over
   `app.core.metrics` and `app.core.db_metrics`: every Gauge has an explicit `multiprocess_mode`;
   no custom collector is registered on the default registry. Must-fire case: a fixture module
   with a bare `Gauge`.
3. `backend/tests/api/test_metrics_queue_page.py` (extend if it exists): `/metrics/queues` under
   multiprocess mode contains only the five queue gauges with this scrape's values.
4. `backend/tests/unit/test_db_connection_budget.py`: new expectations; plus a negative control
   with `UVICORN_WORKERS=8` and the default `PG_MAX_CONNECTIONS` that must fail.
5. `backend/tests/unit/test_api_entrypoint.py`: `docker compose config` of the prod chain shows
   the backend `command`/image `CMD` is `api-start.sh` and `UVICORN_WORKERS` is set; the dev
   override pins `UVICORN_WORKERS=1`. A slow-marked live test (`-m gpu` is wrong; use the repo's
   existing live marker for docker-backed tests, see `backend/tests/CLAUDE.md`) starts the built
   image with `UVICORN_WORKERS=2` and asserts two worker processes under the supervisor
   (`ps` in the container) and that `/metrics` totals requests sent to both.
6. `backend/tests/unit/test_native_provision_lock.py`: two processes call the provision entry
   with a fake exporter that sleeps; the exporter runs once.

### 3.9 Verification

- A/B in the final pass, same snapshot and quiet host: F05 steady-300 mixed at
  `UVICORN_WORKERS=1` and at the default 2, and F08 ramp at the default. Expect: throughput past
  M2's 20 req/s ceiling; the knee (first step where p50 > 1 s) moves from 100 people (M2 run 12)
  to at least 200; backend CPU above 200% mean at the knee; `db_connection_hold_seconds` p95
  below M2's 1.7 s median idle gap.
- `pg_stat_activity` peak for `application_name='opentranscribe-api'` <= 62 during F11 spike.

---

## 4. Liveness and readiness (Lane C)

### 4.1 Problem

`/health/ready` needs two connections from the request pool (`SessionLocal()` at
`backend/app/main.py:465-470` and `engine.connect()` at `:525`) and runs on the default
threadpool. Under load it waits behind user traffic and answers 503 although nothing is broken.
An orchestrator using it for liveness would restart a busy API and make the overload worse; one
using it for readiness would take every busy replica out of rotation at once.

### 4.2 Evidence

- M2 #9: 25-31 `/health/ready` 503s per steady-300 run, 28 in the read-only ramp.
- Compose uses `/health/ready` as the backend healthcheck (`docker-compose.yml:367-372`,
  interval 10 s); `Dockerfile.prod:534-535` `HEALTHCHECK` uses `/health`.
- `/health` (`main.py:435-438`) already touches no dependency: a correct liveness endpoint.

### 4.3 Standard pattern

Kubernetes probe semantics: liveness answers "is this process wedged?" and must not depend on
back-end services ("cascading failures ... restarting of container under high load"); readiness
answers "may this replica receive traffic?" and checks the back-end services. Neither should
fail because the replica is **busy**: a load balancer cannot route around overload that every
replica shares. Startup probes cover slow boots (the backend's 600 s start period). Isolation of
the probe's own work: a dedicated anyio `CapacityLimiter(1)` (anyio *Capacity limiters*) and a
dedicated one-connection engine, so it never waits for the request pool or the default limiter.

### 4.4 Design

1. Move both endpoints out of `main.py` into `backend/app/api/health.py` (router at root, no
   `API_PREFIX`), included where `/metrics` is (`main.py:413-417`). Keep paths and response
   shapes byte-compatible (`/health` -> `{"status","version"}`, `/health/ready` ->
   `{"status","checks"}`), because release scripts and compose read them.
2. **Liveness** `/health`: unchanged, plus `async def` (no thread hop; it does no I/O), so it
   answers even when every thread is busy. Optional `/health/live` alias for Kubernetes naming.
3. **Readiness** `/health/ready`, `async def`:
   - Serves a cached snapshot `(checks, ready, taken_at)`; TTL `READINESS_CACHE_SECONDS`
     (default 2). On a stale snapshot, one refresh runs (single-flight `asyncio.Lock` per loop);
     concurrent callers get the previous snapshot rather than waiting. The first call after boot
     waits for the first refresh, bounded by `asyncio.timeout(DEPENDENCY_PROBE_TIMEOUT_SECONDS
     x 2)`.
   - The refresh runs the existing probes in `anyio.to_thread.run_sync(..., limiter=_probe_limiter())`
     with a per-loop `CapacityLimiter(1)` (same per-loop pattern as `threadpool.py:107-131`).
   - Postgres probe on a **dedicated engine** in new `backend/app/db/probe_engine.py`:
     `create_engine(settings.DATABASE_URL, pool_size=1, max_overflow=0, pool_timeout=1,
     pool_pre_ping=True, connect_args={**base connect_args, "connect_timeout": 2,
     "application_name": "opentranscribe-api-probe"})`, with `SET statement_timeout = 2000` on
     connect. `SELECT 1` and the Alembic current revision both on that one connection.
   - Schema check: cache "ok at head" for the process lifetime once seen (the head is fixed per
     process); re-check a stale result every refresh.
   - Redis, OpenSearch, MinIO probes unchanged (already bounded, `main.py:474-502`).
   - Pool saturation is **reported, never critical**: `checks["db_pool"] = "<checked_out>/<capacity>"`.
   - Critical set unchanged: Postgres, Redis, and schema when `RUN_MIGRATIONS_ON_STARTUP=false`.
4. Compose healthcheck stays `/health/ready` (it gates `depends_on: service_healthy` and `up
   --wait`; Compose never restarts on unhealthy). Document Kubernetes probes in
   `docs-site/docs/operations/production-deployment.md`: `livenessProbe` `/health`,
   `readinessProbe` `/health/ready` (period 5 s, timeout 3 s), `startupProbe` `/health/ready`
   with `failureThreshold x periodSeconds` >= 600 s.
5. Budget: +1 connection per process (lane B's budget test counts it).

### 4.5 Risks

| Risk | Mitigation |
|---|---|
| A cached "ready" hides an outage for up to 2 s | Acceptable vs probe periods of 5-10 s. |
| `async def` readiness blocks the loop | All blocking work is inside `to_thread.run_sync`; `tests/api/test_handler_blocking_io.py` and `test_async_handler_session_use.py` must stay green. |
| Moving the handlers breaks a test or script that imports `app.main.readiness_check` | `rg -n "readiness_check|health_check" backend scripts` and update imports in the same commit. |

### 4.6 Test strategy

`backend/tests/api/test_health_probes.py`:
1. Liveness with the database unreachable (monkeypatch both engines' `connect` to raise): 200.
   Red on HEAD: passes already (it is a control); keep it.
2. Readiness while the request pool is exhausted (override settings to `pool_size=1,
   max_overflow=0`, hold the connection): 200 in < 500 ms. Red on HEAD: 503 after
   `pool_timeout`.
3. Readiness with the probe engine's Postgres down: 503 with `checks.postgres` an error.
4. Two calls within the TTL run one probe (count probe invocations). Red on HEAD: two.
5. Readiness with every default-limiter token taken (`current_default_thread_limiter()` borrowed
   to zero): still answers. Red on HEAD: hangs (bound it with a test timeout).
6. The existing stale-schema 503 test (from the migrate lane) still passes.

### 4.7 Verification

Final pass F04-F06 and F10-F12: **0** `/health/ready` 503s while Postgres and Redis are up (M2: 25-31
per steady-300 run); readiness p99 < 100 ms from the access log.

---

## 5. Query cost (Lane D, then Lane D2)

All items follow the #1337 plan's decision rules (sections 5.1-5.4 of
`docs/design/1337_query_review_plan.md`): measure first, rewrite before indexing, statement-count
assertions that do not grow with rows, structural plan assertions, index-usability assertions.

### 5.1 Speaker-profiles N+1 (M2 #4)

- **Problem.** `GET /api/speaker-profiles/profiles` asks the privacy policy once per
  (owner, organization) scope; each ask is about four statements. 980 scopes, ~3,925 statements,
  2.96 s for one ordinary user; 28-42 s p50 at 300 people; removing it doubled throughput.
- **Evidence.** `_profile_media_instance_counts` (`backend/app/api/endpoints/speaker_profiles.py:80-83`
  at M2) -> `machine_identity_clauses` -> `_in_off_file`
  (`backend/app/services/privacy/redact_view.py:440-480`): one DISTINCT scope query, then
  `view.voiceprints_for_scope` per scope (`redact_view.py:131-151`) -> `policy.voiceprint_decision`
  (`backend/app/services/privacy/policy.py:395-418`) -> `_resolve` (`:305-310`) -> `_resolve_many`
  with one scope, inside `_isolated` (SAVEPOINT, `policy.py:119-133`). The batch resolver
  `_resolve_many` (`policy.py:255`) already exists and `gender_decisions` (`:313-315`) uses it.
- **Pattern.** Batch loading (the same rewrite as #1337's other N+1 fixes: one statement set per
  request, independent of row count). Request-scoped memo only (`PrivacyView` lives for one
  request), which issue #920 calls safe because the scope is one request.
- **Design.**
  1. `policy.voiceprint_decisions(db, scopes, media_file_ids=None) -> dict[Scope, Decision]` on
     `_resolve_many`. When `has_voiceprint_consent_hooks()` is false (community edition), one
     batch. When hooks are registered, layers in one batch and consent per (scope, file) exactly
     as `voiceprints_for_scope` keys it today (`redact_view.py:139-142`); never weaker.
  2. `PrivacyView.preload_voiceprint_scopes(scopes)` fills `self._voiceprints` from (1).
  3. `_in_off_file` calls it with the scope list before the loop.
  4. With many off scopes the `or_(and_(...) x N)` predicate (`redact_view.py:470-480`) grows
     linearly. Measure the statement at 980 scopes; if its planning time is material, replace it
     with a join to a `VALUES` list of `(user_id, organization_id)` using `IS NOT DISTINCT FROM`
     for the NULL organization (keep the docstring's NULL warning true; add a test with a NULL
     organization scope).
- **Risks.** This is the privacy plane: a batched decision that differs from the per-scope one
  leaks or hides voiceprints. A parity test is mandatory (below). `services/privacy/*` is
  security-critical (1337 plan L4 note): never loosen an existing test.
- **Tests.** (a) `count_statements` (`backend/tests/query_budget.py:52`) for the endpoint with 2
  and with 12 scopes: equal. Red on HEAD: 12-scope count is larger. (b) Parity: for a seeded
  matrix of scopes (personal, org, org with the org layer off, user layer off, instance layer
  off, platform ceiling off via `set_privacy_ceiling_resolver`), `voiceprint_decisions` equals
  `{s: voiceprint_decision(s)}`. (c) With a consent hook registered that refuses one file, that
  file's speakers are still hidden. (d) The existing privacy suites green.
- **Verification.** Final pass: `profiles.list` server p50 at steady-300 < 1 s (M2: 28-42 s);
  statements per call constant in `access_stats.csv` dbq.

### 5.2 Thumbnail backfill on every boot (M2 #11)

- **Problem.** Each boot window dispatches `backfill_missing_thumbnails`, which walks every
  completed video without a thumbnail and re-dispatches files whose generation always fails.
- **Evidence.** `backend/app/startup/maintenance.py:378-395` (elected, but every boot);
  `backend/app/tasks/thumbnail_migration.py:231-271` (self-chaining, no memory of failures). M2:
  24,920 `generate_thumbnail` tasks per restart, cpu-worker at 1,100-2,700% for ~20 min.
- **Pattern.** The one-time-job-with-done-flag pattern this repo already uses for the imohash
  recompute (`_run_imohash_recompute`, `maintenance.py:398+`: "Checks a DB flag first").
- **Design.** A `system_settings` flag `thumbnail_backfill_done` (coded default in
  `backend/app/core/constants.py`, no migration). The startup dispatch checks it first. The
  chain sets it when a pass reaches the end (`has_more` false), whatever the per-file outcomes.
  Dispatch the backfill and its `generate_thumbnail_task` children at the background priority
  (`backend/app/core/queues.py` `CPUPriority`'s lowest step). An admin who wants a re-run
  clears the flag (document in `docs-site/docs/operations/runbooks.md`). The WebP migration
  (`migrate_thumbnails_to_webp`) gets the same treatment only if it has the same shape; check
  `thumbnail_migration.py` first.
- **Tests.** (a) Two boots in a row (call `_run_thumbnail_migration` twice with the election
  stubbed true): the second dispatches nothing. Red on HEAD: dispatches again. (b) A file whose
  generation fails does not prevent the flag being set. (c) Priority asserted on the
  `apply_async` call.
- **Verification.** Restart the perf1337 stack twice: second boot dispatches 0 thumbnail tasks
  (count in the cpu-worker log); the final-pass precondition "wait for the boot backfill" is
  removed from the runbook.

### 5.3 Background statements and `/api/tags` (M2 #12, #13)

- **Problem / evidence.** pgss #1 in run 06: a search-maintenance count at 2.9 s per call (M2
  #12; now `backend/app/tasks/search_maintenance_task.py:252-263` `missing_facts_files`, or the
  correlated latest-task subquery in `backend/app/services/search/index_retry.py:89-101`);
  pgss #2: `GET /api/tags` 89 ms mean, which is `accessible_usage_counts`
  (`backend/app/services/tag_collisions.py:118-128`), not the G13 statement (already rewritten,
  `:294-306`).
- **Pattern.** 1337 plan 5.3: `JOIN LATERAL` / `DISTINCT ON` for per-row latest-row subqueries;
  for the facts count, the planner should anti-join `~exists`; EXPLAIN decides.
- **Design.** On the restored perf1337 snapshot, `EXPLAIN (ANALYZE, BUFFERS)` the three
  statements (capture the SQL with `count_statements` or the auto_explain log). Rewrite only
  what EXPLAIN shows as a per-row SubPlan or a scan the 5.1 rule calls avoidable. For
  `accessible_usage_counts`, check whether `accessible_file_ids_subquery` is evaluated as a
  hashed SubPlan or a semi-join; if a semi-join is not chosen, express it as a join to the
  accessible set.
- **Tests.** Structural plan assertions (1337 plan 5.4 b): `SubPlan` absent / `Anti Join` or
  `Semi Join` present on the shared test DB, plus result parity on a seeded set.
- **Verification.** Final pass `pgss.csv`: neither statement in the top 5 by total time.

### 5.4 #920: caching only where provably safe

- **Design.** Write issue #920's rule (its acceptance criterion 1) into `backend/app/core/CLAUDE.md`
  under a new "Caching rule" heading: never cache a value downstream of redaction config,
  quarantine/takedown state or permission/share scope across requests; request-scoped memos are
  safe; each cache states its invalidation signal, what makes it complete, and **where in the
  write path it is emitted** (the issue's 2026-09-12 amendment). This plan adds **no** new
  cross-request cache on those paths: the profiles fix (5.1) is request-scoped; the banner read
  (2.4.2) reuses the existing auth-config process cache, which is outside the three planes.
- **Tests.** None new (documentation); acceptance 3/4 apply to future caches. Comment on #920
  with the commit sha; close only criterion 1.

### 5.5 Lane D2: measured indexes (#1337 L5)

- **Problem.** The L5 lane never ran. Candidates from the 1337 plan (6.2) and M1's verdicts:
  P8 cluster inbox (CONFIRMED, 321-344 ms; `idx_speaker_user_id` reads 72,152 rows, 127,848
  removed by filter), K4 `task(created_at)` (shape confirmed, 42 ms, below budget), V13/A2
  `media_file(completed_at)` (shape confirmed, below budget), T1 (REFUTED), V16 and P4 (not
  measured).
- **Pattern.** The repo's concurrent-index mechanism (`backend/app/db/concurrent_indexes.py`
  `IndexSpec`/`ensure_concurrently`, registered in `migrations._retry_concurrent_indexes`), a
  no-DDL revision, a `schema_detect` arm, exactly as 1337 plan 5.2 steps 1-6 describe. Next free
  revision: **v470** (verify with `ls backend/alembic/versions` at lane start).
- **Design.** Re-measure P8 on the restored snapshot **after** #1337 L2 (which changed the cluster
  queries, `b737fa9c1`). Ship an index only if it passes 1337 plan 5.1 (measured above budget, no
  existing index serves it, usable under `enable_seqscan=off`, write cost acceptable). Expected
  outcome: at most the P8 partial index `speaker (user_id, ...) WHERE verified IS FALSE AND
  profile_id IS NULL`; K4/V13 stay unindexed (below budget) unless the re-measure says
  otherwise. If nothing passes, D2 ships no revision and records the measurements in the 1337
  plan section 9.
- **Tests / verification.** 1337 plan 5.4 c (index-usability), the ladder and consistency tests,
  `python3 scripts/guards/check_migration_safety.py`, and the fresh-stack build-then-exists
  check in 1337 plan 6.3. D2 is the only lane that changes the schema.

### 5.6 Login (Lane E; listed here because the completion plan files it under query cost)

- **Problem.** Login is the heaviest request: ~21 statements, PBKDF2 at 600,000 iterations, two
  synchronous OpenSearch audit writes (0.6-1.95 s each in smoke), and a failed login under load
  becomes a 401 storm.
- **Evidence.**
  - `backend/app/auth/direct_auth.py:213-238`: the pooled raw connection (and its open psycopg2
    transaction) is held across `_verify_and_upgrade_password` (PBKDF2,
    `backend/app/core/security.py:21-33`).
  - `direct_auth.py:256-258`: `except Exception: logger.error(...); return None`, so a pool
    timeout or a dropped connection reads as **bad credentials**: the caller answers 401, and
    the SPA signs in again (M2: 1,000-3,800 401s per contended run). This is a fail-open-shaped
    defect (an outage indistinguishable from a legitimate "no").
  - `backend/app/api/endpoints/auth/login.py:586-592`: `except Exception` -> 500 with
    `exc_info=True`.
  - `backend/app/auth/audit.py:362-365` -> `:190-203`: `client.index(...)` on the request
    thread for `AUTH_LOGIN_SUCCESS` (`login.py:502`) and the session-created event.
- **Pattern.** Short transactions (SQLAlchemy); the transactional outbox already in the repo
  (`backend/app/services/audit_outbox.py`: `stage` in the transaction, `deliver_staged` after
  commit, the beat relay retries); FastAPI `BackgroundTasks` for small same-process work after
  the response (FastAPI *Background Tasks*, https://fastapi.tiangolo.com/tutorial/background-tasks/).
- **Design.**
  1. `direct_auth.authenticate_user`: fetch the row, `conn.rollback()` and return the connection
     **before** hashing; verify; reacquire a connection only when a rehash/upgrade must be
     written. Infrastructure errors (`sqlalchemy.exc.TimeoutError`, `psycopg2.OperationalError`,
     `psycopg2.InterfaceError`) are **raised**, not turned into `None`; only "no such user",
     "cannot authenticate" and "wrong password" return `None`.
  2. `login.py`: let the three overload classes from lane A propagate (an `except` clause
     before the generic one), so they answer 503 with `Retry-After`, never 500 or 401.
  3. Login success and session-created events: `audit_outbox.stage(db, ...)` in the login
     transaction, and `deliver_staged(db)` as a `BackgroundTasks` task after the response, so
     the user does not wait on OpenSearch and the relay retries if the sink is down. Check
     `backend/app/db/audit_protection.py` and `schema_detect/governance.py` accept the event
     types. Every other `audit_logger.log` call on the login path (`rg -n "audit_logger\.log"
     backend/app/api/endpoints/auth/login*.py`) is listed in the commit body with its decision.
  4. Failure events (`log_login_failure`, lockout) stay as they are unless the profile shows
     them hot; they are rare under normal load.
- **Risks.** The audit trail must not lose a login event: staging in the same transaction is
  stronger than today (today an OpenSearch failure falls back to a file). A relay delay of up to
  one beat interval (60 s) when immediate delivery fails is acceptable and documented.
- **Tests.** (a) Connection hold: a fake verifier that records `engine.pool.checkedout()` during
  hashing: 0. Red on HEAD: 1. (b) A pool timeout during the user fetch answers 503, not 401, and
  records **no** lockout attempt. Red on HEAD: 401. (c) Login success writes one `audit_outbox`
  row per event and calls no OpenSearch client on the request path (spy on
  `audit_logger._index_to_opensearch`). (d) Existing login, lockout, MFA and FIPS suites green.
  `./scripts/run-mutation-tests.sh --module security` is opt-in; run it once if `direct_auth.py`
  is in its mapped files (`scripts/run-mutation-tests.sh:136`).
- **Verification.** Final pass: smoke login server p50 below M2's 794-1,069 ms; F11 spike: login
  500s = 0 and "401 session lost" not above the scenario floor.

---

## 6. WebSocket and chat under load (Lane F)

- **Problem.** WS echo p95 6.8 s at 100 people, 26-35 s at 300; handshake fails on pool
  exhaustion and the socket is closed as an auth failure; chat time to first token p95 54 s at 100.
- **Evidence.** M2 #8; `backend/app/api/websockets.py:745-778`: `_authenticate_in_session` in
  `run_in_threadpool`; a pool timeout from the cookie path propagates out of the endpoint, and
  from the first-message path lands in `except Exception` -> close 4003 "Authentication error"
  (`:772-775`), which the SPA treats as a credential problem. Chat streaming itself is already
  async with its own limiter and a global Redis cap (section 0.1).
- **Pattern.** RFC 6455 close code 1013 "Try Again Later" (IANA WebSocket close code registry,
  https://www.iana.org/assignments/websocket/websocket.xhtml#close-code-number) for overload;
  the existing `ws_admission.TRY_AGAIN_LATER_CLOSE` constant is the repo's own use of it.
- **Design.**
  1. In `websocket_endpoint`, catch the three overload classes around both
     `_authenticate_in_session` calls and close with `ws_admission.TRY_AGAIN_LATER_CLOSE`,
     reason "Server busy"; one WARNING line. Check the SPA's reconnect logic
     (`rg -n "1013|TRY_AGAIN" frontend/src`) backs off on 1013 and does not sign the user out;
     if it does not, fix it in this lane (frontend file, with a vitest).
  2. Event-loop lag gauge: new `backend/app/core/loop_lag.py`, a task started in the lifespan
     that sleeps 0.5 s and records the overshoot in a histogram `event_loop_lag_seconds`
     (multiprocess-safe per lane B's rules). No library in the stack exposes asyncio loop lag;
     this is the minimal instrument the sampler and the final pass need to separate "loop
     starved" from "pool starved". Keep a reference to the task (Python *asyncio.create_task*
     docs).
  3. M2 #15: `GET /api/chat/conversations/{uuid}/messages` must not 500 because one stored
     citation lacks `id`: validate per message and drop (and log once) an invalid citation
     instead of failing the list (`backend/app/api/endpoints/chat/messages.py`, the
     `MessageList` validation; locate the current line with `rg -n "MessageList"`).
  4. Document in `backend/app/api/CLAUDE.md`: per-user socket cap is per process; total open
     sockets = `UVICORN_WORKERS x UVICORN_LIMIT_CONCURRENCY`.
- **Tests.** (a) Handshake with the pool exhausted: close code 1013, not 4003. Red on HEAD:
  4003 or an unhandled exception. (b) The loop-lag histogram records a lag when a test blocks the
  loop for 0.3 s. (c) A conversation with one malformed citation lists all its messages. Red on
  HEAD: 500.
- **Verification.** Final pass: WS echo p95 < 1 s at steady-100 (M2: 6.8 s); WS "closed by
  server" count in F05 below M2 run 07's 113; chat first-token p95 at steady-100 < 5 s
  (`thresholds.toml` stream limit).

### Lane A2: the SPA honours 503 and `Retry-After`

- **Problem.** `frontend/src/lib/axios.ts:239-260` parses `Retry-After` for 429 only; a 503 from
  overload is shown as a generic error, and a failed login during overload sends the user back to
  the login loop.
- **Design.** For a 503 whose body code is `server_busy`: idempotent methods (GET, HEAD) retry
  **once** after `Retry-After` seconds plus up to 1 s random jitter (bounded so a crowd does not
  return in lockstep); other methods surface a toast "The server is busy. Try again in a few
  seconds." and do not retry. A 503 never triggers the session-refresh or sign-out path. One new
  string in all 12 locales (`frontend/src/lib/i18n/locales/*.json`), translated, `ar` checked
  RTL. Use the existing `parseRetryAfter` helper.
- **Tests.** vitest in `frontend/src/lib/axios.test.ts`: GET 503 `server_busy` with
  `Retry-After: 1` retries once after >= 1 s (fake timers), POST does not retry, a 503 does not
  call the refresh coordinator. Red on HEAD: no retry. `cd frontend && npm run check:i18n &&
  npm run check && npm run test -- axios`.
- **Verification.** Open the gallery in a browser against a stack with the pool forced tiny
  (`DB_POOL_SIZE=1 DB_MAX_OVERFLOW=0` on a `--fresh` stack) while a script holds the connection:
  the page recovers after the hold ends, no sign-out (root `CLAUDE.md`: look at the UI).

---

## 7. Managed-deployment-only items (out of OSS scope)

These belong to the hosted offering, not the open-source release. They are listed so nothing in
the OSS design blocks them. The lead copies them into `~/ot-cleanup-work/cloud-followups.md`
(private); the public text stays generic.

1. **PgBouncer in transaction mode** in front of Postgres once replicas x pool exceeds what
   Postgres should hold. Breaks `SET` (the app's connect-event `SET idle_in_transaction_session_timeout`
   and lane A's `SET statement_timeout`): move both to `ALTER ROLE <api role> SET ...` (the repo
   already sets the idle timeout on the role, `backend/app/db/roles.py`
   `apply_idle_timeout_to_role`) and use `NullPool` in the API engine (SQLAlchemy *Switching Pool
   Implementations*). Session advisory locks are "Never" under transaction pooling: the migrate
   step must connect directly to Postgres, not through PgBouncer. psycopg2 does not use
   protocol-level prepared statements, so `max_prepared_statements` is not needed.
2. **One process per container and replica autoscaling** on in-flight requests or loop lag, not
   CPU alone (FastAPI *Docker* page; Kubernetes HPA). `UVICORN_WORKERS=1` in that image.
3. **Graceful drain**: `preStop` sleep longer than the endpoint-removal delay, then SIGTERM with
   `UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN`; a PodDisruptionBudget.
4. **Redis-backed per-user WebSocket cap** across replicas; Redis HA (Sentinel or managed) since
   pub/sub fan-out, rate limits, elections and the chat cap all depend on it.
5. **Celery beat as a singleton** across replicas (one beat Deployment with `replicas: 1`, or a
   lock-based scheduler); audit M5's lease work removed the old backstop.
6. **Metrics per replica**: Prometheus scrapes each pod; multiprocess mode is only for
   multi-worker containers.
7. **Read replicas** for gallery/search reads, CDN for media delivery, S3 instead of single-node
   MinIO, OpenSearch sizing per tenant count.
8. **Per-tenant rate limits and fairness** beyond the per-user transcription/NLP fairness already
   shipped.
9. **Tier tests** at 1,000 and 10,000 registered users (issue #1174 P5/P6) on the cluster.

---

## 8. Load tool fixes and sizing guide (Lane G, issue #1174)

### 8.1 Problems and evidence (M2 "Tool bugs")

| Bug | Evidence | Fix |
|---|---|---|
| T1 seed script exports only Postgres ports | `scripts/seed-query-review.sh:45-46` | also export `OPENSEARCH_PORT`/`MINIO_PORT` with the offset (the guard is `backend/app/scripts/corpus_injection/env.py:105-117`) |
| T2 seed then restore cannot add activity | `backend/app/scripts/scale_dataset/pg_governance.py:282-295` | `--activity-only` mode in `backend/app/scripts/scale_dataset/__main__.py` calling `pg_activity.seed_activity` (`pg_activity.py:46`) with the snapshot's bases, as `~/ot-cleanup-work/1337/perf/m2-load/tools/seed_activity_only.py` does |
| T3 eleven sequences behind `max(id)` in the snapshot | M2 T3 list | the seed claims ids for every table it writes through `pg_synthetic` id claims (`pg_synthetic.py:190-199`), and ends with a `setval(pg_get_serial_sequence(t,'id'), max(id))` pass over every seeded table; then take a **new snapshot** with activity and sequences applied and record its sha256 |
| T4 `topics.suggestions` 204 counted as failure | `scripts/perf/load/personas_read.py:115` | `expect=(200, 204)` |
| T5 `admin.audit_logs` needs super admin | `personas_read.py:219`, `backend/app/api/endpoints/admin_audit_logs.py:86` | call it only from a super-admin identity; if the seed has none, add one (`perf-0` super admin) or exclude the route in `routes.py` with the reason |
| T6 `speakers.merge` 404 after the restore deletes the temp speaker | `scripts/perf/load/personas_write.py:208-213`, `backend/app/api/endpoints/transcript_segments.py:191-196` | restore by merging the temp speaker into the original (which moves the segment back) instead of moving the segment then merging |
| T7 sampler/summary false-pass on missing samples | `scripts/perf/load/sampler.py:55,226-227`; `summarize.py:173-195` | (a) a coverage rule: a check fails when fewer than `min_sample_coverage` (new key in `thresholds.toml`, 0.8) of expected sweeps produced data; (b) pool saturation judged from `pg_stat_activity` filtered by `application_name='opentranscribe-api'` (lane B) or, before B merges, by the backend container's IP as `tools/pg_probe.sh` does, so it never goes through the saturated API |
| T8 Locust's own close at run end recorded as `WS dropped` | `scripts/perf/load/ws_listener.py:87-91` | ignore errors after the stop event |
| T9 seeded chat citations lack `id` | seed chat writer in `backend/app/scripts/scale_dataset/` (`rg -n "citations" backend/app/scripts/scale_dataset`) | write `id` like the real pipeline |
| T10 seeded files have no media objects | M2 Setup step 5 | the runbook documents the suppression; seeded files carry the seed id already (`__main__.py:36`): background repair tasks that touch media skip rows with that seed marker **only if** that marker is test-only data; otherwise document only |
| M2 #10 `/metrics` cost and queueing | sampler scrapes through the API | covered by T7 (b) |
| M2 #14 unfiltered `/api/speakers` not driven | personas | add one paged unfiltered `speakers.list` call to the viewer's speakers task |
| Side tools out of repo | `~/ot-cleanup-work/1337/perf/m2-load/tools/` | move `run_profile.sh`, `chain.sh`, `pg_probe.sh`, `report_table.py`, `ramp_stages.py`, `access_stats.py` into `scripts/perf/load/tools/`, cleaned (no host paths), so the final pass is repeatable from the repo |

### 8.2 Tests

- `backend/tests/unit/test_load_scenario_routes.py` (exists): extend for the new expectations
  (204 accepted for `topics.suggestions`, the merge-as-restore, the super-admin-only route).
- New `backend/tests/unit/test_load_summary_coverage.py`: `summarize.py` given 2 of 111 pool
  samples fails the saturation check (red on HEAD: "0.0% of 2 samples ... ok", M2 run 05).
- New `backend/tests/unit/test_scale_seed_sequences.py` (`--throwaway-db`): after a small seed,
  every seeded table's sequence `last_value >= max(id)`; red on HEAD for at least
  `chat_conversation`.
- `--activity-only` on a seeded DB adds rows and does not touch existing users (unique-violation
  free).

### 8.3 Sizing guide

New `docs-site/docs/operations/scaling-and-sizing.md` (#1174 P4; register it in the docs-site
sidebar if the sidebar is not autogenerated; `docs-site/CLAUDE.md` has the rule). Written in two
steps: lane G writes the **method** (what to measure, how to read the knee, the connection
budget formula from section 3.6, RAM per API worker, workers vs replicas, the probe settings from
section 4.4, `PG_MAX_CONNECTIONS` guidance, when to add PgBouncer) with the M2 numbers marked
"before v0.7.0"; the final pass (section 11) fills the "after" table. Cross-link from
`performance-tuning.md` and `docs-site/docs/developer-guide/performance-testing.md`.

### 8.4 Verification

Smoke read-only and smoke mixed on the new snapshot: error rate below the 0.5% threshold (M2:
2.06-3.11%, floor from T4-T6); sampler coverage >= 0.8 in every steady and ramp run.

---

## 9. Lanes (execution detail)

### 9.1 Rules for every lane (from root `CLAUDE.md` and the user's global rules)

- One agent per lane, in its **own git worktree** on its own branch, created from the
  integration branch's latest wave tag (wave 1 lanes from `cleanup-wave-af19` plus this plan's
  commit; wave 2 lanes from the wave-1 tag):
  `git worktree add ../transcribe-app-wt-<lane> -b <branch> <tag>`. Never two writers in one
  worktree. Never push, never merge, never `--no-verify`.
- One commit per fix/concern, conventional commits, hooks on. Commit with explicit file
  pathspecs.
- Red then green: every new test is first run against a `git archive HEAD` tree
  (`mktemp -d -t ot-redcheck.XXXXXX`, root `CLAUDE.md`) and the failing assertion line is pasted
  into the commit body.
- Lanes run **only** targeted tests (`-n 1`) and static checks on touched files
  (`backend/venv/bin/ruff check`, `ruff format --check`, `backend/venv/bin/mypy` on the touched
  files; `npm run check` for frontend). No full suites, no `--all-files` pre-commit, no pre-push
  tier in a lane. Before reporting: `cd backend && python3 ../scripts/audit-tests.py tests` at 0
  open findings.
- DB-backed tests without the dev stack: `./scripts/run-backend-tests.sh --throwaway-db <paths>`.
  Live-stack work only on a `--fresh` stack with its `*_PORT` vars exported
  (`./opentr.sh start dev --fresh <lane> --port-offset <N>`), never the shared dev stack.
- GPU: none of these lanes needs one. Never GPU 0.
- Read the official docs for every package touched, for the pinned version, and cite the pages
  in the commit body (section 1 has the starting list).
- CHANGELOG `[Unreleased]` entry per user-visible change, in the lane's own commit.
- Report: branch, commit shas, red/green evidence per test, files touched, anything deferred
  with its reason.

### 9.2 Lane P: profile (wave 0, measurement only)

- Files: none in the repo. Output in `~/ot-cleanup-work/scale/profile/`.
- Steps: section 3.4. Stack: `perf1337`, restored per M2 "Exact commands", with the M2 finding-1
  suppression no longer needed (P0 fixed) but the thumbnail wait still needed (5.2 not merged yet).
- Acceptance: `SUMMARY.md` with the grouped GIL table, the py-spy version, `uptime` before and
  after, and the three decision-rule outcomes from section 3.4.
- Runs while wave 1 codes; it reads a stack started from a quiet checkout (the
  `transcribe-app-wt-cleanup-wavec` checkout or a fresh worktree at `cleanup-wave-af19`), never
  a lane's worktree.

### 9.3 Lane A: connection lifecycle (wave 1)

- Files (owned): `backend/app/db/session_utils.py`, `backend/app/db/base.py`,
  `backend/app/db/migration_connection.py`, `backend/app/db/request_deadline.py` (new),
  `backend/app/middleware/request_deadline.py` (new), `backend/app/core/db_overload.py` (new),
  `backend/app/api/endpoints/auth/dependencies.py`, `backend/app/api/endpoints/auth/lifecycle_gates.py`,
  `backend/app/main.py` (exception-handler and middleware registration lines only; lane C edits
  the health section of the same file in the same wave, a different hunk),
  `backend/app/core/config.py` (new keys `DB_STATEMENT_TIMEOUT_MS`, `REQUEST_DEADLINE_SECONDS`,
  `DB_OVERLOAD_RETRY_AFTER_SECONDS`, default change `DB_POOL_TIMEOUT_SECONDS`, in one block),
  `.env.example`, `docker-compose.yml` (backend `environment:` one line),
  `backend/app/core/threadpool.py` (docstring), `backend/app/db/CLAUDE.md`, the five test files
  in 2.6, `CHANGELOG.md`.
- Acceptance: tests 2.6.1-2.6.5 red on HEAD and green; existing auth, CSRF, session-probe and
  `test_endpoint_session_lifetime.py` suites green when run targeted (`-n 1`); no new
  `# noqa`/`# type: ignore`.
- Commands: section 2.6.

### 9.4 Lane C: probes (wave 1)

- Files: `backend/app/api/health.py` (new), `backend/app/db/probe_engine.py` (new),
  `backend/app/main.py` (remove `health_check`/`readiness_check`, include the router),
  `backend/app/core/config.py` (`READINESS_CACHE_SECONDS`, own block),
  `backend/tests/api/test_health_probes.py` (new), any test importing the moved functions,
  `docs-site/docs/operations/production-deployment.md`, `docs-site/docs/operations/monitoring.md`,
  `CHANGELOG.md`.
- Acceptance: 4.6 tests; `curl` against a `--fresh` stack shows identical JSON shapes before and
  after; `./opentr.sh start dev --fresh c1` reaches healthy (compose healthcheck still passes).
- Commands: `./scripts/run-backend-tests.sh --throwaway-db tests/api/test_health_probes.py
  tests/api/test_handler_blocking_io.py tests/api/test_async_handler_session_use.py -n 1`.

### 9.5 Lane D: query cost (wave 1)

- Files: `backend/app/services/privacy/policy.py`, `backend/app/services/privacy/redact_view.py`,
  `backend/app/api/endpoints/speaker_profiles.py` (only if the call site changes),
  `backend/app/startup/maintenance.py` (`_run_thumbnail_migration` only),
  `backend/app/tasks/thumbnail_migration.py`, `backend/app/core/constants.py` (one flag default),
  `backend/app/tasks/search_maintenance_task.py` and/or `backend/app/services/search/index_retry.py`
  and/or `backend/app/services/tag_collisions.py` (only what EXPLAIN justifies),
  `backend/app/core/CLAUDE.md` (#920 rule section only), tests per 5.1-5.3,
  `docs-site/docs/operations/runbooks.md` (backfill flag), `CHANGELOG.md`.
- Order inside the lane: 5.1 (biggest offender) -> 5.2 -> 5.4 -> 5.3. Use lane P's summary, if
  ready, to add any route it names.
- EXPLAIN evidence goes to `~/ot-cleanup-work/scale/queries/` and its conclusions into the commit
  bodies; the perf1337 stack is read-only for this lane (no schema change).
- Acceptance: 5.1 statement-count equality and parity tests; 5.2 two-boot test; 5.3 structural
  plan assertions for whatever was rewritten (or a written "EXPLAIN shows no avoidable SubPlan"
  with the plan attached).
- Commands: `./scripts/run-backend-tests.sh --throwaway-db <the new tests> tests/unit/test_privacy*
  tests/api/test_speaker_profiles* -n 1` (adjust the globs to the files that exist; list them in
  the report).

### 9.6 Lane G: load tool (wave 1)

- Files: `scripts/perf/load/{personas_read,personas_write,ws_listener,sampler,summarize,routes}.py`,
  `scripts/perf/load/thresholds.toml`, `scripts/perf/load/README.md`,
  `scripts/perf/load/tools/*` (moved in), `scripts/seed-query-review.sh`,
  `backend/app/scripts/scale_dataset/{__main__,pg_activity,pg_synthetic,pg_governance}.py` (as
  needed), `backend/tests/unit/test_load_scenario_routes.py`, the two new tests in 8.2,
  `docs-site/docs/operations/scaling-and-sizing.md` (new), `docs-site/docs/developer-guide/performance-testing.md`,
  `docs-site/docs/operations/performance-tuning.md` (link only), `CHANGELOG.md`.
- Acceptance: 8.2 tests; a 60 s smoke against a `--fresh` stack seeded with the new path from
  empty (seed -> activity -> snapshot) shows error rate < 0.5% and coverage >= 0.8; the new
  snapshot's sha256 is written to `~/ot-cleanup-work/1337/perf/seed-perf1337-v2.dump.sha256`.
- Does not touch `backend/app/` outside `scripts/scale_dataset`.

### 9.7 Lane B: process model (wave 2; after A and C merge)

- Files: `backend/scripts/api-start.sh` (new), `backend/Dockerfile.prod`, `backend/Dockerfile.lite`,
  `backend/Dockerfile.blackwell`, `docker-compose.yml` (backend env), `docker-compose.override.yml`
  (dev pin), every other overlay that overrides the backend `command` (`rg -n "uvicorn"
  docker-compose*.yml`), `backend/app/core/metrics_multiprocess.py` (new),
  `backend/app/core/metrics.py`, `backend/app/core/db_metrics.py`,
  `backend/app/api/endpoints/metrics.py`, `backend/app/db/base.py` (`application_name` only),
  `backend/app/transcription/native_provision.py`, `backend/app/core/config.py` (read
  `UVICORN_WORKERS` for the startup log and budget; own block), `backend/app/core/threadpool.py`
  (docstring/log per process), `.env.example`, `backend/app/core/CLAUDE.md`,
  `docs-site/docs/operations/performance-tuning.md`, tests per 3.8, `CHANGELOG.md`.
- Acceptance: 3.8 tests; `./opentr.sh start prod --build --fresh b1 --port-offset <N>` starts
  with two workers, `/metrics` aggregates both, `/health/ready` 200; `docker compose config`
  of every chain renders; `./scripts/docker-build-push.sh` not run (no publish).
- Commands: targeted tests with `-n 1`; one prod `--fresh` start and stop
  (`./opentr.sh stop --fresh b1`; `fresh-destroy` only with the owner's y).

### 9.8 Lane E: login (wave 2; after A merges)

- Files: `backend/app/auth/direct_auth.py`, `backend/app/api/endpoints/auth/login.py`,
  `backend/app/api/endpoints/auth/login_completion.py` (only if the session-created event is
  logged there), tests per 5.6, `CHANGELOG.md`.
- Acceptance: 5.6 tests red then green; existing auth suites green targeted
  (`tests/api/endpoints/test_auth_comprehensive.py`, lockout, MFA, FIPS tests).

### 9.9 Lane F: WebSocket and chat (wave 2; after A and B merge)

- Files: `backend/app/api/websockets.py`, `backend/app/core/loop_lag.py` (new),
  `backend/app/main.py` (lifespan: start/stop the lag task), `backend/app/api/endpoints/chat/messages.py`
  (citation validation), frontend reconnect file only if 6.1 needs it, `backend/app/api/CLAUDE.md`,
  tests per section 6, `CHANGELOG.md`.
- Acceptance: section 6 tests; `tests/api/test_websocket_concurrency.py` and the WS revocation
  suites green targeted.

### 9.10 Lane A2: SPA retry (wave 2; after A merges)

- Files: `frontend/src/lib/axios.ts`, `frontend/src/lib/axios.test.ts`, the 12 locale JSON
  files, `CHANGELOG.md`.
- Acceptance and commands: section 6 "Lane A2".

### 9.11 Lane D2: indexes (wave 2; after D merges)

- Files: per 1337 plan 6.2 L5 (`backend/alembic/versions/v470_query_review_indexes.py`,
  `backend/app/db/perf_indexes.py`, `backend/app/db/migrations.py` registration, model
  `__table_args__`, `backend/app/db/schema_detect/` arm, ladder and consistency tests,
  `backend/alembic/CLAUDE.md` lane-table row), only if 5.5's re-measure passes 5.1.
- Acceptance: 1337 plan 6.1 recipe; fresh-stack build-then-exists check.

### 9.12 Waves, sweeps and tags (coordinator)

| Wave | Lanes (max 4 at once, host budget) | Merge order into the integration branch (`--no-ff`) | Sweep, then tag |
|---|---|---|---|
| 0 | P (measurement) | none | none |
| 1 | A, C, D, G | A, C (same file `main.py`, different hunks; resolve by keeping both), D, G | `scale-w1` |
| 2 | B, E, F, A2, then D2 when D's evidence is in | B, E, F, A2, D2 | `scale-w2` |
| 3 | Final load pass (task #37) | none | results file |

The one sweep per wave, on the integration branch, with zero writers active and a clean
`git status`:

```bash
./opentr.sh start dev
./scripts/run-integration-tests.sh
cd frontend && npm run check && npm run build && npm run test && cd ..   # wave 2 (A2, F frontend)
scripts/safe-precommit.sh run --all-files
scripts/safe-precommit.sh run --all-files --hook-stage pre-push
git tag -a scale-w<N> -m "scale foundations wave <N>: <lanes>"
```

Wave 2 also: `./opentr.sh start prod --build --fresh sweep2` once (B's image), `/health/ready`
200, two worker processes, and D2's migration both ways on a fresh stack if D2 shipped one.

While wave 1's sweep runs, wave 2 lanes may start coding from the wave-1 merge commit (not yet
tagged) only if they do not depend on a lane whose sweep might fail; B, E, F, A2 all depend on A,
so they wait for `scale-w1`.

---

## 10. Decisions that need the owner

**Q1. Default number of API worker processes in the shipped compose and images.**
- `UVICORN_WORKERS=2` (recommended default): about twice the API's CPU ceiling (M2 measured
  14-20 req/s with one), for about +1 to +1.8 GB RAM on the host (one more backend process, M2
  RSS). Fits the existing `PG_MAX_CONNECTIONS=200`.
- `UVICORN_WORKERS=4`: about four times the ceiling, for about +3 to +5.4 GB RAM, and needs
  `PG_MAX_CONNECTIONS` raised to 300 (more Postgres memory under worst-case load). Small
  machines (8 GB) would struggle.
- `1` (today): no extra memory, keeps the measured ceiling.
The sizing guide documents all three either way. The plan proceeds with 2 unless told otherwise.

Defaults taken without asking (best practice, reversible by env var): 503 + `Retry-After` on
overload, `DB_POOL_TIMEOUT_SECONDS` 5, API `statement_timeout` 30 s, request deadline 55 s,
readiness never failing on load, login audit events through the outbox, the SPA retrying an
idempotent request once on 503.

---

## 11. Sequencing relative to the final load pass (task #37)

1. The final pass runs **only after `scale-w2` is tagged**, on that tree, from a checkout nobody
   edits during the window (hot reload restarts the backend on any `.py` edit under
   `backend/app/`).
2. Preconditions (M2 "Final-run plan", updated):
   - quiet host (1-minute load < 25% of `nproc`; no suites, builds or other load tests); record
     `uptime`/`nproc` before and after each run;
   - the **new snapshot** from lane G (sequences and activity applied), sha256 verified; the M2
     activity workaround and `setval` steps are no longer needed;
   - `auto_explain` at 1 s without analyze; `pg_stat_statements` reset per run;
   - no finding-1 suppression (P0 is fixed: verify Redis `used_memory` stays flat during the
     first run instead) and no thumbnail wait (lane D 5.2: verify the second boot dispatches 0);
   - the prod process model (lane B), with `UVICORN_WORKERS` recorded in each run's `env.txt`;
   - GPU 1, plus GPU 2 only if `nvidia-smi` shows it idle; never GPU 0.
3. Order (one at a time, `scripts/perf/load/tools/chain.sh` from lane G): F01-F14 exactly as in
   M2's "Final-run plan", plus:
   - `F05w1`: F05 at `UVICORN_WORKERS=1` (the process-model control for F05);
   - `F15-profile`: steady-100 mixed with py-spy attached (section 3.4), not used for latency
     numbers;
   - `F16-overload`: steady-300 read-only with `DB_POOL_SIZE=5 DB_MAX_OVERFLOW=0` to prove the
     overload path is 503-only.
4. Compare F05 vs M2 run 07, F05w1 vs F05, F08 vs M2 run 12, and the M1 single-request table;
   fill the "after" table in `docs-site/docs/operations/scaling-and-sizing.md` and write
   `~/ot-cleanup-work/scale/final/RESULTS.md` with the verification line of every section above
   marked met / not met.
5. Any item not met becomes a fix in the same release if small (repo rule: fix, do not defer
   related findings), or a GitHub issue with its numbers if major.
