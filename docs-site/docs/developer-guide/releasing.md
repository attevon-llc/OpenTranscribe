---
sidebar_position: 7
title: Releasing
description: How an OpenTranscribe release is cut, gated, and published — the staged orchestrator, the criteria file, and the two rehearsal scenarios.
---

# Releasing

A release is a set of **independently runnable, skippable, resumable stages**
driven by `scripts/release.sh`. Nothing here is a checklist you follow by hand;
the mechanics are code, and the gates fail loudly.

:::info Why it works this way
The process used to be three markdown checklists that disagreed with each other.
The most recent release proved the cost: one of them documented "append a row to
`expected-schemas.tsv`", nothing enforced it, and the row was silently skipped —
so the file that called itself "the single source of truth for the schema version
of each release" was wrong and no one noticed for four months.

Every rule below is either **derived at run time** or **enforced by a check**.
Nothing depends on remembering.
:::

## The command

```bash
./scripts/release.sh status              # where am I?
./scripts/release.sh explain publish     # what does this stage do, and is it reversible?
./scripts/release.sh preflight           # seconds — fails fast on the usual suspects
./scripts/release.sh run 0.5.0           # the whole sequence
```

Useful flags on `run`:

| Flag | Effect |
|---|---|
| `--skip scan,rehearse` | Turn stages off |
| `--only build,scan` | Run just these |
| `--from build` | Resume mid-flight |
| `--dry-run` | Print every command, execute nothing |
| `--json` | Machine-readable criteria + an explicit `next[]` |
| `--yes` | Required for any stage that leaves this machine |

State lives in `.release/<version>/steps/` (gitignored) and records status,
operator, git SHA, and any override — so a release that dies at hour three
resumes rather than restarting.

## Stages

| Stage | Does | Leaves this machine? |
|---|---|---|
| `preflight` | Version agreement, clean worktree, remote ARM64 builder reachable, scanners present, `HUGGINGFACE_TOKEN`, model fixtures, disk, deployment matrix | no |
| `bump` | Writes all five version sources, promotes `[Unreleased]`, re-verifies before committing | no |
| `verify` | Fast gate: consistency, deployment matrix, manifest, structural tests, docs build | no |
| `test` | `run-integration-tests.sh` + the E2E suite | no |
| `build` | Local images with the version build-args — **pushes nothing** | no |
| `scan` | Trivy/Grype against the **locally built** images | no |
| `rehearse` | Fresh-install and upgrade scenarios | no |
| `tag` | Annotated tag + push | **yes** |
| `publish` | Multi-arch push of `:vX.Y.Z` only | **yes** |
| `smoke` | Install from Docker Hub; verify both architectures | no |
| `promote` | Move `:latest` **by digest** | **yes** |
| `finish` | GitHub release + assets | **yes** |

### Ordering rules that must not be reordered

- **`build` → `scan` → `rehearse` → `publish`.** Validated bytes reach Docker Hub
  only after the scenarios pass, because `:latest` is what every existing user
  pulls.
- **`tag` before `publish`**, so CI validates the metadata while the 13.8 GB
  build runs.
- **`promote` and `finish` last.** The GitHub Release is what the installer
  resolves for "latest". There must never be a window where `releases/latest`
  names a version whose images do not exist yet.
- **`:latest` moves by `docker buildx imagetools create`**, a manifest copy — so
  `:latest` and `:vX.Y.Z` are provably the same bytes, not two builds that happen
  to share a source tree.

## Criteria live in one file

`scripts/release/release-criteria.yaml` declares every gate: id, command,
severity, and which environments enforce it. The local orchestrator and CI read
the **same file**, so "meets the criteria" means one thing and changing it is a
reviewable diff rather than an edit in three scripts.

A gate can be overridden with `--force-<stage>`, which requires a reason and
records it plus the operator in the ledger. Use it for a real decision — an
accepted CVE with no reachable path — never to turn a red run green.

## The two rehearsal scenarios

```bash
./opentr.sh stop            # required — see below
./scripts/release-tests/test-fresh-install.sh
./scripts/release-tests/test-upgrade.sh
```

**Neither takes a version argument.** They discover what to test:

- **TO** comes from the `VERSION` file. It has to: when they run, the new tag
  does not exist yet and the new images are not on Docker Hub.
- **FROM** is the newest git tag below TO that **also has published Docker Hub
  images**. A tag with no images is not something a user could be running, so it
  is not a valid upgrade source.

This is deliberate. GitLab deleted their equivalent CI job because it read the
previous version from a checked-in file that went stale, and silently validated
an upgrade nobody was performing.

Overrides: `FROM_VERSION`, `TO_VERSION`, and `FROM_VERSIONS` (plural,
space-separated) to run the scenario once per source. Use `FROM_VERSIONS` on
minor and major releases to keep the **oldest supported** upgrade exercised —
once auto-detection moves FROM forward, the older path stops being tested.

:::warning The live stack must be stopped
The scenarios run under the installer's stock container names and ports
5173-5180 **by design**, so they exercise exactly what a real user gets. They
cannot run alongside a live deployment, and `lib/guardrails.sh` refuses to start
if any `opentranscribe-*` container exists or any of those ports is bound. It
also refuses any path under the live data directories and requires an
`I UNDERSTAND` confirmation.

For a stack that runs *beside* the live one, use
`./opentr.sh start dev --fresh <name> --port-offset N` instead.
:::

### What the upgrade scenario proves

An upgrade is not only a database check. The scenario asserts:

| Category | Assertion |
|---|---|
| Migration | Alembic head advanced, and equals the single head derived from the chain |
| Prior schema | The FROM release's head **measured** off the running stack equals the head **derived** from that release's own migration chain |
| Data integrity | Row counts, MinIO ETags, per-file transcript prefixes, speakers |
| **Running version** | `/api/version` equals the version under test, and is not `"unknown"` |
| **API contract** | No route present before the upgrade is missing after |
| Search | The OpenSearch ML model is `DEPLOYED`, not a silent BM25 fallback |
| **New work** | A file uploaded **after** the upgrade transcribes, produces segments, and becomes searchable |

The running-version assertion is what turns "a container started" into "the new
code is running": with `pull_policy: never` and local tag pinning, a silently
stale image would otherwise pass every data assertion against the **old** binary.

The new-work assertion (phase 11) covers the opposite blind spot. Everything
else here inspects data at rest, so **a migration that preserves every existing
row while making every INSERT fail is a total upgrade failure that phases 6-10
report as a clean pass.** Phase 11 uploads a file that was deliberately *not*
seeded before the upgrade and requires it to complete — which is what exercises
the Celery workers under the new image, the ASR stack, the OpenSearch mapping,
and the post-migration insert path.

### Test media

The scenarios read `TEST_MEDIA_DIR` (default
`/mnt/nvm/opentranscribe-test-runs/test-media`), sorted; the first two files are
seeded into the FROM release, and the first unseeded file is what phase 11
uploads after the upgrade. **Sorted** matters: `find` returns directory order, so
without it, which files get seeded varies per run and phase 11 cannot reliably
reserve an unseeded one.

Supply at least three files, and prefer real multi-speaker material — diarization
is only meaningfully exercised by content that has more than one speaker.
`TEST_MEDIA_MAX_SIZE` (default `100M`) bounds run time; it was previously a
hardcoded `5M`, which silently excluded every realistic sample.

### Cleanup and your data

`--cleanup` removes containers, volumes, networks and the test root. Because the
scenarios run under the installer's **stock** project name, their volumes are
called `opentranscribe_postgres_data` and so on — the same names a real
deployment uses. Deleting by name would therefore be indistinguishable from
deleting a user's database, so cleanup never does that. It removes a stock-named
resource only when all three hold:

1. preflight recorded it as **absent before this run**, so the run created it,
2. it carries no `.opentranscribe-live-data` marker (probed **inside a
   container** — a volume's mountpoint is root-owned, so a host-side check
   silently reports "no marker" for every volume), and
3. no container is using it.

No ownership record means no authority to delete, which is what protects a
machine whose live deployment happens to use named volumes. Bind-mounted data —
including the NAS MinIO dataset — is not addressable by `docker volume rm` at
all, and is separately listed in `GR_PROTECTED_PATHS`.

`scripts/release-tests/selftest-cleanup.sh` runs these rules against real
volumes. It is worth running after any change to `guardrails.sh`: on its first
execution it caught the marker check deleting a volume it should have refused.

## Version facts are derived, never recorded

- **The Alembic head** comes from the `down_revision` graph
  (`scripts/release-tests/lib/alembic-head.py`), including for the FROM release,
  read from that release's own worktree. There is no table to keep updated.
  `expected-schemas.tsv` used to hold this and was deleted.
- **Version agreement** across `VERSION`, `pyproject.toml`,
  `frontend/package.json`, `frontend/package-lock.json` (**both** version fields),
  the CHANGELOG section, and the git tag is checked by
  `scripts/release/check-version-consistency.py`, which also runs as a pre-commit
  hook and a unit test.

## What CI does

`.github/workflows/release-validate.yml` runs on tag pushes and on PRs touching
release-relevant paths. It validates **metadata**, not images: version agreement,
the Alembic chain, the deployment matrix, that every `release-manifest.txt` path
is fetchable at the tag, shellcheck, an empty-database migration, and the docs
build.

It deliberately **does not create the GitHub release**. Images are pushed from a
workstation after the tag, and the installer resolves "latest" from the GitHub
Release — publishing it in CI would point new users at a version whose images do
not exist yet. `finish` owns that, and refuses until this workflow is green.

:::note Why publishing is local
The backend production image is ~13.8 GB. GitHub's free runners cannot build it —
that is why `docker-publish.yml`'s backend ARM64 job is disabled. ARM64 builds use
a remote builder over SSH (`scripts/setup-remote-builder.sh`), which turns a 2-3
hour QEMU emulation into roughly 20 minutes of native build.
:::

## Before you start

`preflight` checks all of this, but knowing it saves a cycle:

- **Clean worktree** — a release must be reproducible from its tag.
- **The remote ARM64 builder** is a machine on the LAN, and its address can move.
  Preflight prints the stale endpoint and the fix rather than failing at publish
  time.
- **`HUGGINGFACE_TOKEN`** in `scripts/release-tests/.env.test-secrets`, or both
  rehearsal scenarios fail at their first transcription — hours in.
- **Model fixtures**: `./scripts/release-tests/provision-test-media.sh` derives
  two short real-speech clips from an asset already in the repo. The scenarios
  assert a **non-empty** transcript, so a silent or synthetic fixture fails for a
  reason unrelated to the release.

## Related

- [Upgrading](../operations/upgrading.md) — what a user runs on their deployment
- [Deployment configuration](../operations/deployment-configuration.md) — the
  permutations the matrix validates
- [Testing](./testing.md) — the suites the `test` stage runs
