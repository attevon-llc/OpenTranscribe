# Where files live (every agent, every session)

Decide what a file is the moment you create it. Nothing goes in `~`, `/tmp` (not durable), or a sibling folder
of the repo.

| Kind | Home | Lifetime |
|---|---|---|
| Worktree | `.claude/worktrees/<name>` (git-ignored) | Removed in the same step that merges the lane |
| Scratch: one-off scripts, logs, run output, verification copies (`git archive` trees) | `.claude/tmp/` (git-ignored) | Deleted when the task ends |
| Private notes and private tooling (names, vendors, legal or security detail) | `.claude/work/` (git-ignored); one live `HANDOFF.md` and a `README.md` saying where to start | Dated; deleted once superseded or merged |
| Public notes | `docs/` | Kept |
| Generic reusable tooling (a runner, a gate, an importer, a checker) | `scripts/`, committed, with a header, `--help`, and a test if it gates anything | Kept; it is code |

Rules of thumb:
- Reusable and generic means code: commit it. Not generic yet (hard-coded paths, private rules) means it stays in
  `.claude/work/tools/` until it is scrubbed and generalised; do not leave it in a scratch folder.
- Before deleting any scratch folder, copy out unique results and notes first. A delete-by-size pass once nearly
  lost the query-review perf results this way.
- Close every task and lane with a cleanup pass and report it in one line: `git worktree list` shows only the
  main checkout, no merged branches remain, no verification copies are left, stale notes are gone, and generic
  tooling has been promoted to `scripts/`.
- Large artifacts belong on `/mnt/nvm`, not the 480 GB root disk, and a Docker bind mount must point at a path that
  exists (a missing source creates empty root-owned folders in `/`).
