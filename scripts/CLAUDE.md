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

## Fresh deployments (`opentr.sh --fresh`, state in `.fresh/`)

Root `CLAUDE.md` points here for the mechanics. `.fresh/` is gitignored and fully regenerated.

- `.fresh/<name>.yml` — the ONLY generated compose overlay. It re-pins every hard-coded
  `container_name` to `otfresh-<name>-*` (`FRESH_NAMED_SERVICES` in `opentr.sh`).
- `.fresh/<name>.offset` — the recorded `--port-offset` (plain integer, absent = 0). Read on re-up,
  `status --fresh`, and `fresh-list`; deleted by `fresh-destroy` and by `--port-offset 0`.
- **`--port-offset` is env-var driven, never an overlay.** `fresh_apply_port_offset()` exports the
  `*_PORT` variables the base compose files already interpolate (`FRESH_PORT_VARS`: FRONTEND 5173,
  BACKEND 5174, FLOWER 5175, POSTGRES 5176, REDIS 5177, MINIO 5178, MINIO_CONSOLE 5179, OPENSEARCH
  5180, OPENSEARCH_ADMIN 5181, DOCS 5183; plus KEYCLOAK 8180 / STEP_CA 9000 with
  `--with-keycloak-test`). **Never reintroduce a generated `-ports.yml`:** compose *appends* port
  lists across files, so a second `ports:` entry publishes the base port too and the "isolated"
  stack collides with the main one (issue #343). A shell env var beats `.env` for interpolation, and
  a value already set in `.env` is the base the offset is applied to.
- Adding a published port to `docker-compose.yml` / `docker-compose.override.yml`? Add its variable
  to `FRESH_PORT_VARS` in the same commit, or `--port-offset` silently leaves that one service on
  the main stack's port.
- The pre-flight bind check runs at **every** offset (not just 0) and refuses to start if any
  resolved port is taken, so a bad offset fails before `compose up`.
- `--with-ldap-test` / `--with-smb-test` / `--with-monitoring` hard-code container names (and
  ldap/smb their ports): NOT isolated by `--fresh`, NOT moved by `--port-offset`; the script warns.

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
