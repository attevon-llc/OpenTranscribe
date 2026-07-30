# backend/app/core — config, constants, Celery, cross-cutting singletons

## Purpose

Process-level infrastructure that every other package imports: settings, constants, the Celery
app, security primitives, Prometheus collectors, and the cloud-edition seams. Nothing here
should import `app.api` or `app.services` at module scope.

## Key files

- `config.py` — pydantic-settings `Settings`; the module-level `settings` singleton is the only
  supported import. `extra="ignore"`, so an undeclared/typo'd compose env var is silently
  dropped rather than crashing startup.
- `constants.py` — magic numbers, `CeleryQueues` (**single source of truth for queue names**),
  the `OPENSEARCH_EMBEDDING_MODELS` tiers, and the `DEFAULT_*` values backing DB-stored settings
  (redaction, watch sources, engine/boundary). **Check here before adding a `.env` var.**
- `celery.py` — `celery_app`, explicit `task_queues`, `task_routes`, beat schedule. Patches
  `torch.load(weights_only=False)` *before* any ML import; the whole ML block is skipped when
  `SKIP_CELERY=true` (test startup).
- `enums.py` — centralized enums (`FileStatus`), imported from here instead of model modules to
  break import cycles.
- `exceptions.py` — the `OpenTranscribeError` hierarchy, handled globally in `main.py`.
  **Service-layer only** — endpoints keep raising `HTTPException` (see its docstring).
- `redis.py` — `get_redis()`, the process-wide **sync Redis (db 0) singleton**. Don't call
  `redis.from_url` directly; the documented exceptions (auth rate-limit/lockout, cache db 1,
  async clients) are listed in its docstring.
- `capabilities.py` — server-driven feature gating; `require_capability()` dependency,
  `set_capability_resolver()` for the cloud edition, plus the audience taxonomy.
- `settings_cache.py` — in-process TTL cache in front of `SystemSettings` reads.
- `security.py` — password hashing (FIPS-aware PBKDF2 iteration counts), `create_access_token`,
  `verify_token`. `auth_settings.py` layers DB config over `.env` for auth.
- `metrics.py`, `db_metrics.py`, `celery_metrics.py`, `backup_metrics.py`, `route_template.py` —
  Prometheus collectors on the default registry, plus the bounded route-label resolver.
- `tenancy.py` — `UNSCOPED` sentinel + `OrgScope`; stdlib-only so any layer can import it.
  `tenant_limits.py` — per-org limit resolver hooks (community no-op).

## Conventions / patterns

- Queue names always come from `CeleryQueues`; `task_create_missing_queues=False` turns a typo
  into a dispatch-time error instead of a silent phantom queue.
- Prometheus labels must stay bounded — never `user_id`, raw request paths, or SQL text. Route
  labels use the post-routing **template** via `route_template.route_label`.
- Cloud behavior is injected through registry hooks, never `if edition == "cloud"` in feature code.

## Gotchas

- **Import-linter (pre-commit) forbids `app.*` from importing `cloud`, `clerk`, or `stripe`.**
  Contract: `backend/pyproject.toml [tool.importlinter]`; the hook runs `lint-imports` from
  `backend/`. Keep seams as resolver hooks so the static AST check stays clean.
- `SETTINGS_CACHE_TTL` (30 s) staleness is **cross-process by design**: an admin change is
  instant in the API process but up to 30 s stale in Celery workers. The cache fully bypasses
  when `TESTING=true`, read from `os.environ` at call time.
- `celery_metrics`: kombu shards priority queues into `f"{queue}\x06\x16{prio}"` lists — a bare
  `LLEN <queue>` **undercounts**; depth is the sum over all 10 sub-lists.
- `db_metrics` per-request counting stores a **mutable dict** in a ContextVar: `BaseHTTPMiddleware`
  runs `call_next` in a child task, so re-`set()`ing the var would not propagate back.
- `FileStatus` is `(str, enum.Enum)` and deliberately **not** `StrEnum` — `str(FileStatus.X) ==
  "FileStatus.X"` is pinned by characterization tests and redaction/analytics comparisons.
- The single-process Prometheus registry assumes **one** uvicorn worker (prod CMD has no
  `--workers`).
