# Handoff — finish the mutation-survivor triage (issue #446)

**Read this whole file before running anything.** It exists because this harness has produced
four different wrong measurements, and every trap below was hit by someone who had already
read the docs.

Branch: `chore/test-suite-perf-and-quality-overhaul`. Start from a clean tree.

---

## State as of handoff (verified, `d4af4df8`)

| module | survivors | of | coverage | floor | status |
|---|---|---|---|---|---|
| `spans` | 10 | 206 | 98% | 95% | **DONE — all 10 equivalent, at its floor** |
| `password_policy` | 46 | 292 | 100% | 94% | **DONE — 40 real killed** |
| `lockout` | **149** | 550 | 85% | 80% | TODO |
| `dependencies` | **175** | 789 | 94% | 93% | TODO |
| `security` | **95** | 289 | 79% | 77% | TODO |
| `session` | **73** | 226 | 76% | 76% | TODO |

`./scripts/run-mutation-tests.sh --check-baseline` reports `checked 6/6`. Keep it that way.

**Do ONE module per session.** `lockout` alone is 30–90 minutes of runtime. Each module is an
independent, committable unit.

---

## The method (proven twice — follow it exactly)

1. **Measure.** `PATH="$PWD/backend/venv/bin:$PATH" ./scripts/run-mutation-tests.sh --module <name>`
   ⚠️ The log contains a giant single-line progress spinner that will flood your context.
   Always filter: `| grep -vE '⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏|⠋'`
   Survivor ids are the lines beginning `🙁`:
   `grep '^🙁' .mutation/<name>.log | sed 's/.*\.//' | sort -u`
2. **Read EVERY diff.** `./scripts/run-mutation-tests.sh --show <full-mutant-id>`
3. **Classify each** into `real` / `noise` / `equivalent` — see the rules below.
4. **Write falsifiable tests** for the real ones.
5. **Register the new test file** in `MODULE_TESTS[<module>]` in `scripts/run-mutation-tests.sh`.
6. **Re-measure**, then lower the baseline in `scripts/mutation-baselines.tsv` to the number
   you measured.
7. Update the row's note with the triage so the next person doesn't redo it.

`python3 scripts/triage-mutants.py .mutation/<name>.log <name>` can help, but **do not trust
it** — it classified all 10 `spans` mutants as "unclassified". Verify by reading.

---

## Classification rules

**`real`** — a caller can observe the difference. Predicates, constants, dropped arguments,
and **user-facing strings returned in a response body** (not log text). Write a test.

**`noise`** — edits only a log or error string no caller can observe. Do NOT write a test;
asserting on log text produces tests that break on every reword. Note it and move on.

**`equivalent`** — cannot change observable behaviour, so **no test can ever kill it**.

> ### ⚠️ The one rule that makes this safe
> **An `equivalent` verdict requires a written proof a reviewer can check** — the specific
> code path that makes the two versions identical. If you cannot write that proof, classify
> it **`real` and write a test.**
>
> The dangerous direction is calling a real mutant equivalent: these are auth modules, and a
> missed real mutant is a removable security control. Extra tests cost minutes; a dismissed
> one costs a vulnerability.

Worked examples of valid equivalence proofs, from `spans`:
- `char_start > cursor` → `>=` — at equality the branch appends `text[cursor:cursor]`, the
  empty string, so the joined output is byte-identical.
- `style = "label"` → `None` — `_placeholder` dispatches on `asterisks`/`first_letter`/`blur`
  and **falls through to the label form for anything else**, including `None`.
- `not text or not spans` → `and` — the longer path reaches
  `if not applied: return text, []`, the same answer.

---

## Traps. Every one of these has already bitten someone.

1. **A new test file must be added to `MODULE_TESTS[<module>]`** or it is never selected, and
   **every mutant still "survives"** — which looks exactly like your tests being useless.
   *Confirm registration by watching the coverage percentage move.* (Hit on `spans`.)
2. **NEVER run `--verify` if anything else might be writing.** It transiently edits **live
   source** in `backend/app/` and holds a per-module `flock`; a SIGKILL leaves a mutation in
   the tree. A plain `--module` run is safe — it only touches the gitignored `backend/mutants/`.
   The safe alternative, used successfully on `password_policy`: pull the mutant function from
   `backend/mutants/` and monkeypatch it **in memory** for one test run.
3. **Validate your own verification harness against known negatives first.** The
   `password_policy` harness initially reported **KILLED for the unmutated original** — a
   `PYTHONPATH=/tmp` let a stray `/tmp/token.py` shadow the stdlib. A harness claiming
   everything is killed looks identical to success. Run it against the unmutated original and
   against 2–3 known-equivalent mutants; all must report SURVIVED before you trust a KILLED.
4. **Never run a Python script from `/tmp`** — `/tmp/token.py` exists on this host and shadows
   the stdlib `token` module. Put scratch scripts in a subdirectory.
5. **Do not predict killability from a diff.** On `spans` I read the diffs, judged 7 of 10
   killable, wrote the tests, and the count did not move. Measure.
6. **`--clean` between modules** if the progress counter looks cumulative (206 → 498 → 820);
   the cross-check correctly refuses a log that is not one run.
7. **NEVER raise a baseline** to make a run pass. Down is progress; up means a predicate lost
   its test. If a number rises, that is a finding about your own change.

---

## Rules of engagement

- **NEVER run `git` commands that write** (add/commit/checkout/stash/rm/push). The human commits.
- **NEVER run `pre-commit`.** It stashes the entire working tree — including any other writer's
  in-flight work — before any hook runs, regardless of what is staged. There is no safe variant;
  `--files` stashes identically.
- Do not run `pkill`/`killall`/`kill -9`/GPU resets. **GPU 1 is this project's only GPU; 0 and 2
  are reserved.** (Mutation runs are CPU-only but will saturate every core — don't start one
  beside a benchmark.)
- Never read or print `.env`, keys, certs or tokens.
- Do **not** modify `backend/app/**` — this task adds tests. If you find a production defect,
  **report it** and pin it with a characterization test whose docstring says it pins WRONG
  behaviour and names its replacement (see `tests/unit/test_chunking_service.py` for the shape).
- Stack ops go through `./opentr.sh`, never bare `docker compose`.

---

## Test quality bar

Read `backend/tests/CLAUDE.md` first. `python3 scripts/audit-tests.py backend/tests` must report
no un-allowlisted findings, and `--selftest` must pass. Specifically:

- Never `assert x != 403` — a 500 satisfies it.
- Never put the real assertion inside `if status == 200:`.
- A loop over a runtime iterable needs a non-emptiness assertion **outside** the loop.
- `mock.assert_called_once_with` alone is not enough — assert real state.
- **Watch each test fail before it passes**, and say so in your report. A test never seen
  failing is not evidence.
- Adding an allowlist entry is a last resort and needs a written reason. If a detector fires on
  your new test, restructure the test — that is what the `mock-heavy` finding on
  `test_tasks_utility_gpu_stats.py` turned out to warrant.

---

## Per-module notes

**`lockout` (149) — start here; biggest win.** Its baseline note records that **58 edit a log
string and 8 flip a condition guarding only a log call**, so the actionable set is ~83. Needs
Redis (localhost:5177). Two near-duplicate code paths exist (Redis and in-memory) — a mutant
diff can look like it belongs to the wrong one, which is how a survivor was once misreported.

**`dependencies` (175)** — the privilege gates. An inverted role comparison here **is**
privilege escalation, so bias toward `real`. Note the history: an incomplete `MODULE_TESTS`
list once produced **41 false survivors** here, reported as a proxy header-spoofing
vulnerability before anyone checked. Each mutant boots the test client, so this is slow.

**`security` (95)** — JWT + bcrypt; every mutant pays a bcrypt round. Its coverage floor (77%)
is the lowest of the three, so check whether low coverage is inflating the survivor count
before concluding the tests are weak.

**`session` (73)** — session lifetime. Coverage 76% vs a 76% floor: **no headroom**, so any
test you add should raise it. Needs Redis.

---

## Definition of done, per module

1. Every survivor classified, with equivalence proofs written down.
2. Tests added for all `real` ones, each observed failing first.
3. New test file registered in `MODULE_TESTS`; coverage moved (that is the proof it registered).
4. Re-measured; baseline lowered to the measured number; note rewritten.
5. `--check-baseline` still reports `checked 6/6`.
6. `audit-tests` clean, `--selftest` passing, ruff + mypy clean.
7. `git diff backend/app/` is **empty** — confirm no mutation was left in live source.
8. A report stating: counts per class, the real diffs, fail-then-pass evidence, and **anything
   you could not kill and why**.

---

## Context you may want

- `backend/tests/CLAUDE.md` — mutation section, markers, gates
- `scripts/CLAUDE.md` — `run-mutation-tests.sh`'s four historical wrong measurements
- `backend/tests/redaction/test_apply_redactions_mutants.py` — the worked example of an
  all-equivalent module, and of how to write the proofs
- `backend/tests/unit/test_password_policy_mutants.py` — the worked example of a large real set
- Issue #446 — running commentary, including why the "~275 of 636" estimate in its title does
  not extrapolate (measured: 0/10 in `spans`, 40/86 in `password_policy` — re-derive per module)
