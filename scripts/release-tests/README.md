# OpenTranscribe Release-Test Harness

End-to-end validation for every OpenTranscribe release. Three scenarios:

| Script | What it proves |
|---|---|
| `test-fresh-install.sh` | A new user runs the documented `setup-opentranscribe.sh` one-liner and ends up with a working stack on the current release |
| `test-upgrade.sh` | A user with real data on the previous release can run the documented upgrade path and find their data intact, migrations applied, new features available |
| `test-lite-mode.sh` | The no-GPU lite **topology and pipeline** (`docker-compose.lite.yml`, cloud-only ASR) run the real upload -> ASR -> segments/speakers -> search -> chat flow, against mocked cloud ASR (`scripts/mock-asr-server.py`, a Gladia stand-in) and a mocked LLM (`scripts/mock-llm-server.py`) — no GPU, vendor API key, or network egress required. Complements `scripts/lite-smoke.sh` (Stage 2, Cycle 2D), which only checks lite/cpu-only topology, not the pipeline. Lite became a shipped deployment shape in issue #680 — see below. |

## Scenarios A and B run the SHIPPED commands

Both bring stacks up with **`./opentranscribe.sh start` / `update`** — the management
script `setup-opentranscribe.sh` places next to the compose files, and the one the
installer's own `prompt_start()` runs. They do **not** hand-build
`docker compose -f docker-compose.yml -f docker-compose.prod.yml …` chains, and they do
**not** use `./opentr.sh` (the development script, deliberately absent from
`release-manifest.txt`; no curl install has it).

That matters because `opentranscribe.sh:get_compose_files()` is the single owner of
overlay selection — GPU vs Blackwell vs CPU-only, nginx, scheduled backup. While the
scenarios built their own chains, that whole layer was **dead code at rehearsal time**,
and `release-manifest.txt`'s header records what it cost: a fresh install on a Blackwell
card silently ran the wrong image. Both scenarios now read the resolved chain back
(`./opentranscribe.sh compose-files`) and assert it, including that every overlay the
manifest promises was actually downloaded. Rationale and the full finding list:
[`REHEARSAL_ALIGNMENT_PLAN.md`](REHEARSAL_ALIGNMENT_PLAN.md).

### Scenario C (`--lite`) is now a SHIPPED deployment shape (changed by issue #680)

This section previously said the opposite, and the three facts it rested on were all
changed by #680: `docker-compose.lite.yml` **is** in `release-manifest.txt` (a curl
install downloads it), `get_compose_files()` **has** a `DEPLOYMENT_MODE=lite` branch, and
`scripts/docker-build-push.sh all` **does** build and publish
`opentranscribe-backend-lite`. `test_lite_mode_is_reachable_by_a_shipped_deployment`
(`backend/tests/unit/test_compose_file_selection.py`) now asserts all three in the
positive, so this section still cannot go stale unnoticed.

It matters most on **arm64**, where the full/CUDA image publishes no manifest at all, so
`opentranscribe.sh` defaults an arm64 host to lite. Lite is the only published deployment
for that architecture.

`test-lite-mode.sh` is nevertheless still the one scenario that hand-builds its chain.
That is now justified differently: it must pin one fixed chain regardless of the host, so
that a machine with a GPU does not have the shipped selector add the GPU overlay and
defeat the no-GPU shape it exists to exercise. Driving it through
`DEPLOYMENT_MODE=lite FORCE_CPU_MODE=true ./opentranscribe.sh start` is now possible and
is tracked as follow-up in that test module's exemption entry.

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
3. *(nothing to do — the Alembic head is derived, see "Future releases" below)*
4. **Run pre-commit** locally (`pre-commit run --all-files`)
5. **Merge feature branch into `master` preserving history** (no squash, no rebase)
6. **Build local images without publishing** (`BUILD_MODE=local` loads them into
   the local daemon and pushes nothing):
   ```bash
   BUILD_MODE=local PUSH_LATEST=false ./scripts/docker-build-push.sh all
   ```
   Do NOT use a bare `docker build`: it omits `--build-arg APP_VERSION`, so the
   image reports its version as `"unknown"` and the harness's
   "running version is the version under test" assertion fails.
7. **Fill in `.env.test-secrets`** (see below) — only required once per machine
8. **Run Scenario A**: `./test-fresh-install.sh`
9. **Run Scenario B**: `./test-upgrade.sh`
10. **Review** both `REPORT.md` files. All assertions must be PASS.
11. **Push images to Docker Hub**, tag the release, create the GitHub release
12. **Cleanup** sandboxes: `./test-fresh-install.sh --cleanup` and `./test-upgrade.sh --cleanup`

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

# Scenario B — upgrade from the previous published release (auto-detected)
./scripts/release-tests/test-upgrade.sh

# Scenario C — lite-mode full pipeline rehearsal (mocked cloud ASR + mocked LLM)
./scripts/release-tests/test-lite-mode.sh

# Skip the confirmation gate (for unattended re-runs)
./scripts/release-tests/test-fresh-install.sh --yes

# Force re-run from phase 0 (otherwise resumes from the last completed phase)
./scripts/release-tests/test-fresh-install.sh --force

# Tear down (only resources labeled com.opentranscribe.release-test=*)
./scripts/release-tests/test-fresh-install.sh --cleanup
./scripts/release-tests/test-upgrade.sh --cleanup
./scripts/release-tests/test-lite-mode.sh --cleanup
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

**Nothing needs an edit.** Both scenarios discover their own versions:

- **TO** comes from the `VERSION` file. It cannot come from anywhere else: when
  the scenarios run, the new tag does not exist yet and the new images are not on
  Docker Hub.
- **FROM** is DERIVED as a **set** (issue #783), not a single tag: the newest
  git tag with published Docker Hub images from each of the last
  `OT_UPGRADE_SOURCE_MINORS` (default **2**) minor series strictly below TO —
  for v0.5.0, `{v0.4.1, v0.3.3}`. A tag with no images is not something a user
  could be running, so it is never a valid upgrade source. A patch TO
  collapses to a single hop (a patch adds no Alembic revisions, so a second
  hop would re-measure the identical chain at full price).

`test-upgrade.sh` **re-execs itself once per derived source**, tearing down
between hops so the next one can bind the stock names/ports the previous one
is still holding. Run `./test-upgrade.sh --list-sources` to see the set that
would be used without starting anything.

Overrides still work — `FROM_VERSION` (singular) pins exactly one hop and
skips the dispatcher; `TO_VERSION` overrides the target; `FROM_VERSIONS`
(plural, space-separated) REPLACES the derived set outright and still runs
once per listed source. `FROM_VERSIONS` is an override of the derivation —
the derivation itself is what keeps the oldest supported hop exercised as
auto-detection moves FROM forward; it needs no env var to "turn on".

`REQUIRE_PREVIOUS=1` turns "no published previous release" into a failure instead
of a skip; the release gate sets it, a first-ever release does not.

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

See the dedicated section in the planning doc and the `Edge Cases & Mitigations` section comments in `test-upgrade.sh`. The short list:

- **GPU contention**: tests default to CPU. Pass `CUDA_VISIBLE_DEVICES=2` (or whichever slot is free) before invoking if you want GPU acceleration. Do not steal the GPU the live workers are using.
- **Disk space**: each scenario needs ~20 GB free under `$TEST_ROOT` and ~10 GB on the docker root.
- **Docker Hub rate limits**: an unauthenticated pull is limited to 100/6h per IP. Login (`docker login`) if you're iterating. `ver_upgrade_sources`'s manifest *probes* are memoized (`OT_HUB_CACHE_DIR`, ~4 calls total for the default 2-series derivation) and shared across a multi-hop dispatch, but each hop still *pulls* its own FROM images — that cost is per hop, not memoized, so a multi-hop run against an unauthenticated IP is more likely to hit the limit than a single-hop one.
- **Public test URLs may decay**: edit `fixtures/test-urls.txt` if archive.org links 404.
- **The Alembic migration chain is one-way, and the scenario refuses to run
  when FROM is not strictly older than TO** — that constraint is real and
  still holds. It used to be phrased as "Rollback is not supported", which
  conflated the migration chain with the separate backup/restore MECHANISM
  (`opentranscribe.sh backup`/`restore`, `opentranscribe.sh update --rollback`) — that
  mechanism **is** rehearsed, by `test-upgrade.sh`'s phases 13-17 (issue
  #598; the rehearsal itself stages `opentranscribe.sh` — the shipped production
  command — rather than `opentr.sh` since issue #613). What those phases prove:
  `opentranscribe.sh backup` and the shipped
  `pg_dump` recipe both restore an exact point-in-time database state
  (content digests, not just row counts), `update --rollback` puts the FROM
  image back and the FROM image serves the restored FROM database through
  its real API, and the documented recovery loop (roll back -> re-upgrade)
  completes cleanly. What it deliberately does NOT cover: backup
  `--encrypt` (unattended `gpg` needs a passphrase file the CLI does not
  support — filed as a follow-up), the in-app scheduled-backup system
  (`app/services/backup_service.py` — a separate implementation, its own
  end-to-end restore rehearsal is a follow-up), and MinIO/OpenSearch restore
  (the DB restore does not touch either — asserted, not merely unclaimed).
  Flags: `--no-rollback` (or `ROLLBACK_REHEARSAL=0`) skips phases 13-17;
  `--only-rollback` resumes at phase 12 against an already-completed
  `TEST_ROOT` (run the full scenario first — it does not fabricate that
  state); `ROLLBACK_INJECT_FAULT=truncate|no-damage|stale-oracle`
  deliberately breaks one input of the tail so its own failure detection is
  exercised for real — see `test-upgrade.sh --help` and
  `selftest-rollback-fault-injection.sh` (a ~1-minute self-test against a
  throwaway isolated Postgres container, run it after any change to
  `lib/db-snapshot.sh`).
- **The Alembic head is derived, never recorded.** `expected-schemas.tsv` used to
  claim that role but was read by no script and never got its `v0.4.1` row.
  `lib/alembic-head.py` computes the single head from the `down_revision` graph —
  including for the FROM release, from that release's own worktree — and phase 10
  asserts measured-equals-derived. There is no table to keep up to date.
