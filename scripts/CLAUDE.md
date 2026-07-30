# scripts — test gates, build tooling, and one-off operations

## Purpose

72 mixed shell/python scripts. **`scripts/README.md` (900 lines) already documents the
build / offline-package / model-download / remote-builder family in depth — read it for those
and don't duplicate it here.** It does not mention the other ~60 files (test gates, e2e,
benchmarks, one-off SQL) and contains **zero destructive-operation warnings**. That is what
this file is for.

## Which script to reach for

- **Pre-merge gate** — `run-integration-tests.sh` (`--coverage --e2e-smoke --search-quality --cleanup`).
  Runs the ungated suite, then all `RUN_*`-gated security suites twice (FIPS off, then `FIPS_MODE=true`),
  then `-m integration`.
- **E2E** — `e2e/run-e2e.sh`: two phases, non-visual in parallel (`E2E_WORKERS`, default 3) then
  `-m visual` serially. `e2e/run-e2e-smoke.sh` is a 4-file subset of it.
- **Frontend gate** — `frontend-check.sh`: `npm ci` → `svelte-kit sync` → ESLint → svelte-check → vite
  build. Also the pre-commit hook (`files: ^frontend/src/`) and the `/fix-frontend` command.
- **Publish images** — `docker-build-push.sh`; prefer the skill at `.claude/skills/docker-build-push/SKILL.md`.
- **Models** — `download-models.sh <cache-dir>` is a host wrapper that runs `download-models.py` inside
  the backend image (`DOWNLOAD_ALL_OPENSEARCH_MODELS`, `OPENSEARCH_MODELS`, `WHISPER_MODEL`).
  `fix-model-permissions.sh` chowns the cache to **1000:1000** (the container's non-root `appuser`).
- **Fixtures** — `seed-fresh-deployment.sh`, `setup-watch-source-test-data.sh`, `test-watch-e2e.sh`.
- **Release rehearsals** — `release-tests/`: `test-fresh-install.sh`, `test-upgrade-from-v033.sh`,
  with `lib/guardrails.sh` as the safety firewall and `lib/{compose-patch,api-client,assertions}.sh`.
- **PKI** — `pki/`: `setup-test-pki.sh` (generates the gitignored `test-certs/`), `start-pki-prod.sh`,
  `test-pki-auth.sh`.
- `common.sh` is sourced **only by `opentr.sh`** (docker checks, model-cache chown, OpenSearch model
  bootstrap). `offline-common.sh` is sourced only by the two offline/Windows builders.

## docker-build-push.sh

- **ALWAYS `USE_REMOTE_BUILDER=true`.** It defaults to `false` and `PLATFORMS` defaults to
  `linux/amd64,linux/arm64`, so without it ARM64 builds under QEMU — 2–3 h instead of 15–30 min.
  It hard-exits if the `opentranscribe-multiarch` builder is missing (`setup-remote-builder.sh setup`).
- `SKIP_SECURITY_SCAN=true` for quick iteration; `PLATFORMS=linux/amd64` for single-arch (no remote
  builder needed); `$0 auto` builds only git-changed components.
- **Every path ends in `buildx --push` — there is no local-only mode.** `:latest` and `:vX.Y.Z` hit
  Docker Hub the instant the build finishes, and it then runs `push-security-reports.sh`, which
  **git-commits and pushes** `security-reports/` to whatever branch is checked out.

## DESTRUCTIVE — never run casually

- `reset-retries.sh` — bare `UPDATE media_file SET retry_count = 0` on the live DB: **no WHERE clause,
  no confirmation, no dry-run.**
- `fix-database-issues.sql` / `fix-false-error-files.sql` — mass status UPDATEs; both are stale one-offs
  superseded by `backend/app/tasks/recovery.py`. (`comprehensive-database-review.sql` is read-only.)
- `cleanup-test-users.py` — `DELETE FROM "user"`; dry-run unless `--execute`.
- `uninstall-offline-package.sh` — `docker compose down -v`, `docker rmi`, `rm -rf /opt/opentranscribe`.
- `release-tests/*` — `docker volume rm` on `opentranscribe_*` plus `rm -rf $TEST_ROOT`. They also
  **require the live stack to be stopped** (they bind the standard 5173–5180 ports under the stock
  `opentranscribe-*` container names, contrary to their own README). Gated by an `I UNDERSTAND` prompt
  and a protected-path/volume allowlist in `lib/guardrails.sh`.
- `run_benchmark.py` — `docker compose down -v` on `-p otbench`, plus a legacy cleanup pass aimed at the
  **`transcribe-app` dev project** (suppress with `--no-cleanup-legacy`).
- `test-model-download.sh` — `rm -rf ./test-model-cache`, **relative to cwd**.
- `frontend-check.sh` on failure shells out to the `claude` CLI and then `git add`s every modified file
  under `frontend/` — pass `--no-claude` if you don't want your index touched.
- Everything else writes to the **live dev stack**: benchmarks upload files and never delete them;
  `test-all-auth.sh` / `test-fedramp.sh` register real users and never clean up (that is why
  `cleanup-test-users.py` exists); `run-auth-e2e.sh` mutates admin auth config (restored on an exit
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
