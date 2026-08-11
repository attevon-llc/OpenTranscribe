# CLAUDE.md

Guidance for Claude Code working in this repository. This file holds only what applies
**everywhere**. Subsystem detail lives in nested `CLAUDE.md` files that load when you work in
that directory — see the map at the bottom. Long-form docs live in `docs-site/docs/`.

## ⚠️ CRITICAL: Always use `./opentr.sh` for the stack — never bare `docker compose`

**Start, stop, restart, shell, and logs go through `./opentr.sh` (e.g. `./opentr.sh start dev`).** It composes the correct overlay set (base + dev override + GPU/storage/etc.) so containers get the **correct database, MinIO storage, and env**. Bare `docker compose up/restart/exec` skips those overlays and can attach to a differently-configured stack — symptoms: a backend that ran migrations against the wrong DB, "table/relation does not exist" errors, or storage pointing at the wrong volume. If something looks schema-broken, first re-launch with `./opentr.sh start dev` (rebuilds + runs startup migrations against the right DB) before debugging further. Tests run against the **live stack**, so the stack must be up via `./opentr.sh start dev` (not bare compose) for backend integration/E2E tests to hit the correct DB/storage.

**This rule overrides any general "prefer `docker compose exec <service>`" guidance** from user-level or global instructions: in this repo, use `./opentr.sh shell <service>` to get a shell and `./opentr.sh logs <service>` for logs. The documented exceptions are the rebuild command in the next section and read-only cross-service inspection (`docker ps`, `docker compose logs`), which can't attach you to the wrong database.

## ⚠️ CRITICAL: Never kill processes or containers to "clean up"

**Do not run `pkill`, `killall`, `kill -9`, or `nvidia-smi --gpu-reset`.** These are blocked by
`permissions.deny` in `.claude/settings.json`; this section is the reason why.

A blanket kill previously took down a **CUDA process mid-transcription and wedged the GPU, requiring
a full machine restart** — twice. A CUDA context that dies under `SIGKILL` does not always release
the device; the driver is left in a state no userspace command can recover. The same class of
cleanup ("kill everything related before starting fresh") has also destroyed running containers and
their state.

Instead:
- Stop the stack with `./opentr.sh stop`, restart one service with `./opentr.sh restart-backend` /
  `restart-frontend`.
- To free a GPU, stop the worker container — never signal the process inside it.
- If a task looks stuck, read `./opentr.sh logs celery-worker` first. A long transcription is
  usually still running.
- **Ask before anything destructive.** GPU 1 (RTX 3080 Ti) is this project's only GPU; GPUs 0 and 2
  are reserved for unrelated work and must never be touched.

## ⚠️ CRITICAL: Local Code vs Docker Hub Images

**Production/nginx/PKI overlays serve pre-built images from Docker Hub. Your local code changes are NOT included until you rebuild.** Symptom: 404s on new endpoints, missing UI features.

Why: `docker-compose.prod.yml` references `davidamacey/opentranscribe-*:latest`. `docker-compose.local.yml` sets `pull_policy: never` but does not bind-mount source. The dev `docker-compose.override.yml` (Vite hot-reload) is **not** auto-loaded when you pass explicit `-f` flags.

**Use the script — don't hand-assemble `docker` / `docker compose` invocations.** Every stack
operation has a repeatable `./opentr.sh` entry point, and the script derives the overlay chain
for you (a hand-written `-f` list silently drifts as overlays are added):

```bash
./opentr.sh start prod --build              # build prod images from Dockerfile.prod, then start prod
./opentr.sh start prod --build --with-pki   # same, with the PKI/nginx overlay
./opentr.sh rebuild-backend [--nas]         # dev: rebuild backend services in place (--no-deps)
./opentr.sh rebuild-frontend                # dev: rebuild frontend in place (--no-deps)
./opentr.sh build                           # dev: rebuild all images without starting
```

`--build` runs `build_prod_images()`, which builds **backend, frontend, and docs** from their
`Dockerfile.prod` files — the docs image is easy to forget by hand.

<!-- Escape hatch below: only for recreating frontend/backend WITHOUT restarting postgres/
     minio/opensearch. If you find yourself needing this often, add a flag to opentr.sh
     instead of pasting raw compose commands into new docs. -->
Surgical recreate (leaves data services running) — the one case the script doesn't cover:
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.nginx.yml \
               -f docker-compose.local.yml -f docker-compose.pki.yml \
               up -d --no-deps --force-recreate frontend backend
```

Caching: COPY layers are content-hashed. If a change isn't picked up, touch a file (`echo "<!-- $(date +%s) -->" >> path/to/file`) rather than `--no-cache` (which re-runs `npm ci`). Never run `./opentranscribe.sh update` against local builds — it pulls from Docker Hub.

## Project Overview

Containerized transcription app: **WhisperX** (transcription, 100+ languages) + **PyAnnote** (diarization, with our optimized fork) + optional LLM features. Service inventory lives in `docker-compose.yml`; stacks in the package manifests.

## Development Commands

### `./opentr.sh` — primary script

Run `./opentr.sh` with no arguments for full usage. The ones you'll reach for:

```bash
./opentr.sh start dev                  # start dev environment
./opentr.sh stop | status              # stop all services / service health
./opentr.sh logs|shell [service]       # backend|frontend|postgres|celery-worker
./opentr.sh restart-backend|-frontend  # restart one service
./opentr.sh backup | restore backups/<file>.sql
./opentr.sh reset dev                  # ⚠️ DELETES DATA, runs full migration chain
```

### Fresh / isolated deployments (data-safety guardrails)

**Use `--fresh` for any throwaway or experimental stack — never develop against the live NAS data again.** A fresh deployment runs in its own `otfresh-<name>` compose project (separate containers AND named volumes) and the NAS/bind overlay is **never** loaded, so the real dataset can't be touched.

```bash
./opentr.sh start dev --fresh test1 [--port-offset 100] [--seed-benchmark]
./opentr.sh stop|status --fresh test1        # stop keeps volumes
./opentr.sh fresh-list                       # list fresh deployments + volumes
./opentr.sh fresh-destroy test1              # containers+volumes (y/N; the ONLY destructive fresh op)
./opentr.sh data-paths                       # resolved LIVE data paths — CHECK before deleting anything
./opentr.sh start dev --dry-run              # print compose files + command, start nothing
```

`--fresh` refuses to start when any port it needs is already bound (it offers `--port-offset N`) and generates a gitignored `.fresh/<name>.yml` overlay that re-pins every service to `otfresh-<name>-*`. `--port-offset` works by exporting the `*_PORT` vars the compose files already read — never by overlaying a second `ports:` list, which compose would append (issue #343). The offset is remembered in `.fresh/<name>.offset`. The `--with-ldap-test` / `--with-smb-test` / `--with-monitoring` / `--with-keycloak-test` / `--with-authentik-test` overlays are isolated too (issue #347) — names, ports, and volumes all move — and the overlays used are recorded in `.fresh/<name>.aux`. `--with-watch` / `--with-backup` are **not**: they bind live host directories, and `opentr.sh` warns. Details: `scripts/CLAUDE.md`.

**NAS overlay** (non-fresh `start`): auto-detected from `.env`, announced with a `💾 NAS overlay AUTO-LOADED` banner; `--no-nas` suppresses, `--nas` opts in explicitly. When active it writes a `.opentranscribe-live-data` marker into each bind dir — **if you see that marker, you are looking at live data; do not delete.** Full map: `docs-site/docs/operations/fresh-deployments.md`.

### Auth method overlays

Configure auth via Admin UI (Settings → Authentication); DB config takes precedence over `.env`. Pass test-container flags to spin up local IdPs:

```bash
./opentr.sh start dev --with-ldap-test       # LDAP at localhost:3890, UI :17170 (admin/admin_password)
./opentr.sh start dev --with-keycloak-test   # a Keycloak IdP to test OIDC against, localhost:8180 (admin/admin)
./opentr.sh start dev --with-authentik-test  # an Authentik IdP to test OIDC against, localhost:9022 (bootstrap: admin@example.com/admin_password)
./opentr.sh start prod --build --with-pki    # PKI/mTLS at https://localhost:5182 (prod-only — Vite can't do mTLS)
```

### Mock LLM (chat / AI features without a model)

```bash
./opentr.sh start dev --with-mock-llm        # OpenAI-compatible mock at http://mock-llm:5199/v1
```

Runs `scripts/mock-llm-server.py` on the app network so chat, summarization and
topic extraction work with **no GPU, API key, or internet**. Only token generation
is canned — retrieval, redaction masking, citations, SSE and usage recording all
take their real paths. Scenario models drive the app's real error handling:
`mock-gpt` (normal), `mock-echo` (returns the prompt it was given — assert what the
app actually *sent*), `mock-empty`, `mock-error`, `mock-slow`, `mock-reasoning`
(streams a `delta.reasoning_content` "thinking" phase before the answer — exercises
the collapsible reasoning display). Never start it as a
bare host process: it binds 5199 and then blocks the container. Fixtures and the
full table: `backend/tests/CLAUDE.md`.
Combine flags as needed. PKI client certs: `scripts/pki/test-certs/clients/*.p12`.
Details: `backend/app/auth/CLAUDE.md`, `docs/PKI_SETUP.md`, `docs/LDAP_AUTH.md`, `docs/OIDC_SETUP.md`.

### Multi-GPU worker scaling (optional)

Run with `./opentr.sh start dev --gpu-scale` — **the flag is what enables scaling**, not `GPU_SCALE_ENABLED` (see `backend/app/tasks/CLAUDE.md`). It sets `COMPOSE_PROFILES=gpu-scale` and loads `docker-compose.gpu-scale.yml`, running N parallel Celery workers in one container against `GPU_SCALE_DEVICE_ID`. Tune `GPU_SCALE_WORKERS` to your VRAM. Whether the default single-GPU worker also stays up depends on `GPU_SCALE_DEFAULT_WORKER` — `0` in compose, but `1` in `.env.example`.

### Docker build & push (production images)

Skill: `.claude/skills/docker-build-push/SKILL.md` (multi-arch requires `USE_REMOTE_BUILDER=true`).

### Cutting a release — `./scripts/release.sh`, never by hand

**Do not hand-run git tag / docker push / gh release.** Everything mechanical is a
stage in `scripts/release/NN-<stage>.sh`; `scripts/release.sh` owns arg parsing,
the ledger, and dispatch.

```bash
./scripts/release.sh status                 # where am I? (ledger table)
./scripts/release.sh explain publish        # preconditions, side effects, reversibility
./scripts/release.sh reset 0.5.0            # clear rehearsal history before a REAL run
./scripts/release.sh preflight 0.5.0        # seconds — fails fast
./scripts/release.sh run 0.5.0              # the whole sequence
./scripts/release.sh run 0.5.0 --dry-run    # print every command, execute nothing
./scripts/release.sh scan 0.5.0 --force-scan "reason"   # override, recorded
```

Stage order (each independently runnable, skippable with `--skip`, resumable with
`--from`):

```
preflight → bump → verify → test → build → scan → rehearse
          → tag → publish → smoke → promote → finish
```

- **`tag` / `publish` / `promote` / `finish` reach outside this repo.** Each needs
  `--yes` **and** its `ask` rule in `.claude/settings.json`. They are the only
  stages that touch Docker Hub or GitHub.
- **Exit codes are stable** — `0` pass, `1` gate failed, `2` misuse, `3`
  precondition unmet (live stack up, builder unreachable), `4` operator abort.
  `--json` on any stage emits a `criteria[]` and a `next[]` of legal moves.
- **Gates are overridable, never silently.** `--force-<stage> "reason"` requires a
  reason (there is no bare `--force`) and records `overridden` + operator in the
  ledger — not a pass.
- **Ledger** lives in `.release/<version>/steps/` (gitignored). Rehearsal history
  reads as current until you `reset`, so reset before a real run.
- `rehearse` runs both scenarios and **requires the live stack stopped**; it
  refuses (exit 3) and prints the command rather than stopping it for you.

Full guide: `docs-site/docs/developer-guide/releasing.md` (Developer Guide →
Releasing). Agent interface: `.claude/skills/release/SKILL.md`. Gate definitions:
`scripts/release/release-criteria.yaml`.

**Version facts are DERIVED, never recorded.** The Alembic head comes from the
`down_revision` graph (`scripts/release-tests/lib/alembic-head.py`), and FROM/TO
from the `VERSION` file + Docker Hub. Never add a checked-in table of versions —
the last one (`expected-schemas.tsv`) went stale because it was hand-maintained
and read by nothing.

### Backend / venv

Alembic runs automatically on dev backend startup — `alembic upgrade head` is production-only.

Host venv for pre-commit / mypy / ruff / bandit / pytest outside Docker lives at `backend/venv/` and already exists. If it doesn't: `cd backend && python3.11 -m venv venv && source venv/bin/activate && pip install -r requirements.txt && pip install --no-deps -r requirements-nodeps.txt && pip install pre-commit mypy ruff bandit`. The `requirements-nodeps.txt` step is mandatory, not optional — it installs whisperx/faster-whisper/gliner, and skipping it leaves the venv without a working ASR/redaction stack. This is the exact two-step install `Dockerfile.prod` runs; see `requirements-nodeps.txt`'s header for why those three packages need `--no-deps`.

### Pre-commit / lint hooks

**Every commit must pass pre-commit — no exceptions, and never `--no-verify`.** The hooks are installed locally and are the *same* checks CI runs in its "Run Pre-commit Hooks" job, so anything you skip locally fails the PR instead. Running `ruff` (or any single hook) by hand is **not** a substitute: mypy and prettier catch a different class of problem and have both blocked a PR that was otherwise green locally.

Run the full suite before committing — not just the staged subset:

```bash
backend/venv/bin/pre-commit run --all-files    # the gate CI mirrors
```

Hook inventory is in `.pre-commit-config.yaml`. The frontend hook only fires when `frontend/src/**/*.{svelte,ts,js,css,html}` is staged. Note that `prettier` **rewrites files** and then reports failure — re-stage and re-run, don't "fix" anything by hand.

Manual frontend check: `./scripts/frontend-check.sh [--no-claude] [--check-only]`. Inside Claude Code: `/fix-frontend`.

## Testing

**Local-first.** GitHub Actions runs the unit/API suite (`backend-tests` job, fresh Postgres + CPU-only `requirements-ci.txt`) and vitest as a safety net; the COMPLETE suite needs the live stack and runs locally:

```bash
./scripts/run-integration-tests.sh        # THE pre-merge gate: ungated suite +
                                          # all RUN_*-gated security suites (both
                                          # FIPS modes) + integration-marked tests
                                          # flags: --coverage --e2e-smoke --cleanup
```

MinIO/OpenSearch-backed tests **auto-enable** when the dev stack is reachable (conftest TCP-probes localhost:5178/5180) and skip otherwise. Coverage is configured report-only (`pytest --cov=app`, `npm run test:coverage`).

### E2E (pytest + Playwright)

Tests in `backend/tests/e2e/` (auth, gallery, upload, search, settings, transcript editing, visual). Requires dev environment running (`./opentr.sh start dev`).

```bash
./scripts/e2e/run-e2e.sh                                        # full suite, headless
./scripts/e2e/run-e2e-smoke.sh                                  # quick subset
./scripts/e2e/run-e2e.sh -m upload                              # by marker (upload/search/settings/transcription/gallery/auth/visual)
DISPLAY=:11 pytest backend/tests/e2e/ -v --headed               # visible on XRDP
```
Fixtures, markers, and the `RUN_*` gates: `backend/tests/CLAUDE.md`. Test creds `admin@example.com` / `password`. **E2E suites must never persist changes to dev data** — upload tests delete what they create; edit tests use the cancel path.

Auth-vs-testing rules: the **dev stack relaxes auth security limits** (`docker-compose.override.yml`: rate limit 120/min, lockout threshold 100 — `DEV_*` tunable in `.env`; prod keeps the strict `.env` values since the override is never loaded there). Negative login tests must use a **nonexistent account**, never wrong passwords for `admin@example.com` (progressive per-account lockout poisons the whole suite). Frontend auth is **httpOnly-cookie based** — no JS-readable token; in-page API calls use `fetch(..., {credentials: 'same-origin'})`. `GET /api/auth/session` is the SPA's session probe (200 for anonymous, never 401).

### Browser automation (interactive debugging)

System tool at `~/bin/browser-tools/browse.js` — opens URL, runs actions (`fill:`, `click:`, `screenshot:`, `wait:`, `eval:`), captures console errors. Full action list and setup in `~/bin/browser-tools/README.md`. On XRDP, pass `--display=:13`.

## Model Caching

Configured via `MODEL_CACHE_DIR` in `.env` (default `./models`). Volumes mount each cache (`huggingface`, `torch`, `nltk_data`, `sentence-transformers`, `opensearch-ml`) into the container's `~/.cache/...`. `opensearch-ml` is also mounted read-only at `/ml-models` in the OpenSearch container.

Models persist across rebuilds (~2.5 GB total). Permissions auto-fixed by `./opentr.sh` startup; manual fix: `./scripts/fix-model-permissions.sh` (chowns to UID/GID 1000:1000 — the non-root container user).

## Where subsystem detail lives

These load automatically when you work under the directory — read them before changing that
subsystem, and put new subsystem detail **there**, not in this file.

| Subsystem | File |
|---|---|
| Backend orientation | `backend/CLAUDE.md` |
| ASR, hybrid mode, diarization, boundary correction | `backend/app/transcription/CLAUDE.md` |
| Celery pipeline, queues, GPU scaling | `backend/app/tasks/CLAUDE.md` |
| Auth methods, MFA, privilege model | `backend/app/auth/CLAUDE.md` |
| Migration runner + schema-change procedure | `backend/app/db/CLAUDE.md` |
| Writing an Alembic revision | `backend/alembic/CLAUDE.md` |
| API routers / endpoint conventions | `backend/app/api/CLAUDE.md` |
| SQLAlchemy models | `backend/app/models/CLAUDE.md` |
| Pydantic schemas / wire contracts | `backend/app/schemas/CLAUDE.md` |
| Config, constants, celery wiring | `backend/app/core/CLAUDE.md` |
| Shared backend helpers | `backend/app/utils/CLAUDE.md` |
| Services overview, LLM features, yt-dlp ingestion | `backend/app/services/CLAUDE.md` |
| RAG chat pipeline (retrieval, masking, prompting) | `backend/app/services/chat/CLAUDE.md` |
| Pluggable ASR providers | `backend/app/services/asr/CLAUDE.md` |
| Pluggable diarization providers | `backend/app/services/diarization/CLAUDE.md` |
| OpenSearch indexing + neural/hybrid search | `backend/app/services/search/CLAUDE.md` |
| Content redaction (PII / toxicity / profanity) | `backend/app/services/redaction/CLAUDE.md` |
| Watch sources (local / S3 / SMB auto-import) | `backend/app/services/watch_sources/CLAUDE.md` |
| Test suite: markers, gates, E2E fixtures | `backend/tests/CLAUDE.md` |
| Repo scripts + destructive-op warnings | `scripts/CLAUDE.md` |
| Release pipeline (12 stages, ledger, gates) | `docs-site/docs/developer-guide/releasing.md` |
| Frontend SPA (+ 24 folder-level files) | `frontend/CLAUDE.md` |

> **Cosine score conversion (repo-wide trap):** OpenSearch `cosinesimil` returns `(1 + cosine) / 2`, NOT raw cosine. Every kNN score read must do `raw_cosine = 2.0 * hit["_score"] - 1.0`. All 11 read sites live in the speaker/voiceprint plane under `backend/app/services/` (none in `api/`, and transcript search ranks by RRF, never raw cosine) — all 11 currently correct. Full table: `backend/app/services/search/CLAUDE.md`.

> **Chat retrieval trap (issue #52):** the `transcript_chunks` OpenSearch index stores
> transcript text **UNREDACTED** — correct for search over your own words, but it means
> any path sending chunk content to an LLM must first call
> `services/chat/redactor.mask_chunks()`. Masking fails CLOSED (an unmaskable chunk
> contributes nothing rather than going out raw). Equally: in chat scope resolution
> `file_uuids=None` means "all accessible" while `file_uuids=[]` means "match nothing" —
> inverting those leaks the whole library. Details: `backend/app/services/chat/CLAUDE.md`.

## Conventions

- **Docker compose layering**: base `docker-compose.yml` + auto-loaded `docker-compose.override.yml` (dev) OR explicit `-f` flags for prod / nginx / pki / offline / gpu-scale / local. Mixing dev + prod requires explicit flags (override is NOT auto-loaded then).
- Always `docker compose` (with space), never the legacy `docker-compose`.
- Conventional commits: `<type>(<scope>): <summary>`.
- `.env` is never overwritten without confirmation; `.env.example` is the editable template — keep new vars in sync.
- Keep code files under ~300 lines; Google-style Python docstrings; light/dark mode parity for any frontend change.
- No mocking in production code paths — mocks belong in test fixtures only.
- Real integration testing: if a test depends on Redis/Postgres/OpenSearch, run against the real service or document the dependency. Don't silently skip.
- **LLM speaker-ID suggestions are never auto-applied** — they are surfaced with confidence scores for manual verification only.
- Iterate on existing patterns before introducing a new one. If you do replace an implementation, **delete the old one** — never leave two paths doing the same job.
- Stay in scope: don't modify code unrelated to the task at hand.
- Python imports go at the top **except** heavy optional deps (torch, pyannote.metrics, meeteval), which are imported inside the function so modules stay importable on CPU-only workers.
- Settings that look like they need `.env` vars are often **DB-backed** `SystemSettings` with coded defaults in `backend/app/core/constants.py`, edited in the admin UI with no restart. Check before adding an env var.
- **Worktree → branch → PR, never a local merge onto `master`.** Use a `.claude/worktrees/<name>`
  git worktree for deep, issue/PR-scoped work. When it's done: commit and push *that branch* to
  origin — nothing more. Build and test the branch **itself** (relocate its checkout — e.g.
  remove the worktree and `git checkout <branch>` in the main repo — if it needs the main repo's
  `.env`/tooling; don't create a separate local merge of it into `master` to test "how it looks
  merged"). Only once the branch is fully green does a PR go open from it into `master`; `master`
  changes **only** via that merged PR, never via a local `git merge <branch>` on `master` pushed
  directly — that bypasses review and produces a merge commit nobody chose the message for.
