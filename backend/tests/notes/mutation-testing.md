# backend/tests — mutation testing (opt-in, never in the gate or CI)

Moved intact from `backend/tests/CLAUDE.md`. Driver: `scripts/run-mutation-tests.sh`.

## Mutation testing (opt-in, never in the gate or CI)

**What it is for.** Coverage says a line *ran*. It cannot say the suite would *notice* if the
line were wrong — and this repo has already shipped tests that could not fail (an `or` chain
ending in `"register" in page.url`; a `gpu` marker that selected nothing). Mutation testing
answers the question directly: edit the source (flip `<` to `<=`, drop an `and` clause, change
a constant, return `None`) and re-run the tests. A mutant that **dies** proves a test really
checked that behaviour. A mutant that **SURVIVES** is the finding: the suite executes that line
and asserts nothing about it, so the line could be deleted with the suite still green. For an
auth predicate that is a removable security control.

**Tool: `mutmut`** (`backend/requirements-dev.txt` — deliberately *not* `requirements-ci.txt`,
which CI installs and must stay fast). Chosen over `cosmic-ray` because it needs no session
database or job queue (config is `[tool.mutmut]` in `backend/pyproject.toml`, entry point is one
script), and because cosmic-ray's main advantage — distributed parallel execution — is exactly
what must **not** happen here: every mutant's tests share the one live Postgres, and
cross-worker concurrency on it is this repo's known deadlock shape (the advisory lock and
`system_settings` collisions of issues #389/#431). Serial is correct, not a limitation.

**Scope: six security-critical modules only** (`[tool.mutmut] paths_to_mutate`) —
`redaction/spans.py` (masking off-by-one leaks the character it should hide),
`auth/password_policy.py` (five independent `require_*` predicates), `core/security.py`
(JWT/bcrypt), `api/endpoints/auth/dependencies.py` (+ `lifecycle_gates.py`, `external_token.py`; the
privilege gates — an inverted role comparison is privilege escalation), `auth/lockout.py` (+ `lockout_store.py`, `lockout_admin.py`), `auth/session.py`. A whole-codebase run
is hours and is not the point.

```bash
./scripts/run-mutation-tests.sh --check            # preconditions, mutates nothing
./scripts/run-mutation-tests.sh --list             # targets + the tests each one runs
./scripts/run-mutation-tests.sh --module spans     # START HERE (~1-3 min)
./scripts/run-mutation-tests.sh --module spans --dry-run
./scripts/run-mutation-tests.sh --results          # re-report, no re-run
./scripts/run-mutation-tests.sh --show <id>        # the diff for one survivor
./scripts/run-mutation-tests.sh --verify <id>      # does that survivor really survive?
./scripts/run-mutation-tests.sh --check-baseline   # the RATCHET (also gate phase 7)
python3 scripts/triage-mutants.py <log> <module>   # observable vs unobservable
```

**"Kill every mutant" is not the goal, and treating it as one is why this stalled for a day.**
A clean run of `lockout` reports 149 survivors of which **77 edit a log message and 12 flip a
condition guarding only a log call** — unobservable by the repo's own rule, and asserting on log
text produces tests that break on every reword. So the gate is a **ratchet**:
`scripts/mutation-baselines.tsv` records each module's measured count and
`--check-baseline` fails when one RISES. Down is progress; up means a predicate lost its test.
Lower the baseline when you add tests; **never raise it to make a run pass**.

`triage-mutants.py` splits survivors into `noise-string`, `noise-log-branch` and `logic` so the
count you act on is the last one. Its rules were wrong three times, twice by over-reporting and
once — the dangerous direction — by under-reporting: a string inside a **predicate**
(`hashed_password.startswith("$pbkdf2-sha256$")`) *is* the logic, and calling it a log edit hid
a FIPS-rehash finding. Every rule now has a must-fire and a must-stay-clean case in
`--selftest`.

**A survivor is a claim, not a fact — `--verify` makes it prove itself.** It applies the
mutation to the real source, runs the module's tests, restores, and reports
CONFIRMED-SURVIVED / KILLED / UNVERIFIABLE. Needed because the harness produced wrong numbers
four different ways: an incomplete `MODULE_TESTS` list (41 false survivors in `dependencies`,
which I reported as a proxy header-spoofing vulnerability before checking), a second `--module`
silently replacing the first, cached verdicts from an older test list presented as current, and
a survivor list read from a **stale log** that never ran that module. Each now has a guard —
respectively a coverage pre-flight, an error, a test-list fingerprint, and a
`--- mutating <path> ---` check.

⚠️ **`--verify` transiently edits live source.** It holds a per-module `flock` (two concurrent
verifies each restore from their own backup, so one reinstates the other's mutation — observed)
and restores on INT/TERM/ERR/EXIT, but nothing survives a SIGKILL of the process group: a
stopped batch left `now <` mutated to `now <=` in `app/auth/lockout.py`. **Do not commit while
one runs** (pre-commit stashes the whole tree, issue #434), and check
`git diff backend/app/` after any interrupted run.

**Runtime** (per module, serial): `spans` ~1-3 min · `password_policy` ~5-15 min · `security`
~10-30 min (each mutant pays a bcrypt round) · `dependencies` ~20-60 min (each mutant boots the
test client; `lifecycle_gates`/`external_token` share its test list) · `lockout` ~30-90 min · `session` ~15-45 min · `--all` is **hours**. Needs
Postgres (5176) and, for lockout/session, Redis (5177). CPU-only — it never touches the GPU,
but it will saturate every core, so don't start one beside a benchmark.

**Two traps the script handles, and you must not undo.** `-n0` is appended via
`PYTEST_ADDOPTS` to override pyproject's `-n auto`: without it every mutant forks one xdist
worker per core against the shared DB. The script also still exports the seven old security-suite
`RUN_*` gates (its `GATES` array), but those are dead (see "Markers and gates"): no selected file
is gated any more, so they select nothing. Keep the principle, though: **a skipped test kills no
mutant**, so a gated or skipping file in a module's `MODULE_TESTS` reports false survivors that
look exactly like real findings.

**Reading a surviving mutant.** `--results` lists ids; `--show <id>` prints the diff.
`killed` = genuinely checked. `survived` = the finding; add the missing assertion, or conclude
the line is dead and delete it. `timeout` counts as killed but check it is not a real infinite
loop the tests were papering over. `suspicious` = far slower than baseline, usually a mutated
retry/sleep bound. Survivors in *log lines and error strings* are noise — judge by whether a
real caller could observe the difference.
