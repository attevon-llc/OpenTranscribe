# OpenTranscribe Release-Test Harness

End-to-end validation for every OpenTranscribe release. Two scenarios:

| Script | What it proves |
|---|---|
| `test-fresh-install.sh` | A new user runs the documented `setup-opentranscribe.sh` one-liner and ends up with a working stack on the current release |
| `test-upgrade-from-v033.sh` | A user with real data on the previous release can run the documented upgrade path and find their data intact, migrations applied, new features available |

## ⚠️ Precondition: the live stack must be STOPPED

**These scripts are not sandboxed from the live deployment and cannot run alongside it.** Run `./opentr.sh stop` first.

That is deliberate, not an oversight. Both scenarios validate what a *real user* gets from the documented `setup-opentranscribe.sh` one-liner, so they run under the one-liner's own compose project name (`opentranscribe`), its stock `opentranscribe-*` container names, and its standard **5173–5180** host ports. Renaming any of that would test a configuration nobody actually deploys. `TEST_PROJECT_NAME` (`ot-reltest-*`) is only a **label namespace for cleanup** — it is not passed to `docker compose` as `COMPOSE_PROJECT_NAME`.

Rather than isolate by name, `lib/guardrails.sh` refuses to start unless the field is clear, and walls off live data:

- **Refuses to run if any `opentranscribe-*` container exists**, running *or* stopped (a stopped one would collide on `container_name`).
- **Refuses to run if any of the 5173–5180 ports is already bound.**
- **Refuses any `TEST_ROOT` or bind-mount source that resolves under a protected path** — `/mnt/nas/opentranscribe-minio`, `/mnt/nas/opentranscribe`, `/mnt/nvm/opentranscribe`, `/mnt/nvm/repos/transcribe-app`, and several personal-data paths. `TEST_ROOT` must additionally sit under `/mnt/nvm/opentranscribe-test-runs` or `/tmp/ot-reltest-` unless `OT_RELEASE_TEST_ALLOW_PATH=1`.
- **Never deletes the production named volumes** (`postgres_data`, `minio_data`, `redis_data`, `opensearch_data`, `flower_data`); cleanup re-checks the path allowlist before removing anything.
- **Requires an interactive `I UNDERSTAND` confirmation** and a free-disk check before touching anything.

So the production MinIO at `/mnt/nas/opentranscribe-minio` and Postgres at `/mnt/nvm/opentranscribe/pg` are genuinely protected — but by refusing to start, not by running in parallel. If you want a stack that *can* run beside the live one, use `./opentr.sh start dev --fresh <name> --port-offset N` instead; that is the isolated-deployment machinery.

## Pre-release checklist

Run through this list for every release before tagging:

1. **Bump version strings** (`VERSION`, `frontend/package.json`, `pyproject.toml`, `frontend/package-lock.json`)
2. **Update `CHANGELOG.md`** with the new section
3. **Append the new release row** to `expected-schemas.tsv`:
   ```
   v0.4.0    <alembic_head>
   ```
4. **Run pre-commit** locally (`pre-commit run --all-files`)
5. **Merge feature branch into `master` preserving history** (no squash, no rebase)
6. **Build local 0.4.0 images** (do not push yet):
   ```bash
   docker build -t davidamacey/opentranscribe-backend:0.4.0 -t davidamacey/opentranscribe-backend:latest -f backend/Dockerfile.prod backend/
   docker build -t davidamacey/opentranscribe-frontend:0.4.0 -t davidamacey/opentranscribe-frontend:latest -f frontend/Dockerfile.prod frontend/
   ```
7. **Fill in `.env.test-secrets`** (see below) — only required once per machine
8. **Run Scenario A**: `./test-fresh-install.sh`
9. **Run Scenario B**: `./test-upgrade-from-v033.sh`
10. **Review** both `REPORT.md` files. All assertions must be PASS.
11. **Push images to Docker Hub**, tag `v0.4.0`, create GitHub release
12. **Cleanup** sandboxes: `./test-fresh-install.sh --cleanup` and `./test-upgrade-from-v033.sh --cleanup`

## Secrets file

Both scripts read `scripts/release-tests/.env.test-secrets` (gitignored). On first run, `test-fresh-install.sh` writes a template — fill it in:

```bash
HUGGINGFACE_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxx   # required for PyAnnote
LLM_PROVIDER=                                       # optional
VLLM_BASE_URL=
VLLM_API_KEY=
VLLM_MODEL_NAME=
OPENAI_API_KEY=
OPENAI_MODEL_NAME=
```

`HUGGINGFACE_TOKEN` is the only required key; without it the PyAnnote diarization model cannot download and the test will fail at the first transcription. LLM keys are optional — AI-summary assertions are skipped when absent.

## Running the scenarios

```bash
# Scenario A — fresh install via the one-liner
./scripts/release-tests/test-fresh-install.sh

# Scenario B — upgrade from v0.3.3
./scripts/release-tests/test-upgrade-from-v033.sh

# Skip the confirmation gate (for unattended re-runs)
./scripts/release-tests/test-fresh-install.sh --yes

# Force re-run from phase 0 (otherwise resumes from the last completed phase)
./scripts/release-tests/test-fresh-install.sh --force

# Tear down (only resources labeled com.opentranscribe.release-test=*)
./scripts/release-tests/test-fresh-install.sh --cleanup
./scripts/release-tests/test-upgrade-from-v033.sh --cleanup
```

Each scenario writes:
- `$TEST_ROOT/run.log` — full stdout/stderr
- `$TEST_ROOT/REPORT.md` — pass/fail per assertion
- `$TEST_ROOT/snapshots/{before,after}/` — postgres + MinIO + transcript dumps (Scenario B only)
- `$TEST_ROOT/.phase/<n>.done` — resumability markers

## Isolation summary

The scenarios are isolated from live **data**, not from live **names or ports** — which is why the
live stack has to be down. The only columns that differ from a real user's deployment are the data
root and the cleanup label.

| Property | Live deployment | Scenario A | Scenario B |
|---|---|---|---|
| Compose project name | `transcribe-app` | `opentranscribe` (one-liner default) | `opentranscribe` (one-liner default) |
| Container prefix | `opentranscribe-` | `opentranscribe-` | `opentranscribe-` |
| Frontend port | 5173 | 5173 | 5173 |
| Backend port | 5174 | 5174 | 5174 |
| Postgres port | 5176 | 5176 | 5176 |
| Data root | `/mnt/nas/opentranscribe-minio`, `/mnt/nvm/opentranscribe/pg` | `$TEST_ROOT/install/.../data/` | `$TEST_ROOT/before/.../data/` then upgraded in place |
| Cleanup label namespace | none | `ot-reltest-fresh` | `ot-reltest-upgrade` |
| Label | none | `com.opentranscribe.release-test=fresh-install` | `com.opentranscribe.release-test=upgrade` |

Every port is overridable (`FRONTEND_PORT`, `BACKEND_PORT`, `POSTGRES_PORT`, …) if you need to move
them, but the container names are not — they come from the installer being tested.

## Future releases

Each future release follows the same flow. Only one file needs an edit:

1. Append a row to `expected-schemas.tsv` with the new release's Alembic head
2. Run `FROM_VERSION=v0.4.0 ./test-upgrade-from-v033.sh` (or whichever version is now "previous")

For pre-release testing on a feature branch:

```bash
TO_BRANCH=feat/whatever ./test-fresh-install.sh
```

## Recovery from a crashed test run

If a previous run died after creating containers but before applying labels (rare — the safety harness applies labels via `cp_inject_labels` very early), you may have orphan resources. Find and remove them by **explicit name**, never by wildcard:

```bash
docker ps -a --filter name=^ot-reltest- --format '{{.Names}}'
docker volume ls --filter name=^ot_reltest_ --format '{{.Name}}'
```

Then `docker stop` / `docker rm` / `docker volume rm` each one individually after eyeballing it. The `--cleanup` flag only acts on labeled resources, so it won't help with orphans.

## Edge cases & known limitations

See the dedicated section in the planning doc and the `Edge Cases & Mitigations` section comments in `test-upgrade-from-v033.sh`. The short list:

- **GPU contention**: tests default to CPU. Pass `CUDA_VISIBLE_DEVICES=2` (or whichever slot is free) before invoking if you want GPU acceleration. Do not steal the GPU the live workers are using.
- **Disk space**: each scenario needs ~20 GB free under `$TEST_ROOT` and ~10 GB on the docker root.
- **Docker Hub rate limits**: an unauthenticated pull is limited to 100/6h per IP. Login (`docker login`) if you're iterating.
- **Public test URLs may decay**: edit `fixtures/test-urls.txt` if archive.org links 404.
- **Rollback (0.4.0 → v0.3.3) is not supported** — the migration chain is one-way. Don't run Scenario B in reverse.
- **`expected-schemas.tsv`** is the single source of truth for "what Alembic head should we see for release X". Append, never edit historical rows.
