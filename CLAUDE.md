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

`--fresh` refuses to start when any port it needs is already bound (it offers `--port-offset N`) and generates a gitignored `.fresh/<name>.yml` overlay that re-pins every service to `otfresh-<name>-*`. `--port-offset` works by exporting the `*_PORT` vars the compose files already read — never by overlaying a second `ports:` list, which compose would append (issue #343). The offset is remembered in `.fresh/<name>.offset`. **Every** `--with-*` overlay is isolated too (issue #347) — names, ports, and volumes all move — and the overlays used are recorded in `.fresh/<name>.aux`. Do not maintain the list here by hand: it was stale for four overlays at once, and `--with-llm-test` was missing from `opentr.sh`'s dispatch itself, so a fresh stack collided with the main one on `opentranscribe-llm-test-vllm`/5195 and `fresh-destroy` left a multi-GB vLLM holding a GPU. `backend/tests/unit/test_opentr_fresh_aux_isolation.py` now enumerates the flags and fails on any that is neither isolated nor **explicitly exempt with a written reason**. The three exemptions are `--with-watch` / `--with-backup` (they bind live host directories and declare no `container_name` or ports, so there is nothing project-scoped to re-pin — `opentr.sh` warns instead) and `--with-pki` (a prod/nginx overlay that layers onto existing services). ⚠️ `LLM_TEST_GPU_DEVICE_ID` is deliberately **not** offset: a port offset must never renumber a physical card. Details: `scripts/CLAUDE.md`.

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
(streams a "thinking" phase before the answer — exercises the collapsible reasoning display.
⚠️ It separates reasoning **only when the request activated thinking** via
`chat_template_kwargs={"enable_thinking": true}`; unasked, it reproduces the real server's
#439 behaviour and leaks the thoughts into the answer, so the app's handling of that is
testable). Never start it as a
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

Run the full suite before committing — not just the staged subset, and through the
concurrency-guarded wrapper, not bare `pre-commit` (issue #434):

```bash
scripts/safe-precommit.sh run --all-files    # the gate CI mirrors
```

The wrapper (`scripts/safe-precommit.sh`, self-test: `scripts/safe-precommit-selftest.sh`)
refuses to start — rather than racing silently — when either of the two *known* unsafe
overlaps below is already in flight: another `safe-precommit.sh` run, or a
`run-mutation-tests.sh --verify` run holding one of its per-module locks. **It does not make
an arbitrary unstaged edit elsewhere in the tree safe** — only those two specific hazards.

> ⚠️ **NEVER run `pre-commit` OR `git commit` while anything else is writing to this
> checkout.** Not `--all-files`, not `--files <paths>`, not a plain `git commit`. **All three
> stash every unstaged change in the entire repo** — including another agent's in-flight work in
> files you are not touching — and restore it when the run ends. What you staged is irrelevant:
> the stash happens *before any hook runs* and covers the whole tree.
>
> This paragraph used to recommend `--files` or "just commit" as the safe alternative. **That
> advice was wrong and caused the incident below.** There is no safe alternative for a tree with
> unrelated unstaged work in progress; there is only waiting for a quiet tree. The wrapper above
> catches the two overlaps that have actually bitten this repo — a second pre-commit run, and a
> mutation `--verify` mutation left live — it is not a general fix for the hazard in this box.
>
> **"Anything else" includes background subagents you yourself just dispatched in this same
> turn.** An agent fanned out 8 parallel background subagents to edit disjoint files on one
> branch, then ran `git commit -- <2 files it had already verified itself>` while the other 7
> were still mid-edit — reasoning that an explicit pathspec made it safe. It does not: `git
> commit` stashes the whole tree regardless of pathspec, which would have stashed all 7
> in-flight agents' work the moment it ran. There is no "but these are my own subagents and I'm
> scoping the commit" exception. Wait for every dispatched writer to report done, review, THEN
> run one clean commit/precommit pass.
>
> Three failure modes, all observed here:
>
> 1. **Another writer's work is stashed mid-edit.** An agent's `Edit` failed with "file has been
>    modified", it re-read and re-applied, and the restore then reinstated the *earlier* draft,
>    silently discarding the newer one. Caught only by an unrelated `git diff`. Earlier the same
>    day, a different agent's `conftest.py` was stashed during a failing commit and restored with
>    `Stashed changes conflicted with hook auto-fixes... Rolling back fixes` — recovered, but by
>    luck. If a hook crashes in that window the only copy is
>    `~/.cache/pre-commit/patch<timestamp>-<pid>`.
> 2. **A whole-tree hook fails against a tree that never existed.** `frontend-check` scans all of
>    `frontend/src`, so with an unstaged type change stashed away and an untracked test file left
>    behind, svelte-check failed with 5 errors about a property that *does* exist. Nothing was
>    wrong. The natural response — "fixing" correct code — makes it worse.
> 3. **Spurious `files were modified by this hook`** with no findings at all (bandit printing
>    "No issues identified", frontend-check printing "All frontend checks passed"). The
>    stash/restore moved the files, not the hook.
>
> A related trap that is *not* about concurrency: pre-commit's hooks see the **staged** snapshot.
> If you `git add` a file and then edit it further, mypy/ruff check the stale staged copy and
> report errors you have already fixed. Re-`git add` before committing.
>
> `--all-files` is always correct in CI, where nothing else is writing.

Hook inventory is in `.pre-commit-config.yaml`. The frontend hook only fires when `frontend/src/**/*.{svelte,ts,js,css,html}` is staged. Note that `prettier` **rewrites files** and then reports failure — re-stage and re-run, don't "fix" anything by hand.

### ⚠️ Fix the finding, never silence it

**A hook failure is information, not an obstacle.** The lint gate has already caught real defects in
this repo that review missed — an `Any` used but never imported, a `!=` status assertion that
passes on a 500, a test whose every assertion sat inside a loop over an empty list. Silencing any
of those would have shipped the bug with a clean gate.

The test to apply: **would this change still be an improvement if the linter were deleted
tomorrow?** A fix makes the code better; a suppression only makes the tool quieter.

| Legitimate | Not legitimate |
|---|---|
| Re-staging after `prettier` / `ruff format` **rewrote** the file — they mutate, then report failure. That is the documented workflow, not a bypass. | Adding `# noqa`, `# type: ignore`, `# fmt: off`, or an eslint-disable to make a real finding go away |
| A **type annotation** that tells the checker what an untyped third-party call returns (`response: dict[str, Any] = client.search(...)`). Every downstream use is still checked. | **Widening to `Any`** to stop mypy complaining about a genuine mismatch. That deletes the check. |
| `# noqa: <RULE>` **with a written reason**, where the rule is genuinely inapplicable and no restructuring removes it | Moving an existing `noqa` so the tool finally honours it, without asking whether the suppression was ever right |
| Fixing a test so the auditor's finding disappears | Adding an `audit-allowlist.txt` entry to clear a finding you could fix |
| `--no-verify` | **Never.** Not once, not "just this commit". |

**Worked example, because the wrong version looked reasonable.** `scripts/benchmark_rag.py` raised
`E402` on an import that must follow a `sys.path.insert`. The first attempt relocated a stale
`noqa` so ruff would honour it — quieter tool, unchanged code. The actual fix was to move the
import **into the function that uses it**, which the same file already did eleven lines further
down for the identical reason. Zero suppressions, ruff clean, and the file got *simpler*. Reaching
for `noqa` also nearly buried the `F821` beside it: `Any` was used and never imported.

**When a hook fails, read the finding before deciding it is spurious.** Roughly half the failures
blamed on the stash window (above) were real.

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

### Four tools that keep the suite honest (issue #431)

A test that cannot fail is worse than no test: it buys false confidence and hides the defect it
was written to catch. These exist because this repo had shipped all four failure modes — an
assertion that passed against an empty index, a `gpu` marker that selected nothing, 240 security
tests gated off behind stale env vars, and a progress endpoint returning a hardcoded value that
no test referenced.

```bash
python3 scripts/audit-tests.py backend/tests        # 16 AST detectors, exits 1 on new offenders
cd frontend && npm run test:audit                   # the vitest sibling, 10 detectors
npm run test:audit:selftest                         #   ...and ITS 21-case self-test
python3 scripts/analyze-test-timing.py <junit.xml> [--baseline baseline.xml]
./scripts/run-mutation-tests.sh --module spans      # opt-in, never in the gate or CI
```

- **The auditors are allowlist-gated**, keyed `<file>::<test>::<category>` with a mandatory
  written reason. The category is part of the key on purpose: an entry keyed by test alone once
  exempted a test from all six detectors at once.
- **`--selftest` is not optional ceremony.** It caught two detectors in each auditor that matched
  *nothing* — silently reporting 0 findings, which is indistinguishable from a clean suite. Any
  new detector needs a must-fire case and a must-stay-clean case.
- **`analyze-test-timing.py` finds barriers, not just slow tests.** Unrelated tests from many
  files sharing a sub-second duration band is a released lock queue, not a coincidence; that is
  how one worker was found owning 81% of the wall clock.
- **Coverage says a line RAN; mutation testing says the suite would NOTICE if it were wrong.**
  Scoped to six security-critical modules. A surviving mutant is a finding — add the missing
  assertion, or conclude the line is dead and delete it. Never loosen a test to kill one.
- **Profile before theorising about test speed.** Two plausible hypotheses cost two full
  measurement cycles on the Redis-retry bug; `python -m cProfile -o out.prof -m pytest <test>`
  found it in one.

Current (measured 2026-08-13, load average ~10 on 48 cores — quote the command, not the
number, if you are unsure): backend **6,623 passed / 62 real skips / 104 s** (from
4,752 / 458 / 511 s); frontend **669 passed / 76 files / 21.6 s**; e2e **341 collected,
271 passed / 1 failed** (`test_promote_publishes_to_the_shared_vocabulary`), plus 8
visual-regression baselines currently failing. The junit XML reports 146 skipped because
it counts the 84 xfails; 62 is the real skip count.

**These numbers rot — re-derive rather than trust them.** The previous values above were
wrong by 1,294 backend tests and 188 frontend tests when checked. `./scripts/run-backend-tests.sh
--summary` and `cd frontend && npm run test` answer in seconds.

Barrier clusters: none remain at sub-second scale, but a residual ~9 s DDL cluster does —
21 of the 35 tests over 5 s are `v3xx_migration_consistency` tests from 8 different modules
all landing near 9 s, which is the `ddl_exclusive` advisory-lock queue. DDL modules are
~418 s of the ~1,197 s summed CPU. Far better than the 414-of-511 s it started from, but
"zero barrier clusters" overstates it. Regenerate the timing baseline with
`./scripts/run-backend-tests.sh && cp /tmp/ot-backend-tests/last.xml baseline.xml` — it is
gitignored, because a committed measurement rots.

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
| Deterministic ingest artifacts (facts / extractive digest / keyphrases, no LLM) | `backend/app/services/ingest_artifacts/CLAUDE.md` |
| RAG chat pipeline (retrieval, masking, prompting) | `backend/app/services/chat/CLAUDE.md` |
| **RAG design: the standard patterns and what runs them** | `docs-site/docs/developer-guide/rag-design-and-validation.md` |
| **RAG evaluation: how quality is measured, and the traps** | `docs-site/docs/developer-guide/rag-evaluation.md` |
| **RAG/chat: what is measured, what is NOT, and what to do next** | **issue [#461](https://github.com/attevon-llc/OpenTranscribe/issues/461)** — opens with a phased execution order. Start there before touching retrieval. |
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

> **Chat retrieval trap (issue #52), as amended by the redaction policy of 2026-08-13:** the
> `transcript_chunks` index stores transcript text **UNREDACTED**. Whether it must be masked before
> an LLM sees it depends on **where the model runs**: a **local** model receives it unmasked (the
> text never leaves the machine, so masking costs recall and buys nothing), a **remote provider**
> still gets masked text (sending unredacted PII to a third party is a data-egress event). Key that
> off the **provider**, never a global setting.
>
> ⚠️ **The provider keying is DECIDED, NOT BUILT.** No code branches on the provider — only the
> CLAUDE.md files were amended — so **input masking applies to every provider today** and a local
> deployment is *not* currently less protected than before the decision. **Output redaction landed
> first, deliberately**: `services/chat/output_redactor.py` masks what the model *writes*,
> sentence-buffered, gated on `cfg.enabled and cfg.enabled_categories` (the **display** policy, not
> the `redact_before_llm` **egress** policy). Land the provider keying before it and the gap is
> real, between two commits, on a deployment that believes it is protected.
>
> ⚠️ **Two maskers, not interchangeable.** `redactor.mask_chunks()` addresses text by **time
> range**; `redactor.mask_digests()` by **provenance** (`segment_ids`). A digest through the chunk
> path is rebuilt from every segment in its span and comes back as the **whole recording
> verbatim** — more text than the digest held, from a function whose name says it masked it. Both
> fail closed, at different units: a chunk whole, a digest per sentence.
>
> ⚠️ **Ranking is not mapping.** `retrieve_digests` ranks; `mapreduce.scope_digest_hits` maps. Using
> the ranked leg as the map step produced a summary headed "recordings: 8" over a 25-file scope.
> Raising `size` does not fix it — ranking gives no coverage guarantee at any K.
>
> Equally: in chat scope resolution `file_uuids=None` means "all accessible" while `file_uuids=[]`
> means "match nothing" — inverting those leaks the whole library. Details:
> `backend/app/services/chat/CLAUDE.md`.

## Conventions

- **`gh issue create` bypasses issue-form validation** — the required "Area" dropdown in
  `.github/ISSUE_TEMPLATE/{bug_report,feature_request,task}.yml` only gets enforced by the GitHub
  web UI, not the API/CLI. When creating an issue with `gh issue create`, always pass `--label`
  with a type label (`bug` | `enhancement` | `task` | `documentation`) plus every applicable area
  label from the bank: `backend`, `frontend`, `asr`, `search`, `rag-chat`, `llm-provider`, `gpu`,
  `docker`, `security`, `performance`, `testing`. Check `gh label list` for the current bank
  before inventing a new one — five topic labels (`asr`/`search`/`rag-chat`/`llm-provider`/`gpu`)
  already exist for grouping the ASR/RAG/search issue clusters. An issue with none of these gets
  auto-tagged `needs-triage` by `.github/workflows/label-from-template.yml` — don't rely on that
  as the normal path, it's the fallback for issues that skip the form.
- **A labeled issue is not a tracked issue — it also needs to be on the org Roadmap project.**
  `gh issue create` does not add the issue to a project or set its milestone; that's a second,
  separate step. `.github/workflows/label-from-template.yml` only applies labels, it does not
  touch the project board. After creating (or materially re-scoping) an issue:
  1. Add it to the board: `gh project item-add 1 --owner attevon-llc --url <issue-url>`.
  2. Set `Status` (`Backlog`/`Ready`/`In Progress`/`In Review`/`Done`), `Priority`
     (`P0`–`P3`), and `Epic` (single-select matching the `epic:*` label if one applies, else the
     closest topic) via `gh project item-edit --id <item-id> --field-id <field-id> --project-id
     PVT_kwDOEFrMRc4Bge4t --single-select-option-id <option-id>`. Look up current field/option IDs
     with `gh project field-list 1 --owner attevon-llc --format json` — they are not hardcoded
     here because options get added/renamed over time.
  3. Set `Target` (date field, same `item-edit` pattern with `--date YYYY-MM-DD`) using real
     prerequisites, not a guess — check what the issue's own body says it depends on (`#NNN`
     references) and never date it before an issue it depends on. Two issues that reference each
     other are a dependency chain, not just "related": tag both with a shared `epic:<name>` label
     (`gh label create epic:<name> ...` if one doesn't already exist) so the chain is visible in
     search, not just in the roadmap's date ordering.
  4. If the work is part of an actual release push (not just "eventually"), assign the matching
     `gh issue edit <n> --milestone "vX.Y.Z"` — create the milestone first with `gh api
     repos/attevon-llc/OpenTranscribe/milestones` if it doesn't exist yet. Don't backdate a
     milestone-scoped issue's target ahead of issues it depends on that aren't in that milestone.
  5. **Priority is not the same as sequence.** Every issue in an active build chain can legitimately
     be `P1` — priority says "does this matter for the current push", the `Target` date and the
     `epic:*` label are what encode order and dependency. Don't set `Target` dates by copying a
     sibling issue's date; derive them from the actual `#NNN` cross-references in each issue body.
  The org project is `https://github.com/orgs/attevon-llc/projects/1` ("OpenTranscribe Roadmap"),
  project ID `PVT_kwDOEFrMRc4Bge4t`.
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
- **More than one writer in a checkout? ONE of them commits.** Everyone else hands over exact
  paths plus a commit message. This is not style — pre-commit **stashes the entire worktree** on
  every run (issue #434), so N writers each running their own commit-retry loop means each one's
  hook run is what makes the other N−1 fail with `files were modified by this hook` while the
  hook itself reports no findings. Four agents in one checkout produced four destroyed work
  sets, ~25-minute commit blocks, and a patch file in `~/.cache/pre-commit/` every two minutes
  before this rule was adopted. Serialising cost nothing and every lane landed within the hour.
- **Inside a stash window, your uncommitted work is simply GONE from disk — and the failure can
  present as your own code having never been written.** Three symptoms of the same cause, all
  observed here: the phantom `files were modified by this hook`; the E2E backend flapping; and
  a module reverting to its last-committed version, so a test that passed sixty seconds ago
  fails with `ImportError: cannot import name '<the thing you just wrote>'`. Someone nearly
  spent an afternoon on a non-existent import cycle. **Before debugging a sudden impossible
  failure, check whether another writer is running pre-commit.**
  ⚠️ **And do NOT back up your files during that window — you will back up the stash.** Copying
  to a safe directory mid-stash captures the *reverted* content, and "restoring" from it
  destroys the work for real. Wait for the restore, verify the file actually contains your
  change, and only then copy. This was caught once by diffing the backup against the live file
  instead of trusting the copy.
  ☠️ **The worst variant is a GREEN run: a test suite can pass having tested the OLD code.**
  Observed — a suite reported `13 passed` while the tree was stashed, having imported the
  committed versions of both the source and the test file. A minute later the same command
  reported `2 failed, 11 passed`, because that run caught the app file restored and the test
  file not. **Neither number meant anything**, and the only tell was that the *test names* in
  the output did not match the names just written. A red run makes you look; a green one gets
  recorded as evidence and you move on. So: **when a run is your evidence, check it ran YOUR
  code** — new test names present, a marker string in the output, or a deliberate failure you
  expect to see. A pass you cannot attribute is not a measurement.
- **To watch a test fail against the OLD code, use a `git archive HEAD` tree — never swap files
  in the shared checkout.** The repo's standard is that a test you have not seen red is not
  evidence, so this is done often. Reverting a file in place and putting it back costs two
  backend hot-reloads (each dispatching `search_index_maintenance`), leaves the fix off disk in
  a window where a stash can capture the reverted state, and races every other writer:

  ```bash
  git archive HEAD | (mkdir -p /tmp/redcheck && tar -x -C /tmp/redcheck)
  cp backend/tests/.../test_the_new_one.py /tmp/redcheck/backend/tests/.../   # new tests, old source
  cd /tmp/redcheck/backend && <run them; expect red>
  ```

  Immune to stash windows, costs no reloads, disturbs nobody, and the tree you are testing is
  provably HEAD rather than "what I think I reverted".
- **`audit-tests` is a WHOLE-TREE gate, so an unfinished test file blocks everyone.** One
  in-progress test anywhere under `backend/tests` with an open finding refuses **every** commit
  in the worktree, including commits that do not touch it — twice in one day a lane's own
  next-unit test file was refusing its own finished work. Before reporting a unit ready, run
  `cd backend && python3 ../scripts/audit-tests.py tests` and get to 0 open findings. And do not
  assume a hook failure is the stash bug: **read the finding first.** Roughly half of the ones
  blamed on contention were real.
- **Commit with an explicit pathspec, and remember staging is not protection.**
  `git commit -- <paths>` takes **worktree** content for those paths and leaves everyone else's
  staged work alone; a bare `git commit` in a shared index sweeps up whatever others have staged.
  Pass files, not a directory — a directory pathspec silently swept in two untested modules that
  happened to live beside the finished ones. Note `git commit -- <path>` fails on an **untracked**
  file: `git add` it first, then commit with the pathspec.
- **Every `.py` edit under `backend/app/` restarts the hot-reloading dev backend, and startup
  dispatches `search_index_maintenance`.** That corrupted three reindexes in one day. Batch app-file
  edits, and announce a measurement or reindex window before starting one.
