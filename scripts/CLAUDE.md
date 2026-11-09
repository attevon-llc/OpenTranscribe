# scripts — test gates, build tooling, and one-off operations

## Purpose

~135 top-level shell/python scripts (`ls scripts/*.sh scripts/*.py | wc -l`; more under `release/`,
`release-tests/`, `e2e/`, `lib/`, `pki/`, `tests/`). **`scripts/README.md` (~900 lines) already documents the
build / offline-package / model-download / remote-builder family in depth — read it for those
and don't duplicate it here.** It does not mention the other ~60 files (test gates, e2e,
benchmarks, one-off SQL) and has **zero destructive-operation warnings**; that is what this file is for. Ratchet guards (`scripts/guards/`, baselines vs allowlists): `scripts/notes/ratchet-guards.md`.

## Which script to reach for

- **OpenAPI schema snapshot** (issue #798 step 1) — `generate-openapi.py --write` regenerates
  `backend/openapi.json`; `--check` (no default — the flag is mutually-exclusive-required)
  verifies it is current, which is what CI's `backend-tests` job runs as its
  "OpenAPI snapshot is current" step, right after `Verify app imports`. Any change to a
  request/response schema needs `backend/venv/bin/python scripts/generate-openapi.py --write`
  before committing, or CI fails. The venv is 3.12, CI is 3.13 — verified byte-identical output
  at current pins. `info.version` is normalized to a fixed sentinel so a release version bump
  alone never causes drift.
- **Platform access matrix** (issue #1281) — `generate-platform-access-matrix.py --write|--check`
  (backend venv; imports `app`) writes `docs/security/platform-access-matrix.tsv` from the route
  table and an AST scan via `backend/tests/fixtures/platform_access_matrix.py`. The same comparison
  runs as `tests/unit/test_platform_access_matrix.py`; read the diff of a regenerated file, a new
  row is a new way for platform staff to reach a tenant.
- **Docs generated from code** (issue #1230) — `generate-pipeline-docs.py --write|--check`
  (backend venv; imports `app`) writes the Deployment variants page's three partials under
  `docs-site/docs/installation/_generated/` from `docker-build-push.sh list-platforms`,
  `security-scan.sh list-repos`, the ASR catalog and `resolve_pipeline_plan`; CI's backend job
  runs `--check`. `check-architecture-diagram-drift.py` (stdlib, `--selftest`) compares each
  `/architecture` render's `spec-sha256` stamp, written by `generate-architecture-diagrams.sh`,
  with its spec; it is a pre-commit hook. Both are pinned by unit tests; see
  `docs-site/CLAUDE.md`.
- **Document export timing** (issue #1278) — `bench-transcript-document.py` renders N hours of
  synthetic speech to PDF/DOCX and prints wall time, peak RSS, pages and size. MANUAL ONLY, never a
  test (a wall-clock budget is a flaky gate); run it on a quiet host, ideally inside the backend
  image where the real fonts live (`--font-dir` otherwise).
- **Backend pytest from any worktree, no dev stack** — `run-backend-tests.sh --throwaway-db [--keep] <pytest args>` (engine: `lib/throwaway-pg.sh`, the only one): throwaway branch-migrated postgres on a free loopback port, removed on any exit; a venv-less worktree borrows the main checkout's. Details: `backend/tests/notes/db-tests-without-stack.md`. `OPENTR_NO_ENV_LINK=1` — opentr.sh normally symlinks the main checkout's `.env` into a worktree it runs in; this skips that. `backend/tests/conftest.py` and `tests/unit/_shell_extract.py` export it so the shell tests (which run opentr.sh at the checkout root) leave no `.env` behind; `test_opentr_env_worktree_autolink.py` strips it to test the link itself. `start|reset ... --dry-run` (incl. `--fresh`) writes nothing: it prints the `.fresh/` files it would write.
- **Quick dev-cycle check** — `run-dev-tests.sh --full` (or `--fast`/`--backend-only`/`--e2e-only`/`--frontend-only`): chains the backend gate, e2e and the frontend check, auto-starting the overlays each phase needs. Full flag table, overlay reconciliation and the `run-backend-tests.sh --summary` runs-no-tests warning: `scripts/notes/dev-test-gate.md`.
- **Pre-merge gate** — `run-integration-tests.sh` (`--coverage --e2e-smoke --search-quality --cleanup`).
  Runs the Unit/API suite, then the security suites **once** under `FIPS_MODE=true`, then
  `-m integration`, then `-m gpu`, then **model-vs-schema drift**
  (`RUN_SCHEMA_DRIFT_TESTS=true tests/unit/test_schema_drift.py`), then the dependency-parity,
  session-lifetime, collection-determinism and mutation-ratchet phases. The drift phase's
  variable used to be set in exactly ONE place — the release pipeline's `schema-drift`
  criterion, at severity `warn` — so the check never ran pre-merge at all, and its warn
  justification ("4 known pre-existing offenders") was stale.
  ⚠️ **There is no `GATES` array and no `GATED_FILES` run any more, and re-adding either is a
  regression.** This bullet used to explain that the drift file was deliberately kept out of
  `GATES`; that reasoning is void because the array is gone. It exported seven `RUN_*`
  variables that **no test read** — the module-level `skipif` gates had been deleted from all
  eight security suites and the array was left behind, so a phase named "Gated security
  suites" set variables that changed nothing. The **FIPS-off** half went with it: with no
  gates those files are ordinary members of the Unit/API suite, and that phase re-ran, byte
  for byte, tests phase 1 had just run. Measured on the 2026-09-07 gate's own junit artifacts:
  all **394** ids in `gated-fips-off.xml` also appear in `unit.xml`, **0 missing**. The
  `FIPS_MODE=true` pass stays — `app/core/config.py` reads that at import, so it is a real
  claim. `backend/tests/unit/test_gate_run_env_vars_are_live.py` now fails on any `RUN_*` a
  gate script sets that no test reads through a **live** expression; its predecessor
  `test_gated_files_all_have_gates.py` is **deleted**, because it substring-matched the
  variable name over the whole file and every one of those files still mentions its dead
  variable in a comment — a guard that could not fail, in the file written to prevent tests
  that cannot fail.
- **GPU diarization suites** — `run-diarization-gpu-tests.sh` (issue #577). The gate's `-m gpu`
  phase runs in `backend/venv`, so the three container-only diarization suites SKIP there; this
  is the only thing that executes them. It builds `opentranscribe-backend-test:latest` from
  `backend/Dockerfile.test` (prod image + `requirements-test.txt`) and runs the
  `diarization-tests` service in its own compose project, so it can neither re-tag nor recreate
  anything the live dev stack is using. **Never `pip install` pytest into the prod image at run
  time with `--user root`** — user-site is `$HOME`-derived, so root gets `/root/.local` and the
  entire app dependency tree (fastapi, meeteval, torch) falls off `sys.path`; that is the whole
  of #577's "fastapi missing" and "meeteval wheel won't build" report, one cause with two faces.
  It hard-fails when the base image, the gitignored `benchmark/test_audio/*.wav` fixtures, or the
  `-o addopts= -m gpu` selector are missing, because each of those makes the run exit 0 having
  measured nothing.
- **E2E** — `e2e/run-e2e.sh` (two phases: non-visual in parallel, then visual serially) and `e2e/run-e2e-smoke.sh`. Phase behaviour, exit codes and the zero-collected rules: `scripts/e2e/CLAUDE.md`.
- **Test-suite quality tooling** (issue #431) — `audit-tests.py` (21 AST detectors, allowlist-gated, `--selftest`), `analyze-test-timing.py`, `run-mutation-tests.sh` and the other auditors. Every detector, allowlist rule, selftest and the mutation-harness traps: `scripts/audit_tests/CLAUDE.md`.
- **Summary-merge quality gate** — `run-summary-eval.sh --fresh NAME --arm old=@module --arm new=<file>`
  (design: `docs/design/summary_quality_eval_plan.md`; package `summary_bench/`, entry
  `benchmark_summary.py`). Measures the multi-slice scope/merge rules on a real local model:
  GPU check (GPU 2 idle or exit 4; GPU 0 refused), isolated `--fresh` stack, a smoke that projects
  the matrix against the budget, then run, replay-merge, judge and report. `--dry-run` prints every
  command and runs none. The arms are JSON files OUTSIDE the repo; a results file holds only their
  SHA-256 and length. A verdict of `VOID` (missing cell, arm never applied, sabotage control not
  caught, A/A control tripped) means the numbers must not be read. `run-dev-tests.sh
  --with-summary-eval` runs only its smoke wiring check.
- **Fake providers** — `mock-llm-server.py`: OpenAI-compatible server so chat/AI features work
  without a GPU or API key. Run it via `./opentr.sh start dev --with-mock-llm` (compose
  service `mock-llm`, in-network `http://mock-llm:5199/v1`) rather than by hand — a bare
  host process binds 5199 and then blocks the container. Scenario models (`mock-echo`,
  `mock-empty`, `mock-error`, `mock-slow`) drive the app's real error paths; fixtures and
  the full table are in `backend/tests/CLAUDE.md`.
  `mock-asr-server.py`: a stdlib Gladia API v2 stand-in so cloud-ASR features (and the whole
  `--lite` deployment shape) work without a vendor account. Run via
  `./opentr.sh start dev --with-mock-asr` (compose service `mock-asr`, in-network
  `http://mock-asr:5198`) — never by hand, same bind-port hazard as the LLM mock. Scenario
  models (`ok`/`error`/`malformed`/`upload-reject`) select via `?scenario=` on the request or
  the `MOCK_ASR_SCENARIO` env var at server start; fixtures and the full table are in
  `backend/tests/CLAUDE.md`.
- **Frontend gate** — `frontend-check.sh`: `npm ci` → `svelte-kit sync` → ESLint → i18n parity → svelte-check →
  vitest → frontend test-auditor self-test + audit → vite build. Also the pre-commit hook (`files: ^frontend/src/`) and the `/fix-frontend` command.
  **Two hooks, not one, since issue #688**: `frontend-check` runs it with `--check-only` at
  commit stage (the `vite build` — whose `prebuild` downloads fonts and shells out to
  `docker buildx` for the FFmpeg.wasm core — measured 91.8 s of a 328 s whole-tree run), and
  `frontend-build` runs it in full at **pre-push**. On a PR that hook is the only `vite build`
  in CI at all, so `.github/workflows/pre-commit.yml` invokes both stages; see
  `backend/tests/unit/test_precommit_stage_ci_parity.py`.
- **Requirements resolve check** — `check-requirements-resolve.py [--only FILE]` (a variant named in `--only` also pulls in requirements.txt, the drift reference): `pip install --dry-run` of requirements.txt, -nodeps (`--no-deps`), -dev, -lite and -ci, failing on ResolutionImpossible and on any package lite/CI/dev resolve at a different version than prod (CI job `Requirements resolve`; run with the images' Python 3.13; ~5 min, needs network; offline logic in `test_check_requirements_resolve.py`).
- **Prometheus rules check** — `check-prometheus-rules.sh` (issue #791). Runs `promtool check config`/`check rules`/`test rules` and `amtool check-config` in the digest-pinned images read from `docker-compose.monitoring.yml` (the single home of the pins), network off. Needs Docker; CI runs it in the always-run pre-commit job. Rules: `monitoring/prometheus/rules/`, their unit tests: `monitoring/prometheus/tests/`.
- **mypy hook negative control** — `mypy-hook-negative-control.sh [--ref REF]` (issue #1271). Builds a
  throwaway repo from `git archive REF` under an `ot-redcheck.` temp dir, drops
  `mypy_negative_control_fixture.py.txt` into it as `backend/app/zz_mypy_negative_control.py`, runs the
  REAL mypy hook on that one file, and requires every `# EXPECT: <code>` line to come back as
  `error ... [<code>]`. Exit 0 = `caught 3/3`; 1 = a control was NOT caught (the hook cannot see pydantic's
  or requests' own types); 2 = misuse. It never touches the checkout it runs from, so it is safe beside
  other writers. Red proof: `--ref <commit before the hook was aligned>` reports 0/3 and exits 1. CI runs it
  (`.github/workflows/pre-commit.yml`). The fixture has a `.txt` suffix so no hook or test collector sees it.
  The static half is `backend/tests/unit/test_precommit_mypy_env_parity.py`.
- **Publish images** — `docker-build-push.sh`; prefer the skill at `.claude/skills/docker-build-push/SKILL.md`.
- **Downloader layout** — `download-models.py` keeps only `main()`; the groups live in `scripts/model_download/` (`ui`, `torch_compat`, `whisper`, `pyannote`, `nltk`, `embeddings`, `opensearch`, `redaction`, `diar_native`, `manifest`, `groups`). Every container that runs it must mount the package beside the script (`-v scripts/model_download:/app/model_download`), and every file must be in `release-manifest.txt` (`test_release_manifest.py` globs it). `cleanup-test-data.py` is likewise an entry over `scripts/cleanup_test_data/`; `.gitignore` has a negation for it because `*_data/` would otherwise hide the package from git and from the ruff guards.
- **Models** — `download-models.sh <cache-dir>` is a host wrapper that runs `download-models.py` inside
  the backend image (`DOWNLOAD_ALL_OPENSEARCH_MODELS`, `OPENSEARCH_MODELS`, `WHISPER_MODEL`).
  `fix-model-permissions.sh` chowns the cache to **`$CONTAINER_UID_GID`** (`scripts/common.sh`,
  default **1000:999**) — `appuser` is `useradd -u 1000` but `groupadd -r`, so its GID is 999,
  not 1000 (issue #580). Never hardcode `1000:1000` in a new chown. `1000:999` has no static
  source of truth — `Dockerfile.prod` uses `groupadd -r` with no GID pin, so the GID is
  whatever the base image leaves free. `backend/tests/integration/test_chown_scripts_real_tree.py`
  derives it from the built image; a base-image bump that moves it fails there.
  The `whisperx` group of `download-models.py` (kept as the `--only` key) is a thin caller of
  `app.services.local_models.downloader.download_blocking` (#1246), the same downloader the
  Settings page uses. `WHISPER_MODEL` must name a model in
  `backend/app/core/local_whisper_catalog.py` (aliases such as `crisperwhisper` are applied there,
  not in the shell wrapper); anything else, e.g. `distil-large-v3` or an arbitrary `org/repo`, is
  refused with `unknown_local_model`. A downloader image that predates the catalog fails the group
  with an instruction to use the image matching the script's release (`OT_IMAGE_TAG`); there is no
  whisperx fallback. The `pyannote` group runs **no inference**: it `snapshot_download`s every
  repo of `pyannote_repo_plan()` (derived from `diarizer.PYANNOTE_V4_MODEL`/`PYANNOTE_V3_FALLBACK` and
  `EmbeddingModeService.get_embedding_model_name`; v3/3.1 are optional, reported when gated) plus
  the repos a pipeline `config.yaml` names, then loads community-1 with `Pipeline.from_pretrained`,
  the backend's own call. `tests/unit/test_download_models_pyannote_plan.py` (CI-runnable) fails on
  a new in-process `from_pretrained` site. It used to transcribe synthetic sine waves through whisperx first; whisperx's VAD
  found no speech and transformers 5.x raised `IndexError` on the empty batch, failing every CPU
  run of v0.6.0. Nothing in `backend/app` imports whisperx (`Transcriber` is faster-whisper's
  `BatchedInferencePipeline`, which returns no segments for silence; pinned by
  `tests/integration/test_transcriber_no_speech_real_model.py`). The `nltk` group gives each
  multiply-linked file in `nltk_data` its own inode before downloading (nltk >= 3.10 pathsec
  refuses `st_nlink > 1`) and fails once, with the host fix, when it cannot.
- **Fixtures** — `seed-fresh-deployment.sh`, `setup-watch-source-test-data.sh`, `test-watch-e2e.sh`.
  ⚠️ `seed-fresh-deployment.sh` is **idempotent per file** (#1238): it lists the deployment and
  uploads only absent files, matching on the stored `title` + extension (the stored `filename` is
  sanitized and differs from the on-disk name for ~1 in 4 corpus files), and uploads nothing if the
  listing fails. `--manifest` prints `# source: …` then `<name><TAB><bytes>` with no network call;
  `e2e/update-visual-baselines.sh` derives its expected corpus from it and refuses to capture unless
  the library equals it exactly (names, multiplicity, sizes, all `completed`, nothing extra), because
  the gallery's card count cannot be masked. `opentr.sh fresh-destroy <name> --yes` exists for
  non-tty callers; without it EOF at the prompt now fails (it used to "cancel" with exit 0). The
  wrapper's `--destroy`/`--rebuild` verify the project's volumes are gone rather than trust that exit
  status.
- **Release rehearsals** — `release-tests/`: `test-fresh-install.sh`, `test-upgrade.sh`, `test-lite-mode.sh`, `test-cold-model-download.sh`. Scenarios, phases, guardrails, the `set -e` trap and the harness self-tests: `scripts/release-tests/CLAUDE.md`.
- **Cutting a release** — `release.sh` is the ONLY entry point; never hand-run `git tag` / `docker push` / `gh release`. Stage contracts, the ledger, criteria wiring and overrides: `scripts/release/CLAUDE.md`.
- **Install-path gate** — `verify-install-paths.sh` (issue #683): the only check that compares the scripts at the tip against the published releases. Details: `scripts/release/CLAUDE.md`.
- **Image hygiene** — `check-image-hygiene.sh <image>...` (exit 0 clean / 1 found / 2 could not
  check). Reads the image via `docker create --pull never` + `docker export | tar -t` (never runs
  it, removes only the container it created, never pulls or removes images) and matches the
  `--list-patterns` denylist at any depth under `/app` or `/usr/share/nginx/html`. The release
  `scan` stage runs it per leg tag (`images-carry-no-dev-artifacts`). Its denylist is also what
  `backend/.dockerignore` must exclude with `**/` rules: `unit/test_image_hygiene.py` checks every
  depth and fails on a redundant rule, and `integration/test_image_hygiene_real_docker.py`
  cross-checks the ignore file against a real BuildKit context.
- **Live docs check** — `check-docs-live.py` (stdlib only; exit 0 ok / 1 a check failed / 3 site unreachable).
  Proves https://docs.opentranscribe.app/ matches the checkout: version badge vs `VERSION`, newest blog post
  (by filename date) and newest `operations/upgrading-to-X-Y-Z` page reachable, key pages from
  `sidebars.ts` (comments stripped, file must exist), sitemap, and freshness (Last-Modified, or the last
  successful deploy run with `--use-gh`) against the last commit touching `docs-site/` or `VERSION`.
  Inside `--grace-minutes` (90) a stale site is `PENDING`, not a failure. `--wait-seconds` retries for Pages
  cache lag. Run by `.github/workflows/docs-live-check.yml`; tests drive it against a local `http.server`
  (`unit/test_check_docs_live.py`). It is a release closing step (`.claude/skills/release/SKILL.md`).
- **Release gates** — `check-schema-drift.py` (model-vs-schema, report-first),
  `validate-deployments.sh` (~20 compose permutations in ~15 s),
  `release/check-version-consistency.py` (the six version sources + Alembic single head).
- **PKI** — `pki/`: `setup-test-pki.sh`, `generate-test-env.sh`, `run-pki-e2e-leg.sh`; the gitignored `test-certs/` holds private keys. Details: `scripts/pki/CLAUDE.md`.
- **`common.sh`** is sourced by both front ends (`opentr.sh`, `opentranscribe.sh`) and owns `backup_database` / `restore_database`; every `$VAR` in `opentr.sh` + `common.sh` must be defaulted. It also owns `compose_run_migrate`: any path that starts the API without `up`'s `depends_on` (`compose start`, `up --no-deps`) must call it first, because the prod API does not migrate itself (`docs/design/migrate_job_plan.md`). `verify_stack_health` treats an exited-0 container with restart policy `no` (the `migrate` one-shot) as done, not broken. Details, restore primitives (#599/#600) and the guard's false-positive cases: `scripts/notes/common-sh.md`.
## Fresh deployments (`opentr.sh --fresh`, state in `.fresh/`)

The mechanics (state files, port offset, aux overlay isolation under `--fresh`, issue #347) are in `scripts/notes/fresh-deployments.md`. Root `CLAUDE.md` carries the rule.
## Scale and query performance testing

Seed a large synthetic dataset into a local isolated `--fresh` stack, drive the real endpoints,
replay the hot statements under `EXPLAIN`, and keep the fix honest with a regression check. Never
the dev stack: the seed tool and the driver refuse its ports. Method and traps:
`docs-site/docs/developer-guide/performance-testing.md`.

```bash
./opentr.sh start dev --fresh perf1337 --port-offset 300            # isolated stack (backend :5474, Postgres :5476)
./scripts/seed-query-review.sh --fresh perf1337                      # default shape: 100k files, 1M speakers, 10M segments (~30 min)
./scripts/seed-query-review.sh --fresh perf1337 --files 1000 --users 50 --segments-per-file 10   # quick run
docker exec "$PG" pg_dump -U postgres -Fc -d opentranscribe > ~/ot-cleanup-work/1337/perf/seed-perf1337.dump   # snapshot
docker exec -i "$PG" pg_restore -U postgres -d opentranscribe --clean --if-exists < ~/ot-cleanup-work/1337/perf/seed-perf1337.dump   # restore, then ANALYZE
scripts/perf/query-review/drive.py run --check-routes --base-url http://localhost:5474
scripts/perf/query-review/drive.py run --base-url http://localhost:5474 --out <run dir> --repeat 20
scripts/perf/query-review/drive.py summarise --calls <run dir>/calls.csv --access-log <run dir>/access.log > <run dir>/driver.csv
PG=<fresh postgres container> scripts/perf/query-review/explain.sh <run-id> [id ...]
```

- `seed-query-review.sh` wraps `python -m app.scripts.scale_dataset` (`backend/app/scripts/scale_dataset/`);
  identities `perf-1@loadtest.invalid` (admin), `perf-2@...` (heavy), `perf-3@...`, password `loadtest-password`.
- `scripts/perf/query-review/queries/<id>.sql` are the replayable statements (`<id>.after.sql` is the
  rewrite, `C1`-`C3` are controls that must not move more than 15% between runs, `queries/setup/<id>.sql`
  runs first inside the same rolled-back transaction). `backend/tests/unit/test_query_review_sql_files.py`
  plans every one against the current schema.
- Enable `pg_stat_statements`/`auto_explain` with an UNQUOTED
  `ALTER SYSTEM SET shared_preload_libraries = pg_stat_statements, auto_explain;` (quoted, Postgres will not start).
- Concurrent users: `scripts/perf/load/run.sh --fresh NAME --profile smoke|steady|ramp|spike|soak|pipeline
  [--mix mixed|read-only|mixed-gpu]` (Locust from its own venv, `LOCUST=...`; sampler + summary with a
  pass/fail verdict from `thresholds.toml`), same seed plus `--waveform-pm 1000`, same `--fresh`-only guard,
  stack started `--with-mock-llm`; runbook in `scripts/perf/load/README.md`; side tools in `scripts/perf/load/tools/` (chain/run_profile/pg_probe/report_table/build_snapshot). `backend/tests/unit/test_load_*.py`
  check every request against `backend/openapi.json`, API-group coverage, cleanup, profiles and seed needs.
- Regression checks use `backend/tests/query_budget.py` (`count_statements`): assert the statement count is
  EQUAL at two row counts (for example 2 and 12) with a control that the response held both, never a ceiling.
  Prove each check red on a `git archive HEAD` tree first.

## docker-build-push.sh

The tag grammar (capability in the repository, restated in the tag, issue #680), the platform table and the build/push traps: `scripts/notes/docker-build-push.md`. Ask `./scripts/docker-build-push.sh list-platforms` for the platform set; never transcribe it.
### `80-publish.sh`

`80-publish.sh` pushes the BUILT legs and never builds; its manifest gate has two document shapes and three exit codes. Both: `scripts/release/CLAUDE.md`.
## DESTRUCTIVE — never run casually

- `reset-retries.sh` — bare `UPDATE media_file SET retry_count = 0` on the live DB: **no WHERE clause,
  no confirmation, no dry-run.**
- `fix-database-issues.sql` / `fix-false-error-files.sql` — mass status UPDATEs; both are stale one-offs
  superseded by `backend/app/tasks/recovery.py`. (`comprehensive-database-review.sql` is read-only.)
- `cleanup-test-users.py` — `DELETE FROM "user"`; dry-run unless `--execute`. Targets
  `POSTGRES_HOST`/`POSTGRES_PORT` (env var, or `.env` fallback for the port only — `POSTGRES_HOST`
  is env-var-only, see the script's `_host_setting` docstring for why), never the orphaned
  `POSTGRES_TEST_PORT` name that appeared nowhere else in the repo before issue #601 and always
  silently resolved to the live dev stack's port (5176) regardless of what an operator intended
  to target. Prints `Target: <user>@<host>:<port>/<db>` as the first line of every run — check it
  before trusting `--execute`. A candidate blocked by a leftover child row (`tag`/`task`/
  `comment`/`collection`/`speaker`/`speaker_profile`/`speaker_collection`.`user_id`, all
  `ON DELETE NO ACTION`) is reported and skipped, not fatal to the batch — exit code 1 if
  anything was blocked. `ORPHAN_PATTERNS` is now split into `ORPHAN_PATTERNS_UNAMBIGUOUS`
  (10 patterns — `-e2e-`-infix or RFC 2606 `.invalid`, no human types these) and
  `ORPHAN_PATTERNS_REVIEW` (8 patterns — `testuser_%`, `test-%@example.com`, plausible
  hand-created accounts); `ORPHAN_PATTERNS` stays the union so existing callers/tests are
  unchanged. `--execute-unambiguous` deletes only the unambiguous tier; `--execute` deletes
  both, same as before.
- `cleanup-test-data.py` (issue #629) — the sibling sweep for everything `cleanup-test-users.py`
  never touched: orphaned test-uploaded media, collections, tags, watch sources, speaker
  profiles, and chat conversations. It is the ONE entry point — it shells out to
  `cleanup-test-users.py` (same subprocess idiom `run-integration-tests.sh` uses) for the
  user/LLM-config plane rather than duplicating that logic, and runs it LAST, after every
  other sweep, because clearing a corpus-owning test user's files/collections/tags first is
  what lets that user finally become deletable (`classify()`'s "owns files -> never a
  candidate" rule otherwise makes an owner permanently unsweepable).
  - **Media cannot be swept with SQL.** Child rows reference `media_file.id` with
    `ON DELETE NO ACTION`, and MinIO objects + OpenSearch documents are unreachable from
    Postgres at all — the only correct destroyer is `purge_media_file()`
    (`backend/app/services/file_cleanup_service.py`), reached via `DELETE /api/files/{uuid}`
    (falling back to `/force`). So every Tier A resource type here is deleted over the HTTP
    API, never direct SQL, even where a table has no external-store problem of its own —
    consistency, and so each resource's own cascade/embedding cleanup always runs.
  - **Tier A (unambiguous) vs Tier B (review), same risk-asymmetry reasoning as
    `cleanup-test-users.py`'s split**: a wrongly-deleted media file is annoying; a
    wrongly-deleted collection/tag/watch-source/speaker-profile/conversation is the same
    category of annoying. `--execute-unambiguous` selects Tier A only (matched by a
    registered filename/name/title PREFIX plus an 8-hex-char uniquifier — never a bare
    prefix, so `e2e-tag-notes` a human typed by hand survives); `--execute` adds Tier B
    (currently none defined for this script — every registered signature here is
    unambiguous by construction).
  - **Liveness cutoff, not a name-embedded seed** (`scripts/testrun-registry.sh`): every
    candidate table already carries a creation timestamp, so a row created by a
    CONCURRENTLY LIVE run is protected by comparing its timestamp against
    `min(started_at)` across every currently-held `.testruns/*.lock` flock marker — a
    crashed run's leftovers wait for other live runs to finish before being swept, never
    destroyed while something might still be using them.
  - Admin credentials from `E2E_ADMIN_EMAIL`/`E2E_ADMIN_PASSWORD`, defaulting to the
    documented dev pair. Logs itself out when done (`POST /api/auth/logout`) so the tool
    does not itself contribute to the `refresh_token` leak tracked in issue #633.
  - Degrades rather than blocks: an unreachable backend skips the whole API plane with a
    loud warning (DB-only planes — the `cleanup-test-users.py` subprocess — still run) and
    the process exits non-zero, so `run-integration-tests.sh`/`e2e/run-e2e.sh`'s
    `|| sweep_rc=$?` guard is what keeps a sweep failure from blocking the actual test run.
  - **Store planes (issue #1195)** — `scripts/lib/test_artifact_sweep.py`: throwaway
    OpenSearch indexes + ingest pipelines (Tier A when EMPTY, Tier B with documents), test
    buckets and test-keyed objects in the media/cache buckets, scratch Postgres databases
    (`ot_<word>_test`, Tier B, never with an open connection), `/tmp` trees of this user, and
    labelled throwaway docker containers/networks (Tier B only — `throwaway_pg.py` records why
    a container is never removed automatically). A name must FULLY match a registered shape
    ending in the fixture's uniquifier; the protected list (production indexes, `.`-prefixed
    system indexes, configured buckets, the target and system databases) is checked first and
    again at the point of deletion. Orphan `profile_<uuid>` speaker docs are reported, never
    deleted (Tier C). `--planes records,indexes,...` selects; the integration test of the row
    sweep runs `--planes records` so it never touches the host's real stores.
  - **Injected corpora and benchmark uploads (#1222)**: the records plane also finds media whose
    `metadata_important.rag_eval.seed` fully matches `INJECTED_TEST_SEED_SHAPE` (the seeds the
    TEST suites mint; never the canonical empty seed or an operator seed) and
    `bench-parallel-<hex8>.<ext>` (standalone `benchmark_parallel.py --upload`, which also deletes
    its own uploads unless `--keep-uploads`). Both are deleted over the API, so they end in
    `purge_media_file`, the same destroyer as `python -m app.scripts.corpus_injection --purge`
    (`inject-eval-corpus.sh ... --purge --dry-run|--yes`). An operator's corpus is removed with
    that command, never by the sweep.
  - **Rows owned by another test user (#1237)**: every record delete runs as the admin, who is
    403 on someone else's speaker profile, so a leaked `delres-e2e-<hex8>@example.com` account's
    rows could never go and kept the account `BLOCKED`. A candidate whose owner FULLY matches a
    registered test-user shape (`cleanup-test-users.py`'s `ORPHAN_PATTERNS_UNAMBIGUOUS`; the
    review tier only under `--execute`), predates the liveness cutoff and is not in its keep-list
    is now removed by deleting the owner via `DELETE /api/admin/users/{uuid}` (cascade + storage
    purge) before the per-record deletes. Every failed delete prints `HTTP <status> <reason>:
    <detail>`. `_delete_owners` sits behind the same `effective_execute` guard as
    `_delete_candidates` (AST-scanned).
  - **Stack scoping**: the store planes exit 2 when the EXPORTED `POSTGRES_PORT`/
    `OPENSEARCH_PORT`/`MINIO_PORT` resolve to different stacks (base + one `--port-offset`),
    which is the "exported one port, forgot another" accident. `.env`/default values are one
    stack by construction and never trip it.
  - **`--min-age-minutes N`** lowers the cutoff further: nothing younger is ever touched.
- `cleanup-test-leftovers.sh` (`./opentr.sh test-cleanup`, issue #1195) — the unattended
  wrapper. `--project <compose-project>` or `--fresh <name>` is REQUIRED (it lists candidates
  and refuses otherwise); every port, credential and bucket name is read from that project's
  own containers, selected by `com.docker.compose.project`. Runs ONLY the Tier A sweep with an
  age floor (`--min-age-minutes`, default 30). Modes: `--once` (default), `--background
  [--interval-minutes N]`, `--status`, `--stop`, plus `--dry-run`. State in
  `.test-cleanup/<project>.{log,pid,lock}` (`OT_TEST_CLEANUP_DIR`): one summary paragraph per
  pass in the log, a lock so passes never overlap, a pid file so a project has one loop.
  `--stop` checks `/proc/<pid>/cmdline` names this script's loop for this project and sends a
  plain TERM to that pid only — never pkill/kill -9; a pass in progress finishes first.

  ```bash
  ./opentr.sh test-cleanup --fresh v060 --dry-run          # what a pass would remove
  ./opentr.sh test-cleanup --fresh v060                    # one Tier A pass now
  ./opentr.sh test-cleanup --fresh v060 --background --interval-minutes 60
  ./opentr.sh test-cleanup --fresh v060 --status           # running? last pass summary
  ./opentr.sh test-cleanup --fresh v060 --stop
  ```
- **End-of-run sweep** — `lib/test-data-sweep.sh`: `run-integration-tests.sh` and
  `e2e/run-e2e.sh` call `tds_sweep_at_exit`, which runs the Tier A sweep from an EXIT trap
  (so after a failed phase, a `set -e` abort, Ctrl-C or TERM), ends the run's own liveness
  marker first (`testrun_end`) so its own leftovers are not protected by its own start time,
  and never changes the exit code. `run-dev-tests.sh` sweeps ONCE after all phases
  (`tds_own_end_sweep_for_children` exports `OT_TEST_DATA_SWEEP_AT_EXIT=parent`, which the
  wrapped scripts honour) and owns the single EXIT trap that also stops a `--with-llm-test`
  vLLM it started and the overlays. ⚠️ A second top-level `trap ... EXIT` in any of these
  scripts silently replaces the sweep — `unit/test_test_data_sweep_at_exit.py` fails on one.
- `testrun-registry.sh` — not itself destructive (it only ever creates a lock file under
  `.testruns/`), but is the liveness mechanism `cleanup-test-data.py`'s safety argument
  depends on. `testrun_begin` opens a marker file, takes `flock -n` on it, and holds the fd
  for the rest of the CALLING PROCESS's life — released automatically (by the kernel) on
  exit, including SIGKILL/OOM/reboot, so a crashed run cannot be mistaken for a still-live
  one and there is no PID-reuse hazard. `TESTRUN_REGISTRY_DIR` overrides the `.testruns/`
  location — used only by the isolated-DB test proving this mechanism, never in normal use.
  The directory is the MAIN checkout's `.testruns/` (resolved through `git rev-parse
  --git-common-dir`), shared by every worktree; `backend/tests/testrun_marker.py` (every
  pytest controller) and `cleanup-test-data.py` resolve the same path
  (`unit/test_testrun_registry_location.py`). `testrun_end` releases and removes a run's own
  marker; stale markers are pruned by the sweep's tmp plane (issue #1195).
- `pki/run-pki-e2e-leg.sh` (matrix leg `3-pki`, issue #1222) — starts a PROD+nginx+PKI stack under the
  stock names and stops it from one EXIT trap armed BEFORE `opentr.sh start` (INT/TERM exit 130/143 so
  the trap runs; `--keep-stack` skips the stop). `unit/test_pki_leg_teardown.py` proves the trap logic
  with stubs; a real PKI run is the only proof that `./opentr.sh stop` removes the PKI overlay's
  containers, and none has been done for this change.
- `uninstall-offline-package.sh` — `docker compose down -v`, `docker rmi`, `rm -rf /opt/opentranscribe`.
- `release-tests/*` — `docker volume rm` on `opentranscribe_*` plus `rm -rf $TEST_ROOT`. They
  **require the live stack to be stopped**: they bind the standard 5173–5180 ports under the stock
  `opentranscribe-*` names and the one-liner's `opentranscribe` compose project, **by design**, so the
  run exercises exactly what a real user's install produces. `TEST_PROJECT_NAME` (`ot-reltest-*`) is a
  cleanup label namespace only, never `COMPOSE_PROJECT_NAME`. Isolation is from live *data*, not live
  *names*: `lib/guardrails.sh` refuses to start if any `opentranscribe-*` container exists (running or
  stopped) or any port is bound, rejects a `TEST_ROOT`/bind-mount under a protected path, never deletes
  the production volumes, and gates on an `I UNDERSTAND` prompt. For a stack that runs *beside* the
  live one, use `./opentr.sh start dev --fresh <name> --port-offset N` instead. (The README claimed
  hard isolation with 6173+/6273+ ports until #303 — it never worked that way.)
- `run_benchmark.py` — `docker compose down -v` on `-p otbench`, plus a legacy cleanup pass aimed at the
  **`transcribe-app` dev project** (suppress with `--no-cleanup-legacy`).
- `frontend-check.sh` on failure shells out to the `claude` CLI and then `git add`s every modified file
  under `frontend/` — pass `--no-claude` if you don't want your index touched.
- Everything else writes to the **live dev stack**: benchmarks upload files and never delete them;
  `test-all-auth.sh` / `test-fedramp.sh` register real users and never clean up (that is why
  `cleanup-test-users.py` exists; `test-fedramp.sh`'s `fedramp-e2e-<ts>` account is a registered
  Tier A shape since #1195); `run-auth-e2e.sh` mutates admin auth config (restored on an exit
  trap) and stops the frontend container.

## Gotchas

- Stack must be up (`./opentr.sh start dev`) for `run-integration-tests.sh` (probes Postgres on 5176),
  `e2e/run-e2e.sh` (5173+5174), `test-watch-e2e.sh`, `speaker-profiles-backup.sh`, and the recovery
  `test-*.sh`. Others — `reset-retries.sh`, the benchmarks, the `.sql` files — have **no precondition
  check** and just fail with cryptic docker errors.
- `run-integration-tests.sh --cleanup` runs `cleanup-test-users.py` in **dry-run only**; it reports,
  it does not delete.
- `__pycache__/`, `pki/test-certs/` (contains private keys), and `release-tests/.env.test-secrets` are
  gitignored but present on disk.
- Most `benchmark_*` / `vram-probe-*` / `spike-*` files are accreted one-off experiments;
  `run_benchmark.py` (via `./opentr.sh bench`) is the maintained orchestrator.

- `release-tidy.sh` (issue #1370) — dry-run by default, `--yes` acts; every deletion is delegated
  (`git branch -d` for merged branches, `./opentr.sh fresh-list --names` + `fresh-destroy` for `rel-*`
  stacks only, `test-cleanup --project P`, and `cleanup-test-data.py --planes tmp` for registered
  `/tmp` shapes incl. `ot-redcheck.*`). A failing `fresh-list --names` aborts with exit 1 rather than
  listing nothing.
