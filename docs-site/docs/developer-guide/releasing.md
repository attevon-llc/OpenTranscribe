---
sidebar_position: 7
title: Releasing
description: How an OpenTranscribe release is cut, gated, and published — the staged orchestrator, the criteria file, and the three rehearsal scenarios.
---

# Releasing

A release is a set of **independently runnable, skippable, resumable stages**
driven by `scripts/release.sh`. Nothing here is a checklist you follow by hand;
the mechanics are code, and the gates fail loudly.

:::info[Why it works this way]
The process used to be three markdown checklists that disagreed with each other.
The most recent release proved the cost: one of them documented "append a row to
`expected-schemas.tsv`", nothing enforced it, and the row was silently skipped —
so the file that called itself "the single source of truth for the schema version
of each release" was wrong and no one noticed for four months.

Every rule below is either **derived at run time** or **enforced by a check**.
Nothing depends on remembering. The same failure shape recurred once more
(issue #783): `FROM_VERSIONS` was documented as running the upgrade scenario
once per source, and did nothing — assigned into a variable, read by no loop.
It is now the derivation described under [The rehearsal scenarios](#the-rehearsal-scenarios)
below, guarded by a test that fails on any documented knob nothing reads.
:::

## The command

```bash
./scripts/release.sh status              # where am I?
./scripts/release.sh reset 0.5.0         # clear the ledger before a real run
./scripts/release.sh explain publish     # what does this stage do, and is it reversible?
./scripts/release.sh preflight           # seconds — fails fast on the usual suspects
./scripts/release.sh run 0.5.0 --yes     # the whole sequence (without --yes it stops at `tag`, exit 4)
```

Useful flags on `run`:

| Flag | Effect |
|---|---|
| `--skip scan,rehearse` | Turn stages off |
| `--only build,scan` | Run just these |
| `--from build` | Resume mid-flight |
| `--dry-run` | Print every command, execute nothing |
| `--json` | Machine-readable criteria + an explicit `next[]` |
| `--yes` | Required for `tag`, `publish`, `promote` and `finish`; also approves `rehearse` stopping and restarting the live stack |
| `--patch` | Verify the version delta is a patch and, if the diff allows, waive the rehearsal scenarios — see [Cutting a patch release](#cutting-a-patch-release) |

State lives in `.release/<version>/steps/` (gitignored) and records status,
operator, git SHA, and any override — so a release that dies at hour three
resumes rather than restarting.

**Start a real run from a clean ledger.** After rehearsals the table accumulates
history — stages that failed for reasons since fixed, stages recorded before they
were implemented, stages still `pending` whose work was done by hand. `reset`
clears `.release/<version>/` and nothing else: no artifact, image, or tag is
touched. It prints what it will clear and asks first.

### Driving it from a script or an agent

Exit codes are stable, so nothing has to parse prose to know what happened:

| Code | Meaning | What to do |
|---|---|---|
| `0` | Stage passed | Continue |
| `1` | A **gate failed** | Fix the finding and re-run, or `--force-<stage> "reason"` |
| `2` | **Misuse** — bad arguments | Fix the invocation; retrying verbatim won't help |
| `3` | **Precondition unmet** — live stack up, builder unreachable, dirty tree | Resolve the precondition; this is *not* a gate failure |
| `4` | **Operator aborted** — a declined prompt, or an external stage run without `--yes` | Nothing ran; re-run when ready |

The `1` / `3` split is the one that matters: a `3` means the release was never
evaluated, so treating it as "the release is bad" is wrong.

Add `--json` to any stage for a machine-readable result. Logs go to **stderr**
and JSON to **stdout**, so they never interleave:

```bash
./scripts/release.sh verify 0.5.0 --json | jq -e '.status'
```

```json
{"stage":"scan","version":"v0.5.0","status":"fail",
 "criteria":[{"id":"no-critical-cves","status":"fail","actual":22,"expected":0}],
 "next":["--force-scan \"reason\"","fix the findings and re-run"]}
```

`next[]` enumerates the **legal moves** from that state — it exists so an agent
recovers with a supported action instead of inventing one.

`./scripts/release.sh status --json` returns the whole ledger, which is the
resume point for an agent that lost its context.

### Overriding a gate

A gate can be overridden, never silently:

```bash
./scripts/release.sh scan 0.5.0 --force-scan "why this is acceptable"
```

The reason is **mandatory** — there is deliberately no bare `--force`, because an
override with no recorded justification is the thing the mechanism exists to
prevent. The stage is recorded as `overridden` with the operator and the reason,
not as a pass, and it surfaces in the readiness report. The Fortune-100 posture
is not "no exceptions"; it is "no *undocumented* exceptions".

`--force-<stage>` works for any stage, and an unknown stage name is rejected
rather than silently ignored.

## Stages

| Stage | Does | Leaves this machine? |
|---|---|---|
| `preflight` | Version agreement, clean worktree, remote ARM64 builder reachable, scanners present, `HUGGINGFACE_TOKEN`, model fixtures, disk, deployment matrix, CHANGELOG section (early notice), live-stack state (informational) | no |
| `bump` | Writes all five version sources, promotes `[Unreleased]`, refreshes the committed roadmap snapshot, re-verifies, then commits `chore(release): bump version to X.Y.Z` | no |
| `verify` | Fast gate: consistency, deployment matrix, manifest, structural tests, docs build, visual-baseline freshness | no |
| `test` | `run-dev-tests.sh --backend-only --e2e-smoke --export-capability` (the integration gate plus the smoke E2E subset); **needs the dev stack up** | no |
| `build` | Local images with the version build-args — **pushes nothing** | no |
| `scan` | Trivy/Grype against the **locally built** images | no |
| `rehearse` | Fresh-install, upgrade and lite-mode scenarios; **needs the live stack stopped** (it can stop and restore it for you, with approval) | no |
| `tag` | Annotated tag + push; cuts (or confirms) `release/<major>.<minor>` from the tag | **yes** |
| `publish` | Pushes the **already-built** legs (`docker push`, no rebuild, no re-scan) and assembles the `:vX.Y.Z` indexes; `:vX.Y.Z` only | **yes** |
| `smoke` | Install from Docker Hub; verify each image's capability (CUDA vs CPU) and architecture; **needs the live stack stopped** | no |
| `promote` | Move `:latest` **by digest** — refuses to move it backwards on a backport | **yes** |
| `finish` | GitHub release + assets | **yes** |

### Ordering rules that must not be reordered

- **`build` → `scan` → `rehearse` → `publish`.** The bytes that reach Docker
  Hub are the bytes `build` produced and `scan`/`rehearse` validated: `build`
  records every leg's local image ID in `.release/<v>/legs.tsv`, and `publish`
  refuses (exit 1, naming the leg, before any push) when a leg's ID no longer
  matches, then pushes the local leg tags with `docker push` and assembles each
  `vX.Y.Z` index from the pushed digests with `imagetools create`. It never
  rebuilds and never re-scans. `:latest` moves only in `promote`, by digest.
  Rebuilding after `scan`/`rehearse` therefore needs a fresh `scan`/`rehearse`
  before `publish` (the ledger records the new IDs, so publish itself would
  not catch a rebuild that was never re-validated).
- **`tag` before `publish`**, so CI validates the metadata while the 13.8 GB
  build runs.
- **`promote` and `finish` last.** The GitHub Release is what the installer
  resolves for "latest". There must never be a window where `releases/latest`
  names a version whose images do not exist yet.
- **`:latest` moves by `docker buildx imagetools create`**, a manifest copy — so
  `:latest` and `:vX.Y.Z` are provably the same bytes, not two builds that happen
  to share a source tree.
- **The release branch is cut at `tag`, from the tag it just pushed — never from
  `master`.** `70-tag.sh` derives `release/<major>.<minor>` from the version
  (`v0.5.1` → `release/0.5`) after the tag exists, then asserts the tag is an
  ancestor of `origin/release/<major>.<minor>` — asking origin, not the local
  ref, because that is what a hotfix operator will clone. On a
  patch the branch already exists, so a tag cut from somewhere other than
  this branch fails right here; on the first cut of a minor the branch is
  created from the tag, so the ancestry check holds by construction.
- **`promote` refuses to move `:latest` backwards.** It resolves the newest
  published release from git tags × Docker Hub before touching anything; if the
  version being promoted is OLDER than that, the copy is skipped and `:latest`
  is left alone — a pass, not a failure. See
  [Cutting a patch release](#cutting-a-patch-release).

## Cutting a patch release

Entry condition: the release must be **revertible by pulling the previous
image** — the same rule [the roadmap states for what belongs in a
patch](roadmap.md#between-the-themed-releases-patch-releases), linked here
rather than restated, because a duplicated "what goes in a patch" list is
exactly the kind of table that rotted before (`expected-schemas.tsv`). If
reverting needs a migration, a data fix, or a config change, it was never a
patch.

**The branch.** `release/<major>.<minor>` is cut from the **tag**, by
`70-tag.sh`, the first time a minor ships — never from `master`. It is now
covered by the same CI as `master` (pre-commit, seam-guard, and
`release-validate.yml` all trigger on `release/**`), and by a GitHub ruleset
that blocks force-push, deletion, and direct pushes without a PR.

**The procedure**, verbatim:

```bash
git switch release/0.5
git switch -c hotfix/NNN-short-description
git cherry-pick -x <sha-from-master>
gh pr create --base release/0.5
# ... review, merge ...
git switch release/0.5 && git pull
./scripts/release.sh run 0.5.1 --patch
```

**What `--patch` does.** It VERIFIES a claim, it does not declare one — there is
no flag anywhere that says "this is a patch" (see [Version facts are derived,
never recorded](#version-facts-are-derived-never-recorded)). `release.sh`
resolves the version delta against the highest git tag strictly below the
target:

- If that delta is not a patch (minor, major, or no base tag at all), `--patch`
  is **misuse** — exit 2. Nothing about the release was evaluated, so this is a
  wrong invocation, not a gate that ran and failed.
- If it IS a patch, the diff since the base tag is checked against a widened
  trigger set — an Alembic migration, a `Dockerfile`, a dependency-lock file, a
  `docker-compose*.yml`, or either installer script. Touching any of them means
  the rehearsal runs in full, exactly as without `--patch`. Touching none of
  them means `rehearse`'s three scenarios are WAIVED, recorded in the ledger as
  `status=done detail=patch-rehearsal-waived: <reason>` — a stable, greppable
  prefix distinguishable from `--skip` (`status=skipped`) and
  `--force-rehearse` (`status=overridden`). An empty diff against the base tag
  is treated as a derivation failure, not "nothing changed", and refuses to
  waive.

**`:latest` and backports.** If the version being promoted is older than the
newest already-published release — a hotfix landing after a newer minor has
shipped — `promote` leaves `:latest` alone. That is asserted as a pass, not
skipped as a non-check: moving `:latest` backwards would silently downgrade
every existing user on their next pull.

**The back-merge.** After the release, `release/<major>.<minor>` must be merged
back into `master` so the hotfix isn't lost the next time that branch is
touched. ⚠️ **This step has no gate today** — it is the part of the procedure
most likely to be forgotten, and is flagged here as a known gap rather than a
solved one. A scheduled workflow that fails when a `release/*` branch has
commits unreachable from `master` would close it; none exists yet.

## Criteria live in one file

`scripts/release/release-criteria.yaml` declares every gate: id, command,
severity, and which environments enforce it. The local orchestrator and CI read
the **same file**, so "meets the criteria" means one thing and changing it is a
reviewable diff rather than an edit in three scripts.

A gate can be overridden with `--force-<stage>`, which requires a reason and
records it plus the operator in the ledger. Use it for a real decision — an
accepted CVE with no reachable path — never to turn a red run green.

## The rehearsal scenarios

Three scenarios run in order under `rehearse`: **A** fresh install, **B** upgrade from the
previous published release(s), and **C** the lite (CPU-only, mocked cloud ASR and LLM) deployment
(`test-lite-mode.sh`). A and B rehearse the same `./opentranscribe.sh` path a real operator
runs; see [Upgrading](../operations/upgrading.md) for the operator-facing failure-recovery
guidance this rehearsal is meant to keep accurate.

```bash
./opentr.sh stop            # required — see below
./scripts/release-tests/test-fresh-install.sh
./scripts/release-tests/test-upgrade.sh
./scripts/release-tests/test-lite-mode.sh
```

Scenarios A and C install by **downloading** the `release-manifest.txt` files from the branch
under test (`TO_BRANCH`, defaulting to the current branch). `rehearse` therefore refuses (exit
3) when that branch is not on `origin` — push the branch first.

**Neither takes a version argument.** They discover what to test:

- **TO** comes from the `VERSION` file. It has to: when they run, the new tag
  does not exist yet and the new images are not on Docker Hub.
- **FROM** is DERIVED as a **set**, not a single value (issue #783): the newest
  git tag with published Docker Hub images from each of the last
  `OT_UPGRADE_SOURCE_MINORS` (default **2**) minor series strictly below TO. For
  v0.5.0 that set is `{v0.4.1, v0.3.3}`. A patch TO collapses to a single hop —
  a patch adds no Alembic revisions, so a second hop would re-measure the same
  migration chain at full price for no extra coverage.

This is deliberate. GitLab deleted their equivalent CI job because it read the
previous version from a checked-in file that went stale, and silently validated
an upgrade nobody was performing.

`test-upgrade.sh` **re-execs itself once per derived source** (never an
in-process loop — a `gr_die` in any hop must not lose the evidence about the
*other* hops), tearing its own stack down between hops so the next one can bind
the same stock names and ports. Each hop's evidence lands under its own
`TEST_ROOT/from-<version>/`; a roll-up `REPORT.md` + `hops.tsv` land at the
top-level `TEST_ROOT`. `./scripts/release-tests/test-upgrade.sh --list-sources`
prints the set that would be used and starts nothing (no docker, no
containers — only Docker Hub manifest lookups), so you can sanity-check the
derivation before committing to a multi-hour run.

Overrides: `FROM_VERSION` (singular) pins exactly one hop and disables the
dispatcher entirely; `TO_VERSION` overrides the target; `FROM_VERSIONS`
(plural, space-separated) REPLACES the derived set outright and still runs
once per listed source. `FROM_VERSIONS` is an override of the derivation, not
what "enables" multi-hop — the derivation runs on its own, is what keeps the
**oldest supported** upgrade path exercised as auto-detection moves FROM
forward, and is the mechanism that closed the second half of this
pattern (see the admonition above): a documented feature that did not exist.

:::warning[The live stack must be stopped]
The scenarios run under the installer's stock container names and ports
5173-5180 **by design**, so they exercise exactly what a real user gets. They
cannot run alongside a live deployment. `./scripts/release.sh rehearse` will stop
the stack (`./opentr.sh stop`, data preserved) and restart it afterwards, but only
with approval: `--yes`, `RELEASE_AUTO_STOP_STACK=true`, or an interactive `y`. With
none of those and no tty it refuses with exit 3 and prints the command. The
individual scenario scripts never stop anything: `lib/guardrails.sh` refuses to start
if any `opentranscribe-*` container exists or any of those ports is bound. It
also refuses any path under the live data directories and requires an
`I UNDERSTAND` confirmation.

For a stack that runs *beside* the live one, use
`./opentr.sh start dev --fresh <name> --port-offset N` instead.
:::

:::tip[Non-interactive or backgrounded runs need `--yes`]
The `I UNDERSTAND` confirmation above reads from a tty. Under a backgrounded or
otherwise non-interactive invocation there is no tty to read from, and the prompt
fails with `No such device or address` rather than hanging. Pass `--yes` to skip
it (both scripts also accept `--cleanup`, `--force`, and `test-upgrade.sh` also
takes `--no-rollback`/`--only-rollback` — see each script's own `--help`).
`scripts/release/65-rehearse.sh` already passes `--yes` on all three scenarios when
driven through `./scripts/release.sh rehearse`.
:::

### What the upgrade scenario proves

An upgrade is not only a database check. The scenario asserts:

| Category | Assertion |
|---|---|
| Migration | Alembic head advanced, and equals the single head derived from the chain |
| Prior schema | The FROM release's head **measured** off the running stack equals the head **derived** from that release's own migration chain |
| Data integrity | Row counts, MinIO ETags, per-file transcript prefixes, speakers |
| **Running version** | `/api/version` equals the version under test, and is not `"unknown"` |
| **API routes** | No `METHOD /path` present before the upgrade is missing after — route existence only, not field, parameter or type compatibility (that is `backend/openapi.json`, diffed per-PR in CI) |
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

The upgrade rehearsal's API-routes check proves no endpoint vanished between FROM and TO — it
says nothing about whether an endpoint that still exists still means the same thing. A response
field changing type, or disappearing, passes this check cleanly: it only ever diffs the set of
`METHOD /path` pairs, never their request/response shapes. That is what `backend/openapi.json`
is for — every PR that changes a schema must regenerate it (`backend/venv/bin/python
scripts/generate-openapi.py --write`), and CI's `OpenAPI snapshot is current` step fails the
build if a schema-affecting change lands without a matching regeneration. Read the diff on a
schema PR: it is a **reviewable record of what changed**, not a breaking-change classifier — it
does not tell you whether a change is safe, only that a human is being shown it.

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

`.github/workflows/release-validate.yml` runs on tag pushes, on PRs touching
release-relevant paths, on a schedule, and on manual dispatch. It validates **metadata**, not images: version agreement,
the Alembic chain, the deployment matrix, that every `release-manifest.txt` path
is fetchable at the tag, shellcheck, an empty-database migration, and the docs
build.

It deliberately **does not create the GitHub release**. Images are pushed from a
workstation after the tag, and the installer resolves "latest" from the GitHub
Release — publishing it in CI would point new users at a version whose images do
not exist yet. `finish` owns that, and refuses until this workflow is green.

**`release/**` gets the same coverage as `master`.** A hotfix PR merges onto
`release/<major>.<minor>`, not `master`, and that branch produces the next tag
— it must not be able to land machine-unreviewed code. `pre-commit.yml` and
`release-validate.yml` trigger on pull requests into `release/**`; `seam-guard.yml`
triggers on both pull requests into it and pushes to it, matching how it already
covers `master`. A GitHub ruleset on `release/*` additionally blocks force-push,
branch deletion, and direct pushes without a PR.

:::note[Why publishing is local]
The backend production image is ~13.8 GB. GitHub's free runners cannot build it.
`.github/workflows/docker-publish.yml` is now **retired** entirely (issue #680): it
was a second publisher using the old `:latest-amd64` / `:latest-arm64` grammar, and
its final step assembled `:latest` by hand — which would overwrite the index
`promote` had copied by digest, silently breaking the ":latest and :vX.Y.Z are the
same bytes" guarantee. Publishing happens only through `./scripts/release.sh`.
ARM64 legs use a remote builder over SSH (`scripts/setup-remote-builder.sh`), which
turns 2-3 hours of QEMU emulation into roughly 20 minutes of native build.
:::

### What gets published, and under what tags

Capability lives in the **repository** and is restated in the **tag**. Do not copy this
table into another file — `./scripts/docker-build-push.sh list-platforms` prints it, and
`80-publish.sh` derives its checks from that command rather than from any hardcoded
architecture list:

| Repository | Capability | Legs published | Index | `:latest` |
|---|---|---|---|---|
| `opentranscribe-backend` | `cuda` | `vX.Y.Z-cuda-amd64` | `vX.Y.Z` | digest-copy of index |
| `opentranscribe-backend-lite` | `cpu` | `vX.Y.Z-cpu-amd64`, `vX.Y.Z-cpu-arm64` | `vX.Y.Z` | digest-copy |
| `opentranscribe-frontend` | — | (no capability legs) | `vX.Y.Z` | digest-copy |
| `opentranscribe-docs` | — | (no capability legs) | `vX.Y.Z` | digest-copy |

A fifth table row, `blackwell` (arm64, GPU-generation-gated), appears in `list-platforms` but is
built only by an explicit `docker-build-push.sh blackwell`; `build` and `publish` skip it, so a
release never publishes it and it never carries `:vX.Y.Z` or `:latest`.

`vX.Y.Z-cuda-arm64` is **reserved in the grammar but not built**: there is no aarch64 CUDA
torch wheel at the pinned version, `onnxruntime-gpu` publishes no aarch64 wheels at all, and
diar-native ships no CUDA arm64 build of its sidecar. So the full image is **amd64-only**, and
`opentranscribe.sh` defaults an arm64 host to the lite image with an explanation.

`publish` first proves it is pushing the validated bytes: every declared leg must be in
the build stage's record (`.release/<v>/legs.tsv`) with an unchanged local image ID
(criterion `built-legs-match-ledger`, checked for all legs before any push), each pushed
leg is read back from the registry (`registry-digest-matches-pushed`: the leg tag and the
index both resolve to the digest `docker push` reported) and its config digest must equal the
built image ID (`registry-leg-matches-built-image`). The arm64 legs were `--load`ed from the
remote builder into the local daemon and are pushed from there. On a classic-store (overlay2)
daemon the manifest is regenerated at push time, so the registry manifest digest is not the
one BuildKit exported; the image config (hence the image ID) is the identity that survives,
and that is what is compared. Then it verifies structure, not just existence — the pre-#680 check only grepped the
manifest for an architecture string, which a *degraded but present* arm64 leg passes:

1. each `-<cap>-<arch>` leg tag declares **exactly one** platform, the declared one;
2. each `vX.Y.Z` index declares **exactly** the declared platform set — a missing platform
   fails **and so does an extra one**;
3. legs of the **same** capability are equivalent: identical layer count, size ratio within
   1.25 (lite/frontend/docs) or 2.00 (full). Never compared across capabilities — the whole
   point is that a CUDA image and a CPU image have no reason to be the same size, which is
   why "arm64 is 8.4× smaller" went unnoticed for as long as it did.

## Before you start

Cutting a release is not the place to discover a regression. Run the
[full local test matrix](full-test-matrix.md) on the branch first — its Stage 1 is what
`preflight`/`verify` run automatically, and its Stage 3 rehearsal legs are exactly what
`rehearse` runs below; this page does not re-derive those steps.

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
- [Full application test matrix](./full-test-matrix.md) — the staged local matrix this
  pipeline's stages implement pieces of

### In the repository

| File | What it covers |
|---|---|
| `scripts/release.sh` | The orchestrator — arg parsing, ledger, dispatch |
| `scripts/release/NN-<stage>.sh` | One file per stage; each runnable on its own |
| `scripts/release/release-criteria.yaml` | Gate definitions, read by the script **and** CI |
| `scripts/release-tests/` | The three rehearsal scenarios + `lib/guardrails.sh` |
| `scripts/release-tests/selftest-cleanup.sh` | Numbered cases over the harness's own destructive paths; prints a pass/fail tally — **run after any `guardrails.sh` change** |
| `.claude/skills/release/SKILL.md` | The agent-facing interface |
| `docs/RELEASE_PROCESS.md` | Historical pointer — superseded by this page |
| `CLAUDE.md` → "Cutting a release" | The short version agents read first |
