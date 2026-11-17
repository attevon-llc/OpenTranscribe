# v0.7.0 release runbook

What happens after the code is final: verification, rehearsal, and the public artifacts. A fresh agent must
be able to run it standalone. The release is cut ONLY through `./scripts/release.sh` (never hand-run
`git tag`, `docker push` or `gh release`); `tag`, `publish`, `promote` and `finish` need `--yes` and the
owner's go. Companion: `v060_release_runbook.md` (the same flow for 0.6.0),
`docs-site/docs/developer-guide/releasing.md`, `.claude/skills/release/SKILL.md`.

## Branch and checkout
- `release/v0.7.0` is the prep branch. It is already pushed (the rehearsal scenarios that install from a
  manifest download it from `origin`, and `rehearse` exits 3 without it). Release commits (bump, CHANGELOG,
  docs, the legal documents) are normal conventional commits on this branch; push after each batch.
- **Run everything from the main checkout, on this branch.** No release worktree, no file copying: the
  gitignored files `preflight` needs (`nginx/ssl/server.crt` + `server.key`,
  `scripts/release-tests/.env.test-secrets`, `.env`) live there.
- Master is protected, so the bump commit lands via this branch. After `tag`, open the PR
  `release/v0.7.0` -> `master` as a MERGE commit (never squash), then check
  `git merge-base --is-ancestor v0.7.0 origin/master`.
- The image that is built, scanned and rehearsed must be the exact commit that is tagged: tag the branch
  head, never a later merge commit.

## Preconditions
1. The legal documents are vendored under `backend/app/legal/documents/` with no placeholders left, and
   the manifest hashes are refreshed. Run the legal unit tests, `scripts/lane-check.sh`, the two
   pre-push guards and the openapi snapshot check.
2. Host dependencies match this branch: `backend/venv` (two-step install from `requirements.txt` then
   `--no-deps -r requirements-nodeps.txt`, then `requirements-dev.txt`), `npm ci` in `frontend/` and
   `docs-site/`.
3. The live dev stack is stopped for `rehearse` and `smoke` (`./opentr.sh stop`); `test` needs it UP.
   Only one OpenTranscribe stack runs at a time on GPU 1.
4. A quiet host slot (about 5 to 6 hours): other heavy jobs on the machine are paused first.
5. `scripts/safe-precommit.sh run --all-files` and `... --hook-stage pre-push` are green on the head.

## Stages (each independently runnable and resumable; see `release.sh explain <stage>`)
```
./scripts/release.sh reset 0.7.0         # clear rehearsal history before a REAL run
./scripts/release.sh preflight 0.7.0     # seconds, fails fast
./scripts/release.sh bump 0.7.0          # makes the version-bump commit; do not edit VERSION by hand
./scripts/release.sh verify 0.7.0
./scripts/release.sh test 0.7.0          # dev stack UP; full suites, e2e and the browser journeys
./scripts/release.sh build 0.7.0         # backend, backend-lite, frontend, docs
./scripts/release.sh scan 0.7.0          # an override needs --force-scan "reason" and is ledgered
./scripts/release.sh rehearse 0.7.0 --yes   # D (model download), fresh install, upgrade, lite
```
Then, on the same rehearsal images and back to back: the mutation verify for `lockout_admin` (#1373), alert
fault injection per rule (#791), a load run with the sizing numbers (#1174), the query re-measure (#1337).
The linux/arm64 legs build on the remote arm64 builder (`USE_REMOTE_BUILDER=true`); without it they run
under QEMU.

Each of `tag`, `publish`, `smoke` (stack stopped), `promote` and `finish` is a separate go from the owner.
Never chain them with `run --yes`. A red gate is investigated, never just re-run.

## Pull request
The PR body lists one `Closes #N` per line (a comma list closes only the first issue). Verify issue state
and milestone after the merge.
