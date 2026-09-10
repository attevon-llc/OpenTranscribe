---
name: gate-hardening
description: Fix, verify, or speed up the dev gate, release pipeline, or release rehearsal. Use when the user says the tests are wrong/slow/flaky, when a gate phase reports a result you cannot attribute, when adding a test that must not be able to pass vacuously, or when coordinating multiple agents editing this checkout. Covers run-dev-tests.sh, run-integration-tests.sh, run-e2e.sh, test-matrix.sh, release.sh and the rehearsal scripts.
---

# Gate hardening

Three things must work — **properly, correctly, completely.** Not one, not two:

1. **Dev gate** — `./scripts/run-dev-tests.sh --full`
2. **Release pipeline** — `./scripts/release.sh`
3. **Release rehearsal** — fresh-install and upgrade builds and tests

There are ~10 releases queued and a **development team of one**. Every one of those
releases needs all three to be trustworthy, so a gate the owner has to argue with is
worse than no gate — it gets trusted, then shipped around.

## What "working" means, in priority order

1. **The test is the RIGHT test.** It would actually fail if the code were wrong.
2. **The test ACTUALLY RUNS.** Not silently skipped, deselected, gated off by a stale
   env var, or served from a cached artifact.
3. **The verdict is HONEST.** Green ⇒ measured. NOT MEASURED is a distinct, loud
   outcome that can never be mistaken for green.
4. **Speed — but only as waste removal.** See the table below.

**A green result that did not measure anything is the failure this whole skill exists
to prevent.** It is worse than a red one, because a red run makes you look.

## Speed: the distinction that decides every optimisation

The owner's rule: *"speed and accuracy are both the goals to get fast scripts but not
at the expense of bad test."* So classify every proposed speedup before doing it:

| Waste removal — **DO IT** | Coverage removal — **NEVER** |
|---|---|
| Same tests, same assertions, less overhead | Skipping, deselecting, or narrowing a phase |
| Keycloak re-running Quarkus augmentation every boot (600s→25s) | Raising a skip ceiling to fit a time budget |
| 918 MB of node_modules staged into every rehearsal tree | Loosening an assertion so a phase finishes |
| An 890 MB docker build context (→22 kB) | Dropping a "redundant" test |
| 48 xdist workers each collecting the whole tree | Reducing what a marker selects |

**1.5 hours doing real work is fine. 15 minutes that misses a required test is waste.**

**Mandatory proof for any speed change:** capture the sorted list of collected test node
IDs before and after and `diff` them. The diff must be empty. A speedup with an unproven
test set is a coverage regression wearing a performance badge.

## The failure modes this repo has actually shipped

Check for these first — every one of them reported green:

| Symptom | Real cause |
|---|---|
| Phase passes in seconds | It read a stale cached junit XML and never ran pytest |
| Suite "passes" with N skipped | No `-rs`, no `--junitxml`, no skip ceiling — nothing counts skips |
| A test always skips | It never requested the `token` fixture → 401 → empty list → skip |
| A marker's phase passes | The marker selects **zero** tests |
| A security suite passes | 240 tests gated off behind an env var nothing sets |
| A test cannot fail | `except Exception: pytest.skip` around the assertion |
| Suite proves less on CI than locally | It reads whatever media is in the developer's dev library |
| A GPU stack runs on CPU | `docker info \| grep -q` + `pipefail`: `grep -q` exits early, `docker info` dies of SIGPIPE, 141 propagates, **a match reads as a non-match** (~1 in 200–600) |

**The `pipefail` + early-exiting-consumer hazard is a repo-wide class**, not one call
site. Any `set -o pipefail` script piping a chatty producer into `grep -q` / `head -1` /
`grep -m1` can invert or abort. Capture into a variable and substring-match instead.
`scripts/lib/compose-project.sh` uses a bash loop for the sibling reason (a no-match
`grep -v` exits 1 and aborts the caller).

## Hard rules

- **No test may depend on data in a developer's dev deployment.** Tests create their own
  data and delete it in teardown. The defect is not that tests skip — it is that *the
  same code proves different amounts on different machines*. Precedent to follow:
  `backend/tests/fixtures/search_corpus.py` self-seeds.
- **Never silence a finding.** No `noqa`, `type: ignore`, allowlist entry, or widening to
  `Any` to quiet a real mismatch. Ask: would this change still be an improvement if the
  linter were deleted tomorrow?
- **Never raise a mutation baseline** to make a run pass.
- **A skip ceiling is a floor to drive DOWN, not a target to sit at.** Every remaining
  skip needs a written, justified reason.
- **Watch every new test FAIL first.** Use `git archive HEAD` into a temp dir — never
  revert files in the shared checkout (it costs backend hot-reloads, races other writers,
  and leaves your fix off disk inside a stash window).
- **Profile before theorising.** `python -m cProfile -o out.prof -m pytest <test>`. Two
  plausible hypotheses once cost two full measurement cycles on one bug.
- **Run every timing ≥2×** and report load average. This host shows 21–28s of
  run-to-run noise — enough to manufacture a fake improvement from nothing.

## Multi-writer discipline — the rule that has cost the most

> **NEVER run `pre-commit` OR `git commit` while anything else is writing to this
> checkout.** All three of `--all-files`, `--files <paths>`, and a plain `git commit`
> **stash every unstaged change in the entire repo**. The stash happens *before any hook
> runs*. What you staged is irrelevant.

**Disjoint files do NOT make it safe. There is no "they're my own subagents and I scoped
the commit" exception.** Observed here: an agent fanned out 8 background subagents, then
ran `git commit -- <2 files>` while 7 were mid-edit — which would have stashed all 7.

### ⛔ The rule is about CONCURRENCY, not about who the writer is

**Exactly one writer may be active in a checkout when a commit or pre-commit runs.**
That is the whole rule. Who runs it does not matter:

| Shape | Commit? | Why |
|---|---|---|
| A **lone** subagent, sole writer in the checkout | ✅ yes | Nothing else is in flight to stash |
| An agent in **its own worktree** | ✅ yes | Separate index and stash; no cross-lane exposure |
| The orchestrator, after **every** lane has reported | ✅ yes | Tree is quiet |
| **N>1 writers on one branch**, each committing | ⛔ **never** | Each one's run stashes the other N−1 mid-edit |

The last row is the one that has cost real work here, repeatedly. Four agents in one
checkout produced four destroyed work sets, ~25-minute commit blocks, and a patch file in
`~/.cache/pre-commit/` every two minutes. Serialising cost nothing and every lane landed
within the hour.

So when you fan out N>1 writers onto one branch:

- **Forbid every lane from `git add` / `commit` / `stash` / `pre-commit`** in its brief.
- **Do not reason that scoping a pathspec makes it safe.** It does not — the stash is
  whole-tree and happens before any hook runs.
- **Do not commit a lane early because it looks "obviously done."** Every other lane
  still writing is exposed the moment you do.
- **Worktrees are a legitimate mechanism** for letting agents self-commit — but this
  repo's product work stays on **one branch off master, not in a worktree** (owner's
  standing instruction), so on this branch the answer is serialise, not fan out into
  worktrees. ⚠️ Never run `pre-commit install` from a worktree: `.git/hooks` there
  resolves to the shared common git dir, so it rewrites the hooks of the main checkout
  and every other worktree at once.

`CLAUDE.md` previously recommended `--files` / "just commit" as the safe alternative.
**That advice was wrong and caused the incident it now documents.** There is no safe
alternative for a tree with work in progress; there is only waiting for a quiet tree.
Four agents in one checkout produced four destroyed work sets, ~25-minute commit blocks,
and a patch file in `~/.cache/pre-commit/` every two minutes before this was adopted.
Serialising cost nothing and every lane landed within the hour.

So when running parallel agents:
- Give each lane an **explicit, disjoint file list**, and name the files other lanes own.
- Forbid every agent from `git add` / `commit` / `stash` / `pre-commit`.
- **The orchestrator commits, once all lanes report** — one commit per lane, via
  `git commit -- <explicit paths>`, so history keeps per-lane attribution.
- Read-only recon agents are always safe to run alongside writers.

**A test run is also a writer's victim.** Never let a gate run overlap agent edits: an
e2e phase run against a mutating tree produced 20 meaningless failures, 17 of them
fixture `ERROR`s. Confirm zero writers and a clean `git status` *before* starting a run.

**When a run is your evidence, check it ran YOUR code** — new test names present in the
output, or a deliberate failure you expect to see. A suite once reported `13 passed`
while the tree was stashed, having tested the previously committed code.

## Order of operations

1. All lanes report → confirm no agents and no stray background shells
2. Commit per lane, both pre-commit tiers
   (`scripts/safe-precommit.sh run --all-files` **and** `--hook-stage pre-push`)
3. `git status` clean and quiet
4. Full dev gate — the only thing touching the checkout
5. Release pipeline: `preflight`, `run --dry-run`, then in-repo stages
   ⚠️ `tag` / `publish` / `promote` / `finish` reach Docker Hub and GitHub — **explicit
   owner go-ahead required**, never run to "check they work"
6. Rehearsal **last** — it requires the live stack **stopped**

Report what was **measured**, not just pass/fail. Never report a phase green without
saying what it verified.
