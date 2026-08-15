# Handoff — finish the mutation triage and the bugs it finds (issue #446)

**Read this whole file before running anything.** It exists because this harness has produced
four different wrong measurements, and every trap below was hit by someone who had already
read the docs.

Branch: `chore/test-suite-perf-and-quality-overhaul`. Start from a clean tree.
You own commits in this session — commit and push each module separately.

---

## Scope

Four modules remain. **Do them one at a time, in this order**, committing after each:

| # | module | survivors | of | coverage | floor | notes |
|---|---|---|---|---|---|---|
| 1 | `lockout` | **149** | 550 | 85% | 80% | biggest win; ~83 actionable, needs Redis |
| 2 | `dependencies` | **175** | 789 | 94% | 93% | privilege gates; slow (boots the test client per mutant) |
| 3 | `security` | **95** | 289 | 79% | 77% | JWT + bcrypt; slow (a bcrypt round per mutant) |
| 4 | `session` | **73** | 226 | 76% | 76% | zero coverage headroom; needs Redis |

Already done, do not redo: `spans` (10, **all equivalent — at its floor**) and `password_policy`
(86 → 46, 40 real killed). Their test files are the worked examples — read both.

**They cannot run in parallel.** All modules share `backend/mutants/` and `.mutation/`; one
run's `--clean` wipes another's cache. Sequential only.

Expect hours. `lockout` alone is 30–90 minutes of runtime before you read a single diff.

---

## The method (proven twice — follow it exactly)

1. **Measure.** `PATH="$PWD/backend/venv/bin:$PATH" ./scripts/run-mutation-tests.sh --module <name>`
   ⚠️ The log contains a giant single-line progress spinner that will flood your context.
   Always filter: `| grep -vE '⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏|⠋'`
   Survivor ids: `grep '^🙁' .mutation/<name>.log | sed 's/.*\.//' | sort -u`
2. **Read EVERY diff.** `./scripts/run-mutation-tests.sh --show <full-mutant-id>`
3. **Classify each** as `real` / `noise` / `equivalent` (rules below).
4. **Write falsifiable tests** for the real ones.
5. **Register your new test file** in `MODULE_TESTS[<module>]` in `scripts/run-mutation-tests.sh`.
6. **Re-measure.** Lower the baseline in `scripts/mutation-baselines.tsv` to the number you
   measured. Rewrite the row's note with your triage so nobody redoes it.
7. **Commit and push** that module. Then start the next.

`python3 scripts/triage-mutants.py .mutation/<name>.log <name>` can help, but **do not trust
it** — it classified all 10 `spans` mutants as "unclassified". Verify by reading.

---

## Classification rules

**`real`** — a caller can observe the difference. Predicates, constants, dropped arguments, and
**user-facing strings returned in a response body** (not log text). Write a test.

**`noise`** — edits only a log/error string no caller can observe. Do NOT write a test;
asserting on log text produces tests that break on every reword. Note it and move on.

**`equivalent`** — cannot change observable behaviour, so **no test can ever kill it**.

> ### ⚠️ The one rule that keeps this honest
> **An `equivalent` verdict requires a written proof a reviewer can check** — the specific code
> path that makes the two versions identical. If you cannot write that proof, classify it
> **`real` and write a test.**
>
> The dangerous direction is calling a real mutant equivalent. These are auth modules; a missed
> real mutant is a removable security control. Extra tests cost minutes, a dismissed one costs
> a vulnerability.

Valid equivalence proofs, from the finished `spans` pass:
- `char_start > cursor` → `>=` — at equality the branch appends `text[cursor:cursor]`, the empty
  string, so the joined output is byte-identical.
- `style = "label"` → `None` — `_placeholder` dispatches on `asterisks`/`first_letter`/`blur` and
  **falls through to the label form for anything else**, including `None`.
- `not text or not spans` → `and` — the longer path reaches `if not applied: return text, []`,
  the same answer.

---

## When triage finds a PRODUCTION BUG

It will. The same work on `app/tasks/` turned up 11 real defects, four of which are now fixed
(#455–#458). A surviving mutant on a live predicate often means nobody ever asserted that
behaviour — and sometimes the behaviour is wrong.

The loop that worked:

1. **Pin it first, with a characterization test** whose docstring says it pins WRONG behaviour
   and names its replacement. Shape: `backend/tests/unit/test_chunking_service.py`.
2. **File a GitHub issue** with the file:line, the failure scenario, and the suggested fix.
   `gh issue create --label bug`.
3. **Then fix the production code**, flip the characterization test to assert the fix, and keep
   the reasoning in its docstring so the defect stays legible.
4. Commit the fix separately from the triage.

> ### ⚠️ Green characterization tests do NOT prove a fix landed
> This bit hard on #456. A fix was written, the tests stayed green — because the fix was a
> **silent no-op** (an `elif` swallowed the block, so the code path never ran). A no-op leaves
> the pinned wrong behaviour intact, which is exactly what a characterization test asserts.
>
> After any fix: **call the function directly and print the result.** Do not trust green.

Also from that round: a fix can *drop* a guarantee. #456's fix removed an implicit
"`text` always exists" contract that `storage.py` depends on; the suite caught it. Run the
module's whole suite, not just the tests you touched.

---

## Traps. Every one has already bitten someone who had read the docs.

1. **A new test file must be added to `MODULE_TESTS[<module>]`** or it is never selected and
   **every mutant still "survives"** — indistinguishable from your tests being useless.
   *Confirm by watching the coverage percentage move.* (Hit on `spans`.)
2. **NEVER run `--verify` while anything else might be writing.** It transiently edits **live
   source** in `backend/app/` and holds a per-module `flock`; a SIGKILL leaves a mutation in the
   tree. A plain `--module` run is safe — it only touches the gitignored `backend/mutants/`.
   The safe alternative, used successfully on `password_policy`: pull the mutant function out of
   `backend/mutants/` and monkeypatch it **in memory** for one test run.
3. **Validate your own verification harness against known negatives first.** The
   `password_policy` harness initially reported **KILLED for the unmutated original** — a
   `PYTHONPATH=/tmp` let a stray `/tmp/token.py` shadow the stdlib. A harness claiming
   everything is killed looks identical to success. Before trusting a KILLED, confirm the
   unmutated original and 2–3 known-equivalent mutants all report SURVIVED.
4. **Never run a Python script from `/tmp`** — `/tmp/token.py` exists on this host and shadows
   the stdlib `token` module. Use a subdirectory.
5. **Do not predict killability from a diff.** On `spans`, 7 of 10 were judged killable, the
   tests were written, and the count did not move. All 10 were equivalent. Measure.
6. **`--clean` between modules** if the progress counter looks cumulative (206 → 498 → 820) —
   the cross-check correctly refuses a log that is not one run.
7. **NEVER raise a baseline** to make a run pass. Down is progress; up means a predicate lost
   its test, and that is a finding about your own change.
8. **`rg -r` is `--replace`.** `rg -rn 'foo'` silently replaces matches with `n` and corrupts
   your output. This was hit three times in one session. Use `grep -rn` or `rg -n`.

---

## Rules of engagement

- **Never `pre-commit run --all-files`** unless the tree is quiet. It stashes every unstaged
  change in the repo before any hook runs, regardless of what is staged. Nothing else should be
  writing during this session — if something is, do not commit at all. Use
  `scripts/safe-precommit.sh run --all-files` (issue #434) instead of the bare command — it
  refuses to start rather than race when another pre-commit run or a `--verify` mutation run is
  already in flight, which is exactly the overlap a `--verify` batch in this same session risks.
- Hooks see the **staged** snapshot: `git add`, then further edits, means mypy/ruff check the
  stale copy and report errors you already fixed. Re-`git add` before committing.
- Never `--no-verify`. If a hook fails, fix the finding. If a detector fires on a new test,
  restructure the test rather than allowlisting it — a `mock-heavy` finding this session was a
  fair reading and the test was split instead.
- Do not run `pkill`/`killall`/`kill -9`/GPU resets. **GPU 1 is this project's only GPU; 0 and 2
  are reserved.** Mutation runs are CPU-only but saturate every core — don't start one beside a
  benchmark.
- Never read or print `.env`, keys, certs or tokens.
- Stack ops go through `./opentr.sh`, never bare `docker compose`.
- Conventional commits: `<type>(<scope>): <summary>`. Explain WHY in the body, and record what
  you got wrong on the way — that is the most useful part of the message.

---

## Test quality bar

Read `backend/tests/CLAUDE.md` first. `python3 scripts/audit-tests.py backend/tests` must report
no un-allowlisted findings, and `--selftest` must pass.

- Never `assert x != 403` — a 500 satisfies it.
- Never put the real assertion inside `if status == 200:`.
- A loop over a runtime iterable needs a non-emptiness assertion **outside** the loop.
- `mock.assert_called_once_with` alone is not enough — assert real state.
- Pair every "it fires" test with a **control** proving it does not fire otherwise. Asserting
  only the positive passes for a function that returns True unconditionally.
- **Watch each test fail before it passes**, and say so. A test never seen failing is not
  evidence.

---

## Per-module notes

**`lockout` (149) — start here.** Its baseline note records ~58 mutants editing a log string and
~8 flipping a condition that guards only a log call, so the actionable set is roughly 83 —
re-derive that from the diffs rather than trusting it. Needs Redis (localhost:5177). It has two
near-duplicate code paths (Redis-backed and in-memory); a diff can look like it belongs to the
wrong one, which is how a survivor was once misreported as already-tested. Check which path a
diff sits in before reasoning about it.

**`dependencies` (175)** — the privilege gates. An inverted role comparison here **is** privilege
escalation, so bias toward `real`. History worth knowing: an incomplete `MODULE_TESTS` list once
produced **41 false survivors** here, reported as a proxy header-spoofing vulnerability before
anyone checked. Each mutant boots the test client, so this is the slowest module.

**`security` (95)** — JWT + bcrypt; every mutant pays a bcrypt round. Its coverage floor (77%) is
the lowest, so check whether low coverage is inflating the survivor count before concluding the
tests are weak.

**`session` (73)** — session lifetime. Coverage 76% against a 76% floor: **no headroom**, so any
test you add should raise it. Needs Redis.

---

## Definition of done, per module

1. Every survivor classified, with equivalence proofs written down.
2. Tests added for all `real` ones, each **observed failing first**.
3. New test file registered in `MODULE_TESTS`; coverage moved (that is the proof it registered).
4. Re-measured; baseline lowered to the measured number; note rewritten.
5. `./scripts/run-mutation-tests.sh --check-baseline` still reports `checked 6/6`.
6. `./scripts/run-backend-tests.sh` green (baseline: **7,143 passed / 0 failed**).
7. `audit-tests` clean, `--selftest` passing, ruff + mypy clean.
8. `git diff backend/app/` is **empty** unless you deliberately fixed a bug — confirm no
   mutation was left in live source.
9. Committed and pushed.
10. A comment on issue #446 with the counts, the real findings, and anything you could not kill
    and why.

## Definition of done, overall

All four modules triaged, baselines lowered to measured numbers, `checked 6/6` holding, and #446
updated with the final per-module split. Note in that comment that the issue's title figure
("~275 of 636 are real gaps") does **not** extrapolate — measured so far is 0/10 in `spans` and
40/86 in `password_policy`. Report the real per-module numbers instead.

---

## Context you may want

- `backend/tests/CLAUDE.md` — mutation section, markers, gates
- `scripts/CLAUDE.md` — `run-mutation-tests.sh`'s four historical wrong measurements
- `backend/tests/redaction/test_apply_redactions_mutants.py` — worked example: an
  all-equivalent module, and how to write the proofs
- `backend/tests/unit/test_password_policy_mutants.py` — worked example: a large real set
- `.claude/AUDIT-STATE-AND-NEXT.md` — where the wider audit stands
- Issue #446 — running commentary from the two finished modules
