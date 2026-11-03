# Pre-commit, stash windows and concurrent writers

Moved intact from the root `CLAUDE.md`. The rule itself and the shape table stay in the root file: **never run `pre-commit` or `git commit` while another writer is active in the checkout** (pre-commit stashes the whole tree).

>
> This paragraph used to recommend `--files` or "just commit" as the safe alternative. **That
> advice was wrong and caused the incident below.** There is no safe alternative for a tree with
> unrelated unstaged work in progress; there is only waiting for a quiet tree. The wrapper above
> catches the two overlaps that have actually bitten this repo — a second pre-commit run, and a
> mutation `--verify` mutation left live — it is not a general fix for the hazard in this box.
>
> **"Anything else" includes background subagents you yourself just dispatched in this same
> turn.** An agent fanned out 8 parallel background subagents to edit disjoint files on one
> branch, then ran `git commit -- <2 files it had already verified itself>` while the other 7
> were still mid-edit — reasoning that an explicit pathspec made it safe. It does not: `git
> commit` stashes the whole tree regardless of pathspec, which would have stashed all 7
> in-flight agents' work the moment it ran. There is no "but these are my own subagents and I'm
> scoping the commit" exception. Wait for every dispatched writer to report done, review, THEN
> run one clean commit/precommit pass.
>
> ⚠️ **A test run is a writer's victim too, and the damage is silent.** Never start a gate, a
> suite, or a timing measurement while any writer is active. An e2e phase run against a mutating
> tree produced 20 meaningless failures — 17 of them fixture `ERROR`s, the signature of a
> `conftest.py` changing mid-suite — and they were nearly filed as real defects. Contention also
> invalidates *timings*: two agents running `pytest --collect-only` at once on this 48-core host
> makes both numbers unusable, and comparing a loaded "before" against a quiet "after"
> manufactures an improvement out of nothing. Confirm zero writers **and** a clean `git status`
> before any run whose output you intend to treat as evidence.
>
> Three failure modes, all observed here:
>
> 1. **Another writer's work is stashed mid-edit.** An agent's `Edit` failed with "file has been
>    modified", it re-read and re-applied, and the restore then reinstated the *earlier* draft,
>    silently discarding the newer one. Caught only by an unrelated `git diff`. Earlier the same
>    day, a different agent's `conftest.py` was stashed during a failing commit and restored with
>    `Stashed changes conflicted with hook auto-fixes... Rolling back fixes` — recovered, but by
>    luck. If a hook crashes in that window the only copy is
>    `~/.cache/pre-commit/patch<timestamp>-<pid>`.
> 2. **A whole-tree hook fails against a tree that never existed.** `frontend-check` scans all of
>    `frontend/src`, so with an unstaged type change stashed away and an untracked test file left
>    behind, svelte-check failed with 5 errors about a property that *does* exist. Nothing was
>    wrong. The natural response — "fixing" correct code — makes it worse.
> 3. **Spurious `files were modified by this hook`** with no findings at all (bandit printing
>    "No issues identified", frontend-check printing "All frontend checks passed"). The
>    stash/restore moved the files, not the hook.
>
> A related trap that is *not* about concurrency: pre-commit's hooks see the **staged** snapshot.
> If you `git add` a file and then edit it further, mypy/ruff check the stale staged copy and
> report errors you have already fixed. Re-`git add` before committing.
>
> `--all-files` is always correct in CI, where nothing else is writing.

Hook inventory is in `.pre-commit-config.yaml`. **The mypy hook runs in its own isolated environment, so it only
sees the packages listed in its `additional_dependencies`** — a `py.typed` package missing from that list is `Any`
there and the hook goes green on code `backend/venv/bin/mypy` rejects (#1271: 0 findings from the hook, 55 from the
venv, same 2,269 files, found only by comparing them). Its `rev` and every dependency are therefore exact pins that
must equal `backend/requirements*.txt`, and `backend/tests/unit/test_precommit_mypy_env_parity.py` derives the rest
(every typed import under `backend/` must be a hook dependency or sit in that test's `EXEMPT` table with a reason:
torch, transformers and friends are checked by the venv only). Bumping a package means changing it in
`backend/requirements*.txt` **and** the hook in the same commit. `scripts/mypy-hook-negative-control.sh [--ref REF]`
runs the real hook on deliberately mistyped code in a throwaway repo and must report `caught 3/3`; CI runs it. The
frontend hooks (`frontend-check` at commit stage, `frontend-build` at push stage) only fire when `frontend/src/**/*.{svelte,ts,js,css,html}` is staged. Note that `prettier` **rewrites files** and then reports failure — re-stage and re-run, don't "fix" anything by hand.

- **More than one writer in a checkout? ONE of them commits.** Everyone else hands over exact
  paths plus a commit message. This is not style — pre-commit **stashes the entire worktree** on
  every run (issue #434), so N writers each running their own commit-retry loop means each one's
  hook run is what makes the other N−1 fail with `files were modified by this hook` while the
  hook itself reports no findings. Four agents in one checkout produced four destroyed work
  sets, ~25-minute commit blocks, and a patch file in `~/.cache/pre-commit/` every two minutes
  before this rule was adopted. Serialising cost nothing and every lane landed within the hour.
- **Inside a stash window, your uncommitted work is simply GONE from disk — and the failure can
  present as your own code having never been written.** Three symptoms of the same cause, all
  observed here: the phantom `files were modified by this hook`; the E2E backend flapping; and
  a module reverting to its last-committed version, so a test that passed sixty seconds ago
  fails with `ImportError: cannot import name '<the thing you just wrote>'`. Someone nearly
  spent an afternoon on a non-existent import cycle. **Before debugging a sudden impossible
  failure, check whether another writer is running pre-commit.**
  ⚠️ **And do NOT back up your files during that window — you will back up the stash.** Copying
  to a safe directory mid-stash captures the *reverted* content, and "restoring" from it
  destroys the work for real. Wait for the restore, verify the file actually contains your
  change, and only then copy. This was caught once by diffing the backup against the live file
  instead of trusting the copy.
  ☠️ **The worst variant is a GREEN run: a test suite can pass having tested the OLD code.**
  Observed — a suite reported `13 passed` while the tree was stashed, having imported the
  committed versions of both the source and the test file. A minute later the same command
  reported `2 failed, 11 passed`, because that run caught the app file restored and the test
  file not. **Neither number meant anything**, and the only tell was that the *test names* in
  the output did not match the names just written. A red run makes you look; a green one gets
  recorded as evidence and you move on. So: **when a run is your evidence, check it ran YOUR
  code** — new test names present, a marker string in the output, or a deliberate failure you
  expect to see. A pass you cannot attribute is not a measurement.
- **To watch a test fail against the OLD code, use a `git archive HEAD` tree — never swap files
  in the shared checkout.** The repo's standard is that a test you have not seen red is not
  evidence, so this is done often. Reverting a file in place and putting it back costs two
  backend hot-reloads (each dispatching `search_index_maintenance`), leaves the fix off disk in
  a window where a stash can capture the reverted state, and races every other writer:

  ```bash
  RED="$(mktemp -d -t ot-redcheck.XXXXXX)" && git archive HEAD | tar -x -C "$RED"
  cp backend/tests/.../test_the_new_one.py "$RED/backend/tests/.../"   # new tests, old source
  cd "$RED/backend" && <run them; expect red>
  ```

  Immune to stash windows, costs no reloads, disturbs nobody, and the tree you are testing is
  provably HEAD rather than "what I think I reverted". Use that `ot-redcheck.` name: hand-named
  `/tmp/redcheck*` trees reached **4.1 GB** on this host, and `scripts/cleanup-test-data.py`'s
  tmp plane (`./opentr.sh test-cleanup`) removes only registered shapes (issue #1195).
- **`audit-tests` is a WHOLE-TREE gate, so an unfinished test file blocks everyone.** One
  in-progress test anywhere under `backend/tests` with an open finding refuses **every** commit
  in the worktree, including commits that do not touch it — twice in one day a lane's own
  next-unit test file was refusing its own finished work. Before reporting a unit ready, run
  `cd backend && python3 ../scripts/audit-tests.py tests` and get to 0 open findings. And do not
  assume a hook failure is the stash bug: **read the finding first.** Roughly half of the ones
  blamed on contention were real.
- **Commit with an explicit pathspec, and remember staging is not protection.**
  `git commit -- <paths>` takes **worktree** content for those paths and leaves everyone else's
  staged work alone; a bare `git commit` in a shared index sweeps up whatever others have staged.
  Pass files, not a directory — a directory pathspec silently swept in two untested modules that
  happened to live beside the finished ones. Note `git commit -- <path>` fails on an **untracked**
  file: `git add` it first, then commit with the pathspec.
