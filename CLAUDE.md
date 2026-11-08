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
- **Ask before anything destructive.** GPU 1 (RTX 3080 Ti) is this project's primary card.
  ⛔ GPU 0 genuinely runs unrelated work (`tritonserver`, plus a standalone `b3c` /
  `diar-server:issue1` container) and **must never be touched**. **GPU 2 (RTX A6000) IS usable
  by this project** — it generally hosts this machine's vLLM, but that is not always running.
  **Check `nvidia-smi`; if GPU 2 is idle, USE IT and run the multi-GPU tests** rather than
  skipping them (owner's instruction, 2026-09-05).
  ⚠️ Treating this host as single-GPU is a documented cost, not a safe default: it left #711's
  cross-card verification unrun for weeks and made #713 record a *hardware* blocker for
  something that only needed an idle card. `--gpu-scale`, `gpu-split`, `-m gpu`,
  `run-diarization-gpu-tests.sh` and the `--with-llm-test` legs all belong on GPU 1 + GPU 2.

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
Surgical recreate (leaves data services running) — the one case the script doesn't cover.
`--no-deps` skips the one-shot `migrate` service and the prod API does not migrate itself
(`RUN_MIGRATIONS_ON_STARTUP=false`), so run the migrate step first or `/health/ready` stays 503
on a new revision:
```bash
F="-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.nginx.yml \
   -f docker-compose.local.yml -f docker-compose.pki.yml"
docker compose $F run --rm --no-deps migrate
docker compose $F up -d --no-deps --force-recreate frontend backend
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

**Use `--fresh` for any throwaway or experimental stack — never develop against the live NAS data.** `./opentr.sh start dev --fresh <name> [--port-offset N]` runs in its own `otfresh-<name>` compose project (separate containers AND named volumes) and never loads the NAS/bind overlay; `./opentr.sh fresh-list`, `fresh-destroy <name>` (the ONLY destructive fresh op) and `data-paths` (check before deleting anything) round it out. If you see a `.opentranscribe-live-data` marker, you are looking at live data: do not delete. Port offsetting, `--with-*` overlay isolation (issue #347, policed by `backend/tests/unit/test_opentr_fresh_aux_isolation.py`), the exemptions and the NAS overlay: `scripts/notes/fresh-deployments.md` (root-`CLAUDE.md` text moved intact to its end) and `docs-site/docs/operations/fresh-deployments.md`.

### Test overlays (auth IdPs, mock LLM, mock ASR)

`./opentr.sh start dev --with-ldap-test | --with-keycloak-test | --with-authentik-test | --with-mock-llm | --with-mock-asr`, and `start prod --build --with-pki` for PKI/mTLS. Never start `mock-llm-server.py` as a bare host process. Flags, credentials and ports, the Keycloak pre-built-image story (do not raise its `start_period` without re-measuring) and the mock scenario models: `scripts/notes/test-overlays.md`.
### Multi-GPU worker scaling (optional)

Run with `./opentr.sh start dev --gpu-scale` — **the flag is what enables scaling**, not `GPU_SCALE_ENABLED` (see `backend/app/tasks/CLAUDE.md`). It sets `COMPOSE_PROFILES=gpu-scale` and loads `docker-compose.gpu-scale.yml`, running N parallel Celery workers in one container against `GPU_SCALE_DEVICE_ID`. Tune `GPU_SCALE_WORKERS` to your VRAM. Whether the default single-GPU worker also stays up depends on `GPU_SCALE_DEFAULT_WORKER` — `0` in compose, but `1` in `.env.example`.

### Docker build & push (production images)

Skill: `.claude/skills/docker-build-push/SKILL.md` (any build including `linux/arm64` requires
`USE_REMOTE_BUILDER=true`, or it runs under QEMU).

**Capability lives in the REPOSITORY and is restated in the TAG** (issue #680):
`opentranscribe-backend` is CUDA, `opentranscribe-backend-lite` is CPU, and each publishes
`vX.Y.Z-<cap>-<arch>` legs assembled into a `vX.Y.Z` index whose digest `:latest` is then copied
from. **`PLATFORMS` is an explicit override, not a default** — the old both-arch default is how a
CPU-only arm64 backend got published under the CUDA image's tag. Never transcribe the platform
set into a doc or a script: ask `./scripts/docker-build-push.sh list-platforms`
(`component<TAB>capability<TAB>platforms`), which is the single home for it. Full table and the
reason `cuda-arm64` is reserved-but-unbuilt: `scripts/notes/docker-build-push.md`.

### Cutting a release — `./scripts/release.sh`, never by hand

**Do not hand-run git tag / docker push / gh release.** `./scripts/release.sh status | explain <stage> | preflight <v> | run <v> [--dry-run]`; stages are `preflight, bump, verify, test, build, scan, rehearse, tag, publish, smoke, promote, finish`, and `tag` / `publish` / `promote` / `finish` reach outside the repo (they need `--yes` and their `ask` rule in `.claude/settings.json`). Gates are overridable only with `--force-<stage> "reason"`, recorded in the ledger. `rehearse` requires the live stack stopped. Stage contracts, exit codes, the four rehearsal scenarios and the version-facts rule: `scripts/release/CLAUDE.md` (root-`CLAUDE.md` text moved intact to its end), `docs-site/docs/developer-guide/releasing.md`, `.claude/skills/release/SKILL.md`.

### Backend / venv

Alembic runs automatically on dev backend startup — `alembic upgrade head` is production-only.

Host venv for pre-commit / mypy / ruff / bandit / pytest outside Docker lives at `backend/venv/` and already exists. If it doesn't: `cd backend && (python3.13 -m venv venv || python3.12 -m venv venv) && source venv/bin/activate && pip install -r requirements.txt && pip install --no-deps -r requirements-nodeps.txt && pip install -r requirements-dev.txt`. The venv here is **3.12.3** (the image is 3.13). Install `requirements-dev.txt`, not a bare `pip install mypy`: its mypy and `types-*` pins are the ones the mypy pre-commit hook mirrors. The `requirements-nodeps.txt` step is mandatory, not optional — it installs whisperx/faster-whisper/gliner, and skipping it leaves the venv without a working ASR/redaction stack. This is the exact two-step install `Dockerfile.prod` runs; see `requirements-nodeps.txt`'s header for why those three packages need `--no-deps`.

### Pre-commit / lint hooks

**Every commit must pass pre-commit — no exceptions, and never `--no-verify`.** The hooks are installed locally and are the *same* checks CI runs in its "Run Pre-commit Hooks" job, so anything you skip locally fails the PR instead. Running `ruff` (or any single hook) by hand is **not** a substitute: mypy and prettier catch a different class of problem and have both blocked a PR that was otherwise green locally.

Run the full suite before committing — not just the staged subset, and through the
concurrency-guarded wrapper, not bare `pre-commit` (issue #434):

```bash
scripts/safe-precommit.sh run --all-files                      # commit-stage tier
scripts/safe-precommit.sh run --all-files --hook-stage pre-push # push-stage tier
```

**The gate is TIERED (issue #688), and CI runs BOTH tiers.** The commit stage is the fast
edit loop (eslint + svelte-check, bandit over the staged files); the push stage carries the
two whole-tree jobs that dominated it — the frontend production `vite build` and
`bandit -r backend/`. ⚠️ `pre-commit run --all-files` with no `--hook-stage` runs
**default-stage hooks only** (`default_stages: [pre-commit]`), so "I ran pre-commit" no
longer means "I ran everything CI runs" — run the second line too before pushing.
`.github/workflows/pre-commit.yml` has a step per stage and
`backend/tests/unit/test_precommit_stage_ci_parity.py` fails if a hook ever sits in a stage
CI does not invoke.

**One-time local setup, per checkout:** `pre-commit install --install-hooks` — the config's
`default_install_hook_types` now wires `pre-commit`, `commit-msg` and `pre-push`, but it
only affects a *future* install; an already-installed checkout has just the `pre-commit`
script and neither the push tier nor conventional-commit message linting fires until you
re-run it. ⚠️ **Never run it from a git worktree**: `.git/hooks` there resolves to the
shared common git dir, so you would rewrite the hooks of the main checkout and every other
worktree at once.

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
> **The hazard is CONCURRENCY, not who is committing.** Read this as a rule about how many
> writers share the checkout, and do not over-apply it into "a subagent may never commit" —
> that costs a pointless round trip on solo work and gets ignored as obviously too strict:
>
> | Shape | Commit? | Why |
> |---|---|---|
> | A **lone** writer — one agent, sole occupant of the checkout | ✅ yes | Nothing else in flight to stash |
> | An agent in **its own worktree** | ✅ yes | Separate index and stash; no cross-lane exposure |
> | The orchestrator, after **every** dispatched writer has reported | ✅ yes | Tree is quiet |
> | **Two or more writers on one branch**, each committing | ⛔ **never** | Each run stashes the other N−1 mid-edit |
>
> Only the last row is forbidden — but it is the one that keeps happening, because each writer
> individually looks safe to itself.

The incident write-ups (background subagents you just dispatched count as writers, a test run is a writer's victim, the three observed failure modes, the staged-snapshot trap) and the multi-writer conventions are in `docs/agent-workflow/pre-commit-and-concurrent-writers.md`. **Read it before dispatching more than one writer into a checkout.**

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
./scripts/run-integration-tests.sh        # THE pre-merge gate: unit/API suite, the
                                          # security suites re-run under FIPS_MODE=true,
                                          # integration + gpu marked tests, schema drift
                                          # flags: --coverage --e2e-smoke --cleanup (+ more: see its header)
```

**Testing from a git worktree without the dev stack:** `scripts/run-backend-tests.sh --throwaway-db <pytest args>` starts a throwaway Postgres on a free port, migrates it with that worktree's own alembic, and removes it on every exit (`backend/tests/notes/db-tests-without-stack.md`).

For a quick "does my current branch work" check during normal development — backend gate + e2e
+ frontend check, chained, one consolidated report — use `./scripts/run-dev-tests.sh --full`
(or `--fast` for a smoke-e2e variant, `--backend-only`/`--e2e-only`/`--frontend-only` for a
single phase; mode flags compose). It auto-starts/stops whatever auth/LLM test overlays the
requested phase needs (mock-LLM, Keycloak, LDAP) and reconciles the DB config it flips back on
exit — `--all-overlays`/`--with-gpu-scale`/`--no-overlays`/`--list-overlays`/`--dry-run` control
this; see `scripts/notes/dev-test-gate.md` for the full flag table. This is **not** the same job as
`scripts/test-matrix.sh` (the exhaustive deployment-mode rehearsal across
dev/prod/lite/PKI/GPU-scale/fresh-install/upgrade, run before cutting a release, not during
ordinary development).

MinIO/OpenSearch-backed tests **auto-enable** when the dev stack is reachable (conftest TCP-probes localhost:5178/5180) and skip otherwise. Backend coverage is **ENFORCED, not report-only**: `backend/pyproject.toml`'s `fail_under = 37` is a hard pytest-cov gate — a run below the floor exits non-zero. It is a **ratchet floor** set just below the measured value, so it fails on a *regression*, not on failing to improve. Raise it when coverage rises; never lower it to make a run pass. Frontend coverage (`npm run test:coverage`) genuinely is report-only.

### Four tools that keep the suite honest (issue #431)

A test that cannot fail is worse than no test. `python3 scripts/audit-tests.py backend/tests` (21 AST detectors, exits 1 on new offenders), `cd frontend && npm run test:audit`, `python3 scripts/analyze-test-timing.py <junit.xml>`, and the opt-in `./scripts/run-mutation-tests.sh --module <m>`. Allowlists are keyed `<file>::<test>::<category>` with a written reason; never loosen a test to kill a mutant. Detail and the measured lessons: `scripts/audit_tests/CLAUDE.md` (root text moved intact to its end).

### E2E (pytest + Playwright)

`./scripts/e2e/run-e2e.sh` (full, headless), `run-e2e-smoke.sh`, `DISPLAY=:11 pytest backend/tests/e2e/ -v --headed`. **E2E suites must never persist changes to dev data**; negative login tests use a nonexistent account, never wrong passwords for `admin@example.com`. Fixtures, markers, auth-vs-testing rules and the `RUN_*` gates: `backend/tests/CLAUDE.md`, `backend/tests/e2e/CLAUDE.md` (root text moved intact to its end).

### Browser automation (interactive debugging)

System tool at `~/bin/browser-tools/browse.js` — opens URL, runs actions (`fill:`, `click:`, `screenshot:`, `wait:`, `eval:`), captures console errors. Full action list and setup in `~/bin/browser-tools/README.md`. On XRDP, pass `--display=:13`.

## Model Caching

Configured via `MODEL_CACHE_DIR` in `.env` (default `./models`). Volumes mount each cache (`huggingface`, `torch`, `nltk_data`, `sentence-transformers`, `opensearch-ml`) into the container's `~/.cache/...`. `opensearch-ml` is also mounted read-only at `/ml-models` in the OpenSearch container.

Models persist across rebuilds (~2.5 GB total). Permissions auto-fixed by `./opentr.sh` startup; manual fix: `./scripts/fix-model-permissions.sh` (chowns to `$CONTAINER_UID_GID`, default **1000:999** — the non-root `appuser`, whose GID is 999 because the Dockerfile uses `groupadd -r`; issue #580).

## Where subsystem detail lives

These load automatically when you work under the directory — read them before changing that
subsystem, and put new subsystem detail **there**, not in this file.

| Subsystem | File |
|---|---|
| Backend orientation | `backend/CLAUDE.md` |
| Every other nested backend file (api/endpoints, scim, tags, auth/oidc, auth/proxy, services/clustering, erasure_receipt, search/indexing, speaker_matching, tasks/transcription, tests/eval, tests/fixtures) | `git ls-files 'backend/**/CLAUDE.md'` |
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
| **RAG/chat: what is measured, what is NOT, and what to do next** | **issue [#461](https://github.com/attevon-llc/OpenTranscribe/issues/461)** — CLOSED, but still the map: read its phased execution order before touching retrieval. Of the six `epic:rag-quality` issues #461 deferred (#462, #464, #506, #523, #526, #532), five are now CLOSED with real measured outcomes (#464/#506/#523 shipped or correctly evaluated-negative; #462/#526 closed earlier). **#532 is the only one still open** — fully implemented and unit/oracle-tested (`docs/design/532_hybrid_summary_synthesis_plan.md`), with only its GPU validation measurement remaining, deliberately deferred pending GPU-2 capacity. |
| **Release themes: what each version is FOR, and its exit criteria** | `docs-site/docs/developer-guide/roadmap.md` (Developer Guide → Release Themes). The **live** issue/milestone view is the generated `/roadmap` page — never hand-edit its data, run `python3 scripts/generate-roadmap.py`. |
| Pluggable ASR providers | `backend/app/services/asr/CLAUDE.md` |
| Pluggable diarization providers | `backend/app/services/diarization/CLAUDE.md` |
| OpenSearch indexing + neural/hybrid search | `backend/app/services/search/CLAUDE.md` |
| Content redaction (PII / toxicity / profanity) | `backend/app/services/redaction/CLAUDE.md` |
| Watch sources (local / S3 / SMB auto-import) | `backend/app/services/watch_sources/CLAUDE.md` |
| Word / PDF transcript documents with numbered lines | `backend/app/services/transcript_document/CLAUDE.md` |
| Test suite: markers, gates, E2E fixtures | `backend/tests/CLAUDE.md` |
| Repo scripts + destructive-op warnings (detail moved down: `scripts/{audit_tests,e2e,pki,release,release-tests}/CLAUDE.md`, `scripts/notes/`) | `scripts/CLAUDE.md` |
| Release pipeline (12 stages, ledger, gates) | `docs-site/docs/developer-guide/releasing.md` |
| Full local test matrix (4 stages, overlay sub-matrix) | `docs-site/docs/developer-guide/full-test-matrix.md` |
| Frontend SPA (+ one file per folder under `frontend/src/`: `git ls-files 'frontend/src/**/CLAUDE.md'`) | `frontend/CLAUDE.md` |
| Docs site, generated tables + architecture diagram drift checks | `docs-site/CLAUDE.md` |

> **Cosine score conversion (repo-wide trap):** OpenSearch `cosinesimil` returns `(1 + cosine) / 2`, NOT raw cosine. Every kNN score read must do `raw_cosine = 2.0 * hit["_score"] - 1.0`. All 10 read sites (measured) live in the speaker/voiceprint plane under `backend/app/services/` (none in `api/`; transcript search ranks by RRF, never raw cosine) and all route through the helper; `test_cosine_conversion_discipline.py` fails on any inline conversion. **It applies to WRITES too** (issue #674): a threshold sent *into* OpenSearch (`min_score`) needs the inverse, `(1 + raw_cosine) / 2`, and no read-site audit can find a bad one. Both directions are named functions in `backend/app/utils/cosine_space.py` — call them. Full table: `backend/app/services/search/CLAUDE.md`.

> **Chat retrieval trap (issue #52), as amended by the redaction policy of 2026-08-13:** the
> `transcript_chunks` index stores transcript text **UNREDACTED**. Whether it must be masked before
> an LLM sees it depends on **where the model runs**: a **local** model receives it unmasked (the
> text never leaves the machine, so masking costs recall and buys nothing), a **remote provider**
> still gets masked text (sending unredacted PII to a third party is a data-egress event). Key that
> off the **provider**, never a global setting.
>
> ✅ **The provider keying is BUILT and shipped**, not just decided. `chat/context_prep.py`'s `_mask_evidence`
> (called from `_prepare_context`) resolves `redaction.llm_guard.is_local_provider(llm.config)` once per turn (via `chat/masking.py`'s
> `_build_mask_kwargs` / `_build_digest_mask_kwargs`) and threads it as
> `unmask_for_local` to all three masking call sites (`mask_chunks`, and both `mask_digests` calls).
> A **local** model (a `vllm`/`ollama`/`custom` config whose `base_url` resolves to
> loopback/RFC1918/link-local/a docker-compose hostname) receives excerpt text unmasked; a
> **remote/cloud provider** still gets masked text. The classification **fails closed** — any
> ambiguity reads as remote. An admin's `redaction.force_redact_before_llm` lock always wins the
> local exemption. Full detail: `backend/app/services/chat/CLAUDE.md`. **Output redaction**
> (`services/chat/output_redactor.py`, masks what the model *writes*, gated on
> `cfg.enabled and cfg.enabled_categories`) is a separate, independent layer — the **display**
> policy, not the `redact_before_llm` **egress** policy this section describes.
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

- **Issues**: `gh issue create` bypasses form validation, so pass `--label` (a type label plus every applicable area label); then add the issue to the org Roadmap project and set Status/Priority/Epic/Target/milestone (`gh project item-add 1 --owner attevon-llc --url <issue-url>`). Full procedure, project ID and field handling: `.github/CLAUDE.md`.
- **Docker compose layering**: base `docker-compose.yml` + auto-loaded `docker-compose.override.yml` (dev) OR explicit `-f` flags for prod / nginx / pki / offline / gpu-scale / local. Mixing dev + prod requires explicit flags (override is NOT auto-loaded then).
- Always `docker compose` (with space), never the legacy `docker-compose`.
- Conventional commits: `<type>(<scope>): <summary>`.
- `.env` is never overwritten without confirmation; `.env.example` is the editable template — keep new vars in sync.
- Keep code files under ~300 lines; Google-style Python docstrings; light/dark mode parity for any frontend change.
- **i18n is a requirement of EVERY release.** Any user-facing copy lands in all **12** locales (`frontend/src/lib/i18n/locales`: ar de en es fr it ja ko nl pt ru zh; `ar` is RTL). `npm run check:i18n` enforces key PARITY, not translation, and a commit touching only locale JSON does not fire the pre-commit hook: run `cd frontend && npm run check:i18n` yourself. The two traps in full: `frontend/CLAUDE.md` ("i18n").
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
- **More than one writer in a checkout? ONE of them commits**, everyone else hands over exact paths plus a commit message. Inside a stash window your uncommitted work is GONE from disk and a green test run can have tested the OLD code: check the run used YOUR code. To watch a test fail against old code use a `git archive HEAD` tree (`ot-redcheck.` prefix), never swap files in the shared checkout. `audit-tests` is a whole-tree gate; commit with an explicit pathspec (and `git add` untracked files first). The reasoning and incidents: `docs/agent-workflow/pre-commit-and-concurrent-writers.md`.
- **Every `.py` edit under `backend/app/` restarts the hot-reloading dev backend, and startup
  dispatches `search_index_maintenance`.** That corrupted three reindexes in one day. Batch app-file
  edits, and announce a measurement or reindex window before starting one. ⚠️ Until 2026-09-07 this
  was worse than the sentence says: `./backend:/app` is bind-mounted and the command was a bare
  `uvicorn --reload`, so **any `.py` anywhere under `backend/` restarted the API** — including
  `backend/tests/e2e/*`, i.e. editing a *test* file took the backend down for ~15 s mid-run (the
  reloader parent holds the listening socket, so the port stays open and answers nothing, which is
  the blank-SPA shape behind most `wait_for_selector` e2e timeouts). Fixed by `--reload-dir app` in
  `docker-compose.override.yml`; the sentence above is now literally true.
