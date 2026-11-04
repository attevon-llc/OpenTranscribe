---
name: release
description: Cut an OpenTranscribe release. Use when the user says "cut a release", "release vX.Y.Z", "ship a version", "publish a release", "/release", or asks to bump the version and tag. Drives scripts/release.sh; do NOT hand-run the individual git/docker/gh commands.
---

# Release

**The mechanics are code, not a checklist.** `scripts/release.sh` owns every
step. Your job is the two things a script cannot do — writing the CHANGELOG prose
and the blog post — plus reading gate output and deciding what to do about it.

This replaced three competing markdown checklists that disagreed with each other
(`.claude/commands/release.md`, `docs/RELEASE_PROCESS.md`, and
`scripts/release-tests/README.md`). The most recent release proved why: the
`expected-schemas.tsv` step was documented in one of them, enforced by nothing,
and silently skipped.

## Never do these by hand

| Don't | Do |
|---|---|
| Edit `VERSION`, `pyproject.toml`, `package.json`, `package-lock.json` | `./scripts/release.sh bump X.Y.Z` |
| `docker build ... -t ...:latest` | `BUILD_MODE=local ./scripts/docker-build-push.sh` |
| `git tag` + `git push` + `gh release create` | the `tag` / `finish` stages |
| Append a row to `expected-schemas.tsv` | nothing — that file is deleted; the Alembic head is derived |

A bare `docker build` omits `--build-arg APP_VERSION`, so the image reports its
version as `"unknown"` and the harness's "running version is the version under
test" assertion fails. That is the single most common way to waste an hour here.

## The flow

```bash
./scripts/release.sh status              # where am I?
./scripts/release.sh preflight           # seconds; fails fast on the usual suspects
./scripts/release.sh bump 0.5.0          # all five version sources, self-verified
#   <- write the CHANGELOG section and the blog post here
./scripts/release.sh verify              # fast gate: consistency, matrix, docs build
./scripts/release.sh test                # the pre-merge gate on its OWN fresh stack (see "Running the stages")
./scripts/release.sh build               # local images, nothing published
./scripts/release.sh scan                # scans the LOCAL images + image-hygiene check per leg
git push -u origin <branch>              # rehearse installs the branch's files from origin
./opentr.sh stop                         # rehearse and smoke need the live stack down
./scripts/release.sh rehearse            # fresh-install, upgrade and lite-mode scenarios
./scripts/release.sh tag --yes           # ← first step that leaves this machine
./scripts/release.sh publish --yes       # pushes the BUILT legs + assembles :vX.Y.Z; never :latest, never rebuilds
./scripts/release.sh smoke               # install from Hub; check capability and arch
./scripts/release.sh promote --yes       # move :latest by digest
./scripts/release.sh finish --yes        # GitHub release + assets
```

`run` does the whole sequence: `./scripts/release.sh run 0.5.0 --yes --skip scan`
(without `--yes` it stops at `tag` with exit 4).
`--dry-run` prints without executing. `--from <stage>` resumes. `--only a,b` runs
just those stages. `--json` on any stage gives machine-readable criteria and an
explicit `next[]`. `--patch` (from `release/<major>.<minor>`) verifies the version
delta is a patch and may waive the rehearsal; see `releasing.md`'s "Cutting a patch
release". `reset <version>` clears the ledger; `explain <stage>` prints a stage's
purpose and reversibility.

Exit codes: `0` pass, `1` gate failed, `2` misuse, `3` precondition unmet, `4`
operator abort.

## Running the stages

- **Stop everything first.** No other gate, rehearsal, `run-dev-tests.sh` or `otfresh-*` stack; the dev
  stack down before `rehearse`. Nobody else writes to the checkout while a stage runs, and neither do
  you: edit in a worktree (`git worktree add .claude/worktrees/<name> -b <branch> origin/<prep>`).
- **`test` builds and destroys its own isolated stack** (`rel-<version>`, port offset 400, tmpfs
  OpenSearch/Postgres data, NAS overlay never loaded, library seeded, first-run wizard completed,
  frontend warmed). Env: `RELEASE_TEST_FRESH_NAME`, `RELEASE_TEST_PORT_OFFSET`,
  `RELEASE_TEST_FAST_VOLUMES`, `RELEASE_TEST_MAX_LOAD`, `RELEASE_TEST_OS_MAX_SECONDS`,
  `RELEASE_TEST_KEEP=1` (leave the stack up to diagnose a failure). Functions: `scripts/release/fresh-stack-lib.sh`.
- **Preflight (exit 3 with one reason line, changes nothing)** runs before the build and again once the
  stack is healthy: no other gate process or `otfresh-*` stack; a scratch OpenSearch index creates and
  deletes within 5 s; no `blocks.*` cluster setting and no `read_only_allow_delete` index; the data volumes
  really are tmpfs; load below the ceiling. Each exists because its absence cost a multi-hour run.
- **`test` may run in parallel with `build` then `scan`** (own stack, offset ports, no built image). A second
  `test`, `rehearse`, `run-dev-tests.sh`, `run-integration-tests.sh` or other fresh stack still blocks it.
  Then `rehearse` after `scan`; it needs the host quiet.
- **Detach long stages** and wait on the pid; a harness task stop must not kill them:
  `setsid nohup ./scripts/release.sh <stage> 0.X.Y > ~/ot-cleanup-work/<name>.log 2>&1 &` then
  `timeout 590 tail --pid=<pid> -f /dev/null`. Logs under `~/ot-cleanup-work`, never `/tmp` (it is cleaned).
  No watcher loops, no signals.
- **Waivers are recorded as such.** `MUTATION_RATCHET_PARTIAL_OK=1` (modules whose source changed since the
  last mutation run are NOT MEASURED; list that in the release notes) and `SKIP_VISUAL_BASELINE_FRESHNESS=true`
  are waivers, never passes. `--force-scan "<reason>"` records `overridden`.
- **Evidence-neutral paths** (`evidence_neutral_paths` in `release-criteria.yaml`): a change since a stage's
  recorded sha keeps that evidence fresh only if every changed path is neutral for the stage. All
  `CLAUDE.md` files and `.claude/skills/**` are neutral for all four stages. `docs-site/**` is neutral for
  test/rehearse but NOT for build/scan, because the docs image bakes it in. Scripts, tests, Dockerfiles,
  backend and frontend are never neutral.
- **Stage order for a final cut:** merge everything, push, then `build` and `scan` (or `test` in parallel with
  them), then `rehearse`, then `tag`. Any non-neutral change after a stage means that stage runs again.
- **Feature-check premises are data-driven.** A rehearsal check that exercises a migration is judged only if
  the FROM release's alembic graph does not already contain it (`alembic-head.py --has-revision`); otherwise
  it is recorded `N/A` (counted separately, not a pass). New migration-dependent checks add one line to
  `FC_MIGRATION_OF` in `scripts/release-tests/lib/feature-checks.sh`.

Human-facing explanation and the lessons list: `docs-site/docs/developer-guide/releasing.md` ("Running the
test stage" and "Lessons learned"). Library and flag reference: `scripts/CLAUDE.md`.

## Closing the release

A release is a repo milestone: tidy, then start the next task. Everything here is scoped and
non-destructive by default; anything marked "owner" needs David's word.

1. **git**
   - `gh pr create --base master --head release/prep-X.Y.Z` and merge with a MERGE commit (never squash);
     then `git fetch origin && git merge-base --is-ancestor vX.Y.Z origin/master && echo ok`.
   - `git switch master && git pull --ff-only`.
   - `git branch --merged master` then `git branch -d <name>` per branch (never `-D`).
   - `git worktree list` then `git worktree remove <path>` for merged ones (never `--force`, never another
     session's worktree). Remote branch deletion: owner only.
2. **docker (this daemon is shared)**
   - `./opentr.sh fresh-list`; `./opentr.sh fresh-destroy <name> --yes` for our own `rel-*` stacks.
   - Only our labels: `docker ps -a --filter label=ot-test-artifact=1`, `--filter label=com.docker.compose.project=otfresh-*`
     or `ot-reltest-*`. Never `docker system prune`, `prune -a` or `--volumes`.
   - Check for leftover networks of those projects; then `./opentr.sh start dev` to restore the dev stack.
3. **scratch**
   - `/tmp/ot-redcheck.*`; `~/ot-cleanup-work` logs older than the release (keep what the notes cite);
     old roots under `/mnt/nvm/opentranscribe-test-runs` via the cache purge script (see `scripts/CLAUDE.md`).
   - `.fresh/<name>` leftovers (a seeded `diar-native-models` dir is root-owned: owner removes it).
   - `git status` clean; `git checkout -- security-reports` (the scan rewrites tracked reports).
4. **records**
   - Leave `.release/<version>/` in place. Verify the GitHub release body and assets, record the Docker Hub
     digests (`docker buildx imagetools inspect davidamacey/opentranscribe-backend:vX.Y.Z`), send the cloud
     report, and publish an advisory only after the fixed release is live.
5. **project hygiene**
   - Close the milestone after checking its issues (`gh issue list --milestone vX.Y.Z --state open`); create the
     next milestone; file follow-ups found during the release with a type label, area labels and the Roadmap
     board (CLAUDE.md "Conventions"); update memory; write down what the next release should improve.

### Things this release taught us

Each is recorded once, in its home: NAS-backed volumes, tmpfs vs disk watermarks, the first-run wizard, mock
ports vs the opentr offset, frontend warm-up, stale feature-check premises (`releasing.md` "Lessons learned");
the libs behind them (`scripts/CLAUDE.md`).

## Ordering rules that must not be reordered

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
- **`tag` before `publish`** so CI validates the metadata while the 13.8 GB build
  runs. `tag` also cuts `release/<major>.<minor>` from the tag it just pushed (never
  from `master`) and, on a patch, asserts the tag is an ancestor of that branch.
- **`promote` and `finish` LAST.** The GitHub Release is what the installer
  resolves for "latest". There must never be a window where `releases/latest`
  names a version whose images do not exist.

## Gates

`scripts/release/release-criteria.yaml` declares every gate, its severity, and
which environments enforce it. The local orchestrator and CI read the same file,
so "meets the criteria" means one thing.

A gate can be overridden with `--force-<stage>`, which requires a reason and
records it plus the operator in `.release/<version>/steps/`. Use it for a real
decision (an accepted CVE with no reachable path), never to make a red run green.

**`rehearse` starts with Scenario D, the model downloader from an empty cache**
(`scripts/release-tests/test-cold-model-download.sh`, criteria `cold-cache-*`). v0.6.0
shipped a `download-models.py` whose pyannote group failed for every user, CPU and GPU,
while A-C passed: they install unattended (which skips the download) and seed a warm cache.
D runs each `--only` group CPU-only in the built image and needs `HUGGINGFACE_TOKEN` and the
`build` stage's image; without either it is not-measured (exit 3), not a pass. A red
`cold-cache-model-download` names the failing group; read `groups/<group>.log` under its
TEST_ROOT. Never `--force-rehearse` past it: it is the bug a fresh install hits first.

## What you own

- **CHANGELOG**: `bump` promotes `[Unreleased]` to `[X.Y.Z] - <date>` and opens a
  fresh `[Unreleased]`. Writing the content is yours.
- **Blog post**: `docs-site/blog/YYYY-MM-DD-vX.Y.Z-release.md`. Authors must exist
  in `authors.yml`, tags in `tags.yml`, and the slug must be unique — a duplicate
  slug breaks `npm run build` the moment `draft: true` comes off. `verify` builds
  the docs site for exactly this reason (`deploy-docs.yml` does not run on tags).
  Source material for v0.5.0 is in `docs/releases/drafts/`.

## Before you start

`preflight` checks these, but knowing them saves a cycle:

- **Clean worktree.** A release must be reproducible from its tag.
- **The remote ARM64 builder.** It is a Mac Studio on the LAN and its DHCP lease
  moves. `preflight` prints the stale endpoint and the fix.
- **`HUGGINGFACE_TOKEN`** in `scripts/release-tests/.env.test-secrets`, or both
  rehearsal scenarios fail at their first transcription — hours in.
- **The live stack must be STOPPED for `rehearse` and `smoke`.** The scenarios
  deliberately use the one-liner's stock container names and ports 5173-5180 so they
  exercise what a real user gets. Without approval both refuse (exit 3). `rehearse`
  alone can stop the stack and restart it afterwards when given `--yes`,
  `RELEASE_AUTO_STOP_STACK=true`, or an interactive `y`; `smoke` never stops it.
  A stack that owns the stock ports under any other project (an `otfresh-*` test stack)
  is also exit 3, naming the project and the stop command (`./opentr.sh stop --fresh
  <name>`); `preflight` warns. `scripts/lib/stock-port-owners.sh` is the one definition.
- **`tag` refuses stale evidence.** `artifacts-match-tagged-commit` needs `test`,
  `build`, `scan` and `rehearse` each `done`/`overridden` in the ledger with a recorded
  sha equal to HEAD, or an ancestor of HEAD with only evidence-neutral paths changed since
  (`evidence_neutral_paths` in `release-criteria.yaml`: CHANGELOG, `docs/**`, root `*.md`;
  plus `docs-site/**` and `.release/**` for `test`/`rehearse` only, since the docs image
  bakes `docs-site/` in). So docs written after `bump` do not stale the evidence. Any other
  change makes the stage stale and it is named: re-run it, or `--force-tag "reason"`.
- **The branch under test must be pushed.** `rehearse` downloads the manifest files
  from `TO_BRANCH` (default: the current branch) and exits 3 if it is not on origin.
- **`test` is the opposite:** it needs the dev stack UP (it probes Postgres on 5176).
- **Run `test` on a quiet host.** Timeouts in the e2e and unit phases are load-induced above about
  load 20-30; check `uptime` and pause other heavy jobs first (the v0.6.0 gate lost days to this).
- **`rehearse` uses the images `build` made.** A backend fix needs a rebuild before the rehearsal can
  see it; scripts and compose files come from the pushed branch. For a final cut run
  build -> scan -> test -> rehearse once, on the last commit, with nothing committed in between.

## Related

- `scripts/release-tests/README.md` — harness mechanics and the safety firewall
- `.claude/skills/docker-build-push/SKILL.md` — image build details
- `docs/RELEASE_PROCESS.md` — background and rationale
