# Handoff: full end-to-end audit of `chore/test-suite-perf-and-quality-overhaul`

**Paste this whole file as your first message in a fresh session.** Nothing else is needed;
everything you must know is here or reachable from here.

---

## What you are auditing

Branch `chore/test-suite-perf-and-quality-overhaul` in `/mnt/nvm/repos/transcribe-app`,
**77 commits, 346 files, +42,308 / −3,810** against `master`. Tracking issue **#431**.

It began as "make the test suite fast and accurate" and became a much larger correctness
sweep, because tightening tests kept exposing real defects in the application.

You are task **#27** in the session task list. Everything else is closed. Your findings decide
whether this branch is ready to become a PR.

---

## Your scope — all five parts, not a sample

### 1. Blind spots — issues nobody thought to look for
This is the part with the most value and the least structure. Seed it with the seven shapes
this branch proves the project is bad at seeing, because **every one was invisible until
something forced it**:

1. **A gate that exists but never runs.** `scripts/audit-tests.py` was in neither pre-commit
   nor CI and appeared only in prose. `run-e2e-smoke.sh` exited non-zero on *every* run
   (pytest exit 5 = "no tests collected"). `RUN_SCHEMA_DRIFT_TESTS` was set in exactly one
   place, at severity `warn`. `alembic/env.py` never imported `app.models`, so
   `target_metadata` was empty (0 tables vs 54) and `--autogenerate` has **never** worked as
   a drift check here.
2. **A measurement that is wrong and believed.** The mutation harness produced wrong numbers
   four different ways; one was reported to the user as a proxy header-spoofing
   vulnerability before being checked. The route-coverage metric was quoted as 141, then 117,
   when the answer was 28.
3. **A test that cannot fail.** Tautologies (`assert mode in {"v3","v4"}` where those are the
   only modes), assertions inside an `if` with no `else`, "controls" that also pass against a
   handler returning all zeros.
4. **A test that passes for the wrong reason.** `Settings` had `env_file=".env"` resolved
   against the *working directory*, so `pytest` from the repo root loaded the production
   `.env`; an SSRF test stopped exercising the guard and started asserting that nothing
   happened to be listening on port 11434.
5. **Coupling to how it is invoked.** One `uuid4()` in a `parametrize` made all 24 xdist
   workers fail collection — and passed when its own file ran alone.
6. **A constraint the database enforces and Python never declares.** 23 found.
7. **A security flag that loosens more than it says.** `allow_private=True` disabled the
   cloud-metadata block entirely, not just the address range — reachable from
   `auth/oidc/discovery.py`, i.e. at **login time**.

**Ask explicitly: what is NOT on that list?**

### 2. Full review of every change — independently, not on trust
Re-run the control experiments. Re-derive the numbers. Challenge the conclusions.

**The previous agent got things wrong and corrected them publicly. Assume more.** The known
record, so you can calibrate:

- Reported `_enforce_proxy_identity_consistency` as having zero coverage. **Wrong** — it was
  an incomplete mutation test map (41 false survivors). The "proof" was a hand-applied
  mutation to the *wrong function*: `s.index(...)` found the first occurrence in the file,
  which was the Redis path, not the in-memory one.
- Quoted 141 then 117 uncovered routes. **Both wrong**; 28 was right. The first fix
  over-matched (`/api/tasks/{task_id}` scored as covered by a test naming
  `/api/tasks/system/fix-file/x`) and reported 32 for the wrong reason.
- Called 19 `session` survivors "real logic findings". Of 4 hand-checked, **1** was real and
  **2 were equivalent mutants** no test can kill.
- Its own mutant classifier was wrong three times — twice over-reporting, once
  **under**-reporting (a string inside a predicate is logic, not a log edit), which hid a
  FIPS-rehash finding.
- Wrote a guard inside `if ! $DRY_RUN` so no dry run could exercise it, and a stale-log check
  that read "improved (77 → 0)" from logs that never ran those modules.

Pay particular attention to anything asserted **without** a control experiment.

### 3. Compliance
The FIPS / FedRAMP / GDPR surfaces specifically: are the controls **enforced** and
**exercised**, not merely present? Includes both Art. 17 erasure paths
(`POST /admin/gdpr/erase-user/{uuid}` and the org-admin twin), the password-history control,
and the audit-record fidelity that mutation testing flagged as observable only by someone
reading the audit log.

### 4. Everything still works
Backend suite, frontend vitest, E2E, and the pre-merge gate — green, with the numbers stated.
**Not "green on my machine":** also confirm the CI-mode configuration
(`SKIP_S3=True SKIP_OPENSEARCH=True`), because two tests already **failed rather than skipped**
there and would have broken the CI job.

### 5. What is missing or wrong in the suite itself
Anything to add, remove or correct: uncovered modules, weak assertions, wrong markers, slow
barriers, allowlist entries that have gone stale, and **docs that state a number the code no
longer produces** (that has happened repeatedly here).

---

## Current measured state — verify these, don't trust them

| | baseline | claimed now |
|---|---|---|
| backend passing | 4,752 | **6,591** |
| real skips | 458 | **62** (146 in junit, which counts xfails) |
| wall clock | 510.9 s | **138.9 s** |
| failed / errors | 0 | **0** |
| frontend | 474 | **669 passed, 76 files** |
| routes with no test reference | 141 (mis-measured) | **0** |
| mutation: `dependencies` | 470 survivors | **180** (coverage 68.4% → 93%) |
| mutation: `lockout` | 200 | **149** (coverage 80% → 85%) |
| mutation: `session` | 77 | **73** |
| mutation: `spans` / `security` / `password_policy` | — | 10 / 133 / 91 |

Reproduce with:

```bash
./scripts/run-backend-tests.sh            # writes junit XML + a tee'd log; NEVER re-run for counts
./scripts/run-backend-tests.sh --summary  # re-report from the artifacts
cd frontend && npm run test && npm run test:audit && npm run test:audit:selftest
python3 scripts/audit-tests.py --selftest && python3 scripts/audit-tests.py backend/tests
python3 scripts/audit-route-coverage.py --selftest && python3 scripts/audit-route-coverage.py
python3 scripts/triage-mutants.py --selftest
./scripts/run-mutation-tests.sh --check-baseline      # the ratchet; reads the LAST run's logs
./scripts/run-integration-tests.sh                   # THE pre-merge gate, ~10 min
```

---

## The application defects this branch claims to fix

Each was control-verified (revert the fix → a test fails). **Re-verify a sample yourself.**

- **`DELETE /api/admin/users/{uuid}` returned 500 for any account that had ever had a file
  transcribed.** Speakers were bulk-deleted before the segments referencing them, and
  `transcript_segment.speaker_id` is `ON DELETE NO ACTION`. Both existing tests deleted an
  empty user, so every `if <ids>:` branch was skipped.
- **SSRF:** `allow_private=True` disabled the cloud-metadata block (above).
- **`GET /users/search` was a cross-tenant directory** — no `RequestContext` reached it.
- **The group plane had no tenant boundary** — `user_group` was the only user-owned table
  without `organization_id`. Migration **v388** adds it; `add_member` could previously add a
  member of another tenant.
- **`unlock_account` had no test of any kind.** Dropping `_normalize_identifier` clears the
  wrong key, so an account stays locked while the admin is told it worked.
- **The entire Redis-down lockout path had no tests** (dispatch is
  `hasattr(store, "pipeline")`, and the fake Redis has one).
- **`verify_token`'s `exp={"essential": True}`** — flip it and a token with no `exp` claim is
  valid forever.
- `DELETE /admin/scim-tokens/{uuid}` 500'd on a malformed UUID (a *revocation* endpoint).
  `PUT /user-settings/download` silently accepted a typo'd field with 200. The ORM declared
  `NOT NULL` what v387 made nullable. `ORDER BY start_time` was not a total order in 23 places
  (#433), which made re-indexing one unchanged corpus produce three different chunk counts.

---

## Hard rules — non-negotiable, and each has a reason

- **NEVER `pre-commit run --all-files`.** It stashes every unstaged change in the repo,
  including other agents' in-flight work. It has destroyed work twice here (issue **#434**).
  Use `backend/venv/bin/pre-commit run --files <paths>`.
- **NEVER `pkill`, `killall`, `kill -9`, or `nvidia-smi --gpu-reset`.** Blocked by
  `permissions.deny`. A blanket kill previously wedged a GPU mid-transcription and required
  two machine restarts.
- **GPU 1 (RTX 3080 Ti) is this project's only GPU.** GPUs 0 and 2 are reserved for unrelated
  work — never touch them.
- **Stack ops go through `./opentr.sh`**, never bare `docker compose` (wrong DB/storage →
  "relation does not exist").
- **Never delete or mutate dev data.** E2E deletes only what it creates. Negative-login tests
  must use a **nonexistent account** — progressive per-account lockout poisons the whole suite.
- **Never read, log or paste** `.env`, `.env.*`, `*.key`, `*.pem`, `*.p12`, `id_rsa*`, or any
  token. If a task needs a secret value, ask.
- **`./scripts/run-mutation-tests.sh --verify` transiently edits live source.** It holds a
  per-module `flock` and restores on INT/TERM/ERR/EXIT, but nothing survives SIGKILL. **Do not
  commit while one runs**, and check `git diff backend/app/` after any interrupted run.
- **Don't re-run a slow suite to get counts.** Use the junit XML and tee'd log that
  `run-backend-tests.sh` already wrote.
- **A branch changes only via a reviewed PR.** No local merge onto `master`.

---

## Coordination — two other branches are live

- **`feat/rag-corpus-scale-403`** is *stacked on this branch* and merges it in periodically.
  It independently fixed `#433`'s ordering on its own path and has its own
  `test_segment_ordering_is_total.py`. At merge, **fold the narrower fix and test into this
  branch's version** — two tests for one rule is the "two paths doing the same job" shape the
  repo forbids. Its notes are worth reading:
  `.claude/worktrees/rag-403/.rag-403/notes-for-testing-agent.md` and
  `ddl-orm-divergence.md`.
- **PR #364** (auth identity overhaul) is open and unmerged, and touches the same auth surface.
  Check its diff before proposing any auth change.

---

## Where the project's own knowledge lives

Read these before judging anything in their area; they are dense and load-bearing.

- Root `CLAUDE.md` — repo-wide rules, the `opentr.sh` requirement, the four test-honesty tools.
- `backend/tests/CLAUDE.md` — markers, `RUN_*` gates, E2E fixtures, and the mutation section
  (purpose, the ratchet, the four wrong-number modes and their guards).
- `scripts/CLAUDE.md` — every script, including `audit-route-coverage.py` and why it is not a
  grep, and the mutation harness's failure history.
- `backend/app/api/CLAUDE.md` — route conventions and traps (`require_capability` returns
  **404, not 403**; depend on `get_current_active_user`, never `get_current_user`).
- `backend/app/db/CLAUDE.md`, `backend/alembic/CLAUDE.md` — the 5-step schema procedure and
  why every revision needs a detection arm.

---

## Deliverable

1. **Findings, ranked by severity**, each with: the file/line, a concrete failure scenario
   (inputs → wrong output), and whether you verified it or are inferring.
2. **Verdict on each of the five scope parts**, with the numbers you measured yourself.
3. **Anything the previous agent got wrong** — stated plainly. It corrected itself several
   times; assume the record is incomplete.
4. **What is still missing from the suite**, as a prioritised list.
5. Explicitly: **what you could not verify**, and why.

Do not fix things silently as you go. If you fix something, say so with the evidence, and
control-verify it (revert the fix, watch a test fail) — that is the standard this branch was
held to and the only reason its claims are worth anything.

Use parallel agents freely for breadth, but **integrate and run the tests yourself** — the
pattern that worked here was agents editing disjoint files while one session ran every suite
and judged the output. Deconflict by file ownership; two agents in one file corrupts both.
