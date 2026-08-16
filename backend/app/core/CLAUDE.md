# backend/app/core — config, constants, Celery, cross-cutting singletons

## Purpose

Process-level infrastructure that every other package imports: settings, constants, the Celery
app, security primitives, Prometheus collectors, and the cloud-edition seams. Nothing here
should import `app.api` or `app.services` at module scope.

## Key files

- `config.py` — pydantic-settings `Settings`; the module-level `settings` singleton is the only
  supported import. `extra="ignore"`, so an undeclared/typo'd compose env var is silently
  dropped rather than crashing startup.
  **Gate every security control on `settings.is_hardened`, never on
  `ENVIRONMENT in ("production", "prod")`.** `ENVIRONMENT` defaults to `production` and only the
  closed `RELAXED_ENVIRONMENTS` set (`development`/`dev`/`testing`/`test`/`local`) relaxes
  anything, so an unset, empty, or misspelled value fails closed. The old form fails **open**,
  and because nothing passed `ENVIRONMENT` into the containers it was never true in any
  deployment — the default-secret refusal, `DEBUG` enforcement, Redis-password requirement, and
  cookie `Secure` flag were all dead code until #284 A0.3. Dev declares itself via the
  `x-dev-environment` anchor in `docker-compose.override.yml`, which prod never loads.
- `constants.py` — magic numbers, `CeleryQueues` (**single source of truth for queue names**),
  the `OPENSEARCH_EMBEDDING_MODELS` tiers, and the `DEFAULT_*` values backing DB-stored settings
  (redaction, watch sources, engine/boundary). **Check here before adding a `.env` var.**
- `celery.py` — `celery_app`, explicit `task_queues`, `task_routes`, beat schedule. Patches
  `torch.load(weights_only=False)` *before* any ML import; the whole ML block is skipped when
  `SKIP_CELERY=true` (test startup).
- `enums.py` — centralized enums (`FileStatus`), imported from here instead of model modules to
  break import cycles. `ReasoningOffSwitch` lives here rather than beside its probe because
  three layers read it (the service that measures it, the Pydantic response that carries it,
  and the chat path that gates on it).
- **`LLM_REASONING_*` in `constants.py` are a probe's INSTRUMENT SETTINGS, not tunables** —
  prompt, temperature 0, token ceiling, the 10% suppression tolerance and the 32-character
  floor. Changing one re-defines what "the off-switch works" means, so change it with the
  measurement that justifies the new value. The *result* is not here at all: it is a
  `SystemSettings` row keyed by a (provider, base_url, model) fingerprint, written only by
  the probe. Full rationale: `app/services/CLAUDE.md`.
- `exceptions.py` — the `OpenTranscribeError` hierarchy, handled globally in `main.py`.
  **Service-layer only** — endpoints keep raising `HTTPException` (see its docstring).
- `redis.py` — `get_redis()`, the process-wide **sync Redis (db 0) singleton**. Don't call
  `redis.from_url` directly; the documented exceptions (auth rate-limit/lockout, cache db 1,
  async clients) are listed in its docstring.
- `capabilities.py` — server-driven feature gating; `require_capability()` dependency,
  `set_capability_resolver()` for the cloud edition, plus the audience taxonomy.
- `settings_cache.py` — in-process TTL cache in front of `SystemSettings` reads.
- `opensearch_auth.py` — `opensearch_connection_kwargs()`, the single builder for every
  `OpenSearch(...)` client (search plane ×2, audit writer + reader, admin audit export).
  `OPENSEARCH_AUTH=basic` (default) reproduces the previous inline kwargs exactly;
  `sigv4` signs with the AWS credential chain for a managed domain and **forces
  `RequestsHttpConnection`** — `AWSV4SignerAuth` is a `requests` AuthBase, so under
  opensearch-py's default urllib3 transport it is silently ignored and every request
  goes out unsigned (a blanket 403). Don't build a client inline again.
- `security.py` — password hashing (FIPS-aware PBKDF2 iteration counts), `create_access_token`,
  `verify_token`. `auth_settings.py` layers DB config over `.env` for auth —
  `get_auth_settings(db)` returns a `DynamicAuthSettings` that resolves **DB > .env > coded
  default**. Resolve it **once per request** and pass it down: a handler that resolves it for
  enforcement but reads `settings.*` for the audit record documents a policy nobody applied.
- `auth_settings.py`'s `proxy_*` properties are the layered readers for trusted-header auth;
  the `.env` fallbacks (`PROXY_*` in `config.py`) exist mainly so `main.py`'s fail-closed
  boot guard can see `PROXY_ENABLED`/`PROXY_TRUSTED_PROXIES` before a DB session exists.
- `legacy_auth_env.py` — **input adapter, not a second implementation.** It translates the
  historical `KEYCLOAK_*` environment names onto the canonical `OIDC_*` ones *before* `Settings`
  is built, so nothing downstream (including `AuthConfigService.ENV_TO_CONFIG_MAPPING`) ever
  sees the old spelling. The legacy name **wins** when both are set. No removal is planned —
  renaming a user-owned `.env` is not something this project gets to do — but
  `deprecated_oidc_env_names()` drives one startup log line. This is one of only two files under
  `backend/app/` allowed to name the retired provider; `tests/unit/test_oidc_naming_invariant.py`
  fails the build otherwise.
- `request_context.py` — **the** per-request correlation `ContextVar`, in one place. There were
  two objects both *named* `"request_id"` (`middleware/audit.py` set one, `auth/audit.py` read
  the other); a `ContextVar`'s display name is documentation, not identity, so every audit event
  fell back to a fresh random id and a multi-event flow could not be reconstructed. Stdlib-only,
  so `middleware`, `auth`, `core` and the Celery hooks can all import it without a cycle.
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

- **Import-linter (pre-commit) forbids `app.*` from importing `cloud` or the managed edition's
  vendor packages.** The authoritative list is the contract itself —
  `backend/pyproject.toml [tool.importlinter]`; the hook runs `lint-imports` from `backend/`.
  Keep seams as resolver hooks so the static AST check stays clean. Don't restate the vendor
  names here: CI's seam guard greps `backend/app` and `frontend/src` for them and fails the
  build on any match, **including in docs** — that is what took master red at `2a71fb1`.
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
