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
./scripts/release.sh test                # the existing pre-merge gate
./scripts/release.sh build               # local images, nothing published
./scripts/release.sh scan                # scans the LOCAL images
./opentr.sh stop                         # rehearse needs the live stack down
./scripts/release.sh rehearse            # fresh-install + upgrade scenarios
./scripts/release.sh tag --yes           # ← first step that leaves this machine
./scripts/release.sh publish --yes       # :vX.Y.Z only, never :latest
./scripts/release.sh smoke               # install from Hub; check both arches
./scripts/release.sh promote --yes       # move :latest by digest
./scripts/release.sh finish --yes        # GitHub release + assets
```

`run` does the whole sequence: `./scripts/release.sh run 0.5.0 --skip scan`.
`--dry-run` prints without executing. `--from <stage>` resumes. `--json` on any
stage gives machine-readable criteria and an explicit `next[]`.

## Ordering rules that must not be reordered

- **`build` → `scan` → `rehearse` → `publish`.** Validated bytes reach Docker Hub
  only after the scenarios pass, because `:latest` is what every existing user
  pulls.
- **`tag` before `publish`** so CI validates the metadata while the 13.8 GB build
  runs.
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
- **The live stack must be STOPPED for `rehearse`.** The scenarios deliberately
  use the one-liner's stock container names and ports 5173-5180 so they exercise
  what a real user gets. `release.sh` never stops it for you.

## Related

- `scripts/release-tests/README.md` — harness mechanics and the safety firewall
- `.claude/skills/docker-build-push/SKILL.md` — image build details
- `docs/RELEASE_PROCESS.md` — background and rationale
