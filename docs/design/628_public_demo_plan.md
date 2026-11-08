# #628 public read-only demo: implementation plan (E1 inert, E3 upgraded by a release)

Status: plan written 2026-11-07 on branch `v070/demo-628`, verified against that tree. The
design record (rejected alternatives, Immich comparison, cost) is the issue's linked gist and its
2026-09-09 restatement comment; this file is the **implementable** plan and is self-standing.

Owner decisions adopted (from the v0.7.0 decision list, recommended option in each case):

| Id | Decision |
|---|---|
| D5 | Visitors sign in without a password through a demo-session endpoint for one read-only demo user; exempt from lockout, limited per IP. Until it ships, the shared demo credentials work through the allowlisted login routes. |
| D6 | Reset on every upgrade, plus nightly at 04:00 UTC, from a versioned seed bundle. |
| D7 | No read-only Postgres role for v0.7.0. The app-layer refusal plus the #898 checker is the control. |
| D8 | `t4g.xlarge` arm64, provisioned by the owner (account, instance, DNS, TLS). |
| D9 | Launch the demo on a pre-release image, so publishing v0.7.0 is the visible upgrade. |
| D10 | Pull-based updater on the demo host; no deploy credentials stored in GitHub. |
| Owner (2026-10-01) | Chat serves pre-generated answers or is disabled, never a live LLM call. Seed content is open datasets and public-domain news or science media. Acceptance test: a demo session cannot enqueue any Celery task. |

Chat in phase 1 is **disabled** (every chat write is refused by the HTTP guard). Pre-generated
conversations are a seed-bundle follow-up (section 6, P-D).

---

## 1. Inventory: what exists and what is missing

### 1.1 Exists (E1)

| What | Where | Notes |
|---|---|---|
| Publish counter on the one `before_task_publish` receiver | `backend/app/core/inert.py:65-96`, receiver in `backend/app/core/celery.py` | Counts every `delay`/`apply_async`/`send_task`; self-test probe; NOT MEASURED state (#898). |
| Static route-to-publish reachability model | `backend/app/core/inert_reach.py`, `backend/app/api/inert_routes.py:56-102` | Branches on `Route` / `WebSocketRoute` / `Mount`; unknown types fail closed. |
| Gate CLI | `backend/app/scripts/assert_inert.py` (`python -m app.scripts.assert_inert [--json]`, exit 0/1/2/3) | |
| Ratchet of GET routes that enqueue today (9) | `backend/tests/unit/test_inert_checker.py:32-42` | `/files/{uuid}` and four siblings (lazy redaction scan), `download-stream`, `/admin/stats`, `/system/stats`, `/auth/oidc/callback`. |
| Beat entries classified READ_ONLY / MUTATING / INGEST | `backend/app/core/celery_beat.py:234-280` | `BEAT_ENTRY_EFFECT`; one READ_ONLY entry (`gpu-stats-update`). |
| Per-IP resolution behind trusted proxies | `backend/app/utils/client_ip.py` | Right-to-left XFF walk; `RATE_LIMIT_TRUSTED_PROXIES` defaults coded in every proxy overlay (`docker-compose.nginx.yml:39`), pinned by `test_proxy_trust_overlays.py`, empirical two-IP test in `test_668_nginx_hardening.py`. |
| nginx streams bodies (no disk fill before a 403) | `nginx/site.conf.template` `proxy_request_buffering off` | Pinned by `test_668_nginx_hardening.py`. |
| robots.txt and `X-Robots-Tag` on the API | `frontend/static/robots.txt`, `backend/app/middleware/robots.py` | |
| CPU-only image, published multi-arch | `docker-compose.lite.yml`, `scripts/docker-build-push.sh` (lite component) | Lite workers' GPU replicas are 0. |
| Upload body limit middleware (pure ASGI pattern) | `backend/app/middleware/upload_limit.py` | The template for a pure ASGI guard. |
| `MAX_CONCURRENT_SESSIONS=0` means unlimited | `backend/app/services/session_cap_service.py` | |

### 1.2 Exists (E3)

| What | Where |
|---|---|
| Pinned-tag upgrade with a health wait | `opentranscribe.sh update --version vX.Y.Z` (rewrites `OT_IMAGE_TAG`, records `# OT_PREVIOUS_IMAGE_TAG`, phased restart polling `/health` for 15 min) |
| Rollback to the previous tag | `opentranscribe.sh update --rollback` |
| Config files for a tag | `opentranscribe.sh update-full` (reads `release-manifest.txt` at the pinned ref) |
| Every compose image pinned through `OT_IMAGE_TAG` | `docker-compose.prod.yml`, `docker-compose.lite.yml` |
| The About modal shows the build tag | `frontend/src/components/AboutModal.svelte` |

### 1.3 Missing (this lane implements the ones marked "here")

| Gap | Where it lands | Lane |
|---|---|---|
| `DEPLOYMENT_MODE` is a bare `str`; a third value silently disarms the ASR guard (`services/asr/factory.py:34`) | validator on `Settings.DEPLOYMENT_MODE` + vocabulary test | here |
| `DEMO_MODE` setting | `backend/app/core/config.py` | here |
| Server-side refusal of every write, failing closed, HTTP **and** WebSocket | new pure ASGI `backend/app/middleware/demo_readonly.py` | here |
| Structural test that fails when a new write route is not refused | new `backend/tests/unit/test_demo_readonly_coverage.py` | here |
| Enqueue-side kill switch (the 9 GET routes, beat, anything missed) | Celery app `send_task` refusal in demo mode | here |
| Lazy redaction claim writes a row on a GET | skip the claim under demo (`api/endpoints/files/redaction_gate.py`) | here |
| Global per-IP request ceiling for a public instance | slowapi `default_limits` + `SlowAPIASGIMiddleware` under demo | here |
| Demo overlay, `--demo` flag, installer selection | `docker-compose.demo.yml`, `opentr.sh`, `opentranscribe.sh`, `release-manifest.txt` | here |
| Seed bundle format, licence policy, seeder | `backend/app/scripts/demo_seed/`, `demo/seed-catalog.json` | here (format, validator, importer); bundle **build** is a GPU-host task (section 6) |
| Reset and pull-based upgrade with health check and rollback | `scripts/demo/demo-reset.sh`, `scripts/demo/demo-upgrade.sh`, systemd units | here |
| Runbook | `docs-site/docs/operations/public-demo.md` | here |
| Demo-session endpoint (D5), "Try the demo" button, read-only banner with next-reset time in 12 locales, disabled-with-tooltip controls | `backend/app/api/endpoints/auth/demo_session.py`, `frontend/src/...` | follow-up P-B (needs a frontend caller in the same change for `test_route_has_a_caller`) |
| Pre-generated chat conversations in the seed | seed bundle `conversations/` | follow-up P-D |
| `finish`-stage criterion `demo-upgraded` | `scripts/release/` + `release-criteria.yaml` | L-E3 / L-RELEASE (CF-release, serial) |
| Version + upgrade time on the login page | frontend | follow-up P-B |

---

## 2. Architecture: four independent layers (each fails closed on its own)

| Layer | Mechanism | Proof |
|---|---|---|
| L1 HTTP | `DemoReadOnlyMiddleware`, pure ASGI, registered on every deployment, active only when `settings.DEMO_MODE` (read per request). Unsafe method (`POST/PUT/PATCH/DELETE` or anything not `GET/HEAD/OPTIONS`) is refused with **403** `{"detail": ..., "code": "demo_read_only"}` unless `"METHOD /exact/path"` is in `DEMO_WRITE_ALLOWLIST` (each entry carries a written reason). Exact match after `rstrip("/")`; the allowlist holds only parameter-free paths, so there is no templating to bypass. A **WebSocket** scope is closed before accept (`starlette.websockets.WebSocketClose(1008)`), which the ASGI spec turns into an HTTP 403 for the handshake. Registered right after `UploadBodyLimitMiddleware` so it runs **outside** it (a refused upload never reads its body) and **inside** CORS (the 403 carries CORS headers). | `test_demo_readonly_coverage.py`: every write route of the real app is refused, every allowlist entry names a real route, every reason is non-empty, the websocket is refused, and a synthetic write route and a synthetic websocket are reported (must-fire). |
| L2 Enqueue | `celery_app` is a `Celery` subclass whose `send_task` raises `DemoPublishRefused` when `DEMO_MODE` is on. `Task.apply_async`/`delay`, chains, groups and chords all publish through `Celery.send_task` (celery 5.6.3 `app/task.py` `apply_async` returns `app.send_task(...)`), and `before_task_publish` fires after it, so the #898 counter stays at zero. A signal receiver cannot do this job: Celery's `Signal.send` returns receiver exceptions instead of raising them. | unit test: in demo mode `.delay`, `.apply_async`, `send_task` and a chain all raise and the publish counter does not move; out of demo mode they publish. The `assert_inert` crawl on the live demo is the end-to-end proof. |
| L3 Topology | `docker-compose.demo.yml`: every Celery worker, `celery-beat` and `flower` scaled to 0; telemetry forced off; backend bound to loopback; `DEMO_MODE=true`, `DEPLOYMENT_MODE` stays `lite`. Seeding runs in a separate phase of the reset script with workers up and `DEMO_MODE=false`, nginx stopped (the public port is closed for the whole window). | `scripts/lite-smoke.sh` (no GPU worker, no device memory); `docker compose ... config` assertions in `test_demo_overlay.py`. |
| L4 Image | Lite image has no whisperx/CUDA; `services/asr/factory.py` raises for `lite`. The validator makes the vocabulary closed so a new mode cannot fall through. | `test_deployment_mode_vocabulary.py` |

GET routes that enqueue (the #898 ratchet): L2 refuses the publish. Each site already handles a
publish error (redaction: withdraws the claim; GPU stats: caught; download-stream: only reached
after a refused POST; OIDC callback: OIDC is not configured on the demo). The redaction site is
additionally changed to **skip the claim** under demo, so a GET does not write
`redaction_status`; the seeder guarantees every seeded file has a terminal redaction status.

GET routes that write the database directly (not Celery): found by the live row-count diff
(section 7, V4). Any found is fixed in the route, not allowlisted.

---

## 3. Settings and environment

| Variable | Where read | Demo value | Default |
|---|---|---|---|
| `DEMO_MODE` | `Settings.DEMO_MODE` (`env_bool`) | `true` | `false` |
| `DEMO_RATE_LIMIT_PER_MINUTE` | `Settings.DEMO_RATE_LIMIT_PER_MINUTE` | `120` | `120` (only applied under demo) |
| `DEPLOYMENT_MODE` | validator: `{"full", "lite"}`, normalised strip+lower, anything else raises at startup | `lite` | `full` |
| `MAX_CONCURRENT_SESSIONS` | existing | `0` (verified unlimited) | `5` |
| `ACCOUNT_EXPIRATION_ENABLED` | existing | `false` | `false` |
| `OPENTRANSCRIBE_TELEMETRY` | existing | `false` | |
| `RATE_LIMIT_TRUSTED_PROXIES` | existing | `127.0.0.1/32,172.16.0.0/12` plus the upstream TLS terminator's subnet (ALB/CloudFront), **never** a wildcard | |
| `API_MEDIATED_UPLOAD_ENABLED` | existing | `false` | |

All new keys go into `.env.example` (`test_env_example_coverage`).

---

## 4. File-by-file change list (this lane)

Commits in this order, one concern each:

1. `docs(demo): plan` - this file.
2. `fix(config): close the DEPLOYMENT_MODE vocabulary`
   - `backend/app/core/config.py`: `VALID_DEPLOYMENT_MODES = frozenset({"full", "lite"})`, `field_validator("DEPLOYMENT_MODE")` normalising and rejecting.
   - `backend/tests/unit/test_deployment_mode_vocabulary.py`: unknown value rejected; every non-`full` member makes `_guard_local_allowed` raise; red-checked by adding a member with no branch.
3. `feat(demo): refuse every write in demo mode (HTTP and WebSocket)`
   - `backend/app/core/config.py`: `DEMO_MODE`, `DEMO_RATE_LIMIT_PER_MINUTE`.
   - new `backend/app/middleware/demo_readonly.py`; register in `backend/app/main.py`.
   - new `backend/tests/unit/test_demo_readonly_coverage.py`.
   - `.env.example`.
4. `feat(demo): refuse every Celery publish in demo mode`
   - `backend/app/core/celery.py`: `Celery` subclass with the `send_task` refusal; `DemoPublishRefused` in `backend/app/core/demo.py`.
   - `backend/app/api/endpoints/files/redaction_gate.py`: no lazy claim under demo.
   - new `backend/tests/unit/test_demo_publish_refusal.py`.
5. `feat(demo): per-IP request ceiling for a public instance`
   - `backend/app/auth/rate_limit.py`: `default_limits` under demo; `backend/app/main.py`: `SlowAPIASGIMiddleware` under demo.
   - tests: two client addresses behind a trusted proxy get independent buckets; a forged XFF from an untrusted peer is ignored.
6. `feat(demo): compose overlay, --demo flag and installer selection`
   - new `docker-compose.demo.yml`; `opentr.sh` (`--demo` on `start`/`reset`, help, compose chain, fresh isolation classification); `opentranscribe.sh` (`DEMO_MODE=true` in `.env` selects the overlay); `release-manifest.txt`.
   - new `backend/tests/unit/test_demo_overlay.py`; `test_opentr_fresh_aux_isolation.py` classification.
7. `feat(demo): seed bundle format, licence policy and importer`
   - new `backend/app/scripts/demo_seed/` (`bundle.py` schema + licence policy, `__main__.py` importer reusing `corpus_injection` row builders, MinIO upload, terminal redaction status, settle wait).
   - new `demo/seed-catalog.json` (sources, licence, attribution).
   - new `backend/tests/unit/test_demo_seed_bundle.py`.
8. `feat(demo): reset and pull-based upgrade with health check and rollback`
   - new `scripts/demo/demo-reset.sh`, `scripts/demo/demo-upgrade.sh`, `scripts/demo/systemd/*.{service,timer}`.
   - new `scripts/tests/test-demo-upgrade.sh` (stubbed `docker`/`curl`, exercises upgrade, no-op, failed health -> rollback).
9. `docs(demo): public demo runbook and changelog`
   - new `docs-site/docs/operations/public-demo.md`, `docs-site/sidebars.ts`, `CHANGELOG.md`.

### 4.1 `DEMO_WRITE_ALLOWLIST` (initial)

| Key | Reason |
|---|---|
| `POST /api/auth/login` | Visitors sign in with the published demo credentials. Creates a session row; sessions are destroyed by the nightly reset. |
| `POST /api/auth/token` | OAuth2 password form used by the API docs and clients; same effect as login. |
| `POST /api/auth/token/refresh` | Keeps a visitor's session alive; rotates the refresh token row only. |
| `POST /api/auth/logout` | Ends the visitor's own session. |
| `POST /api/auth/logout/all` | Ends the visitor's sessions; on a shared account this also signs other visitors out, accepted (they sign in again). |

Nothing else. Chat, search reindex, MFA setup, registration, password reset, invitations and
every admin route are refused.

### 4.2 Upgrade flow (`scripts/demo/demo-upgrade.sh`)

Runs on the demo host from the install directory (a normal `setup-opentranscribe.sh` install with
`DEPLOYMENT_MODE=lite` and `DEMO_MODE=true`), from a systemd timer every 15 minutes.

1. Take a lock (`flock`); refuse to overlap a reset.
2. Resolve the target: `DEMO_TARGET_TAG` if set (pre-release pin, D9), else the GitHub
   `releases/latest` `tag_name` (unauthenticated GET, no credentials on the host). Validate
   `^v[0-9]+\.[0-9]+\.[0-9]+$`.
3. Same as `OT_IMAGE_TAG`: exit 0 (no-op).
4. Record the current tag; `./opentranscribe.sh update --version <target>` then
   `./opentranscribe.sh update-full` (config files of that tag).
5. `scripts/demo/demo-reset.sh` (fresh seed against the new schema, D6).
6. Health: `/health` 200 **and** a content assertion: `GET /api/files` as the demo user returns
   at least `DEMO_MIN_FILES` items, and `GET /api/version` (or the About payload) reports the
   target tag.
7. On failure: `./opentranscribe.sh update --rollback`, `update-full`, reset again, health again;
   exit 1 either way so the timer's `OnFailure=` alerts.
8. Write `demo-status.json` (`{tag, previous_tag, upgraded_at, result}`) for the release
   `finish` stage to poll.

Because every upgrade resets from the seed (`down -v`), the one-way migration chain never blocks a
rollback on the demo.

### 4.3 Reset flow (`scripts/demo/demo-reset.sh`)

1. Stop nginx (the public port closes; this is the fence, no iptables needed).
2. `docker compose <chain> down -v` scoped by the install's compose project.
3. Seed phase: `DEMO_MODE=false` with workers scaled to 1 (`--scale`), nginx not started.
4. `python -m app.scripts.demo_seed import --bundle /seed/<version>` in the backend container;
   waits for the neural model `DEPLOYED`, index document count == seeded segments, zero files with
   `redaction_status IS NULL`.
5. Demo phase: recreate with `DEMO_MODE=true` (workers and beat back to 0), then start nginx.
6. Any failure exits non-zero **with nginx still stopped**.

---

## 5. Seed content policy

Allowed licences (machine-checked by the bundle validator): `CC0-1.0`, `CC-BY-4.0`,
`CC-BY-SA-4.0` (attribution shown on the file page), `PDM-1.0` (public domain mark) and
`US-GOV-PD` (works of the US federal government). Anything else is refused. Every item records
`source_url`, `license`, `license_url`, `attribution`, `sha256`. An item may not carry personal
data of private individuals: `contains_private_individuals` must be `false`. PII used to show
redaction must be invented and marked `synthetic_pii: true`.

Candidate sources (`demo/seed-catalog.json`), each to be confirmed on its licence page when the
bundle is built: AMI Meeting Corpus (CC BY 4.0, multi-speaker meetings, role-labelled), NASA
mission audio and briefings (US-GOV-PD), Voice of America news in several languages (US-GOV-PD,
third-party segments excluded), LibriVox recordings (public domain).

---

## 6. Sequencing after this lane

| Step | Needs | Owner |
|---|---|---|
| P-A merge this lane, wave sweep | lead | lead |
| P-B demo-session endpoint + "Try the demo" + banner + disabled controls, 12 locales, browser check light/dark/`ar` | P-A | Sonnet lane |
| P-C build seed bundle v1 on the GPU host (process each catalog item through the normal pipeline on a `--fresh` stack, export with `demo_seed export`, verify licences) | P-A, GPU 1 | lead |
| P-D pre-generated conversations into the bundle | P-C | lane |
| P-E `finish` criterion `demo-upgraded` | L-RELEASE | L-E3 |
| P-F stand up staging on the owner's instance from a pre-release tag (D9) | owner infra | lead + owner |
| P-G 3-5 day soak, nightly resets, zero integrity findings | P-F | lead |
| P-H flip `demoAvailable` on the website | P-G, ZAP triaged, #790 (or chat disabled, as here) | owner |

---

## 7. Verification

### Static (this lane, no stack)

- `test_deployment_mode_vocabulary.py`, `test_demo_readonly_coverage.py`, `test_demo_publish_refusal.py`, `test_demo_overlay.py`, `test_demo_seed_bundle.py`, `scripts/tests/test-demo-upgrade.sh`, each watched red on a `git archive HEAD` tree first.
- Whole-tree guards: `test_route_has_a_caller`, `test_openapi_snapshot_gate`, `test_env_example_coverage`, `test_platform_access_matrix`, `test_no_exception_echo`, `test_hold_freeze_coverage`, `test_inert_checker`, `test_opentr_fresh_aux_isolation`; `audit-tests.py tests` at 0 open.

### Live (lead, in order; never on `ldape2e`/`perf1337`)

- **V1 prod-build fresh stack with the demo overlay**: `./opentr.sh start prod --build --lite --demo --fresh demo1 --port-offset <N>`; `docker compose ... ps` shows no worker, beat or flower container; `scripts/lite-smoke.sh` green (exit 4 is NOT MEASURED, not a pass).
- **V2 XFF**: through nginx, two requests with different `X-Forwarded-For` from the same untrusted client must share one bucket (forgery ignored); two different real sources (or two trusted-proxy hops) must get independent buckets: hit `POST /api/auth/login` with a nonexistent account until 429 from source A, then confirm source B still gets 401.
- **V3 inertness probe across all routes**: sign in as the demo user; for every route in the app's route table issue the request (unsafe methods with an empty JSON body) and record the status. Every unsafe route not in the allowlist must be exactly `403` with `code == "demo_read_only"`; `/api/ws` handshake must be refused; then `python -m app.scripts.assert_inert --json` on the backend container plus `celery_tasks_published_total` from `/metrics` must show zero publishes after the crawl.
- **V4 row-count diff**: `SELECT relname, n_tup_ins + n_tup_upd + n_tup_del FROM pg_stat_user_tables` before and after V3; only `refresh_token`/session and auth-event tables may move.
- **V5 upgrade dry run**: on the fresh stack, `DEMO_TARGET_TAG=<current pre-release> scripts/demo/demo-upgrade.sh --dry-run` prints the exact command sequence; then a real run from one pre-release tag to the next; then a forced failure (`DEMO_MIN_FILES=100000`) must roll back and exit 1.
- **V6** egress: with the host firewall denying outbound except Docker Hub/GitHub, a 24 h run logs no refused outbound connection from app containers.

---

## 8. Needs the owner

- AWS account, `t4g.xlarge` instance, `demo.opentranscribe.app` DNS, TLS termination (ALB + ACM recommended; the repo has no ACME path). The ALB subnet goes into `RATE_LIMIT_TRUSTED_PROXIES`.
- The pre-release tag to launch from (D9).
- Confirmation of the seed items' licences once the bundle is built (P-C).
- Billing and disk alarms on the account.
