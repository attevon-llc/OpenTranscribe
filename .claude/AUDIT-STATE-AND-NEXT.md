# #431 audit — state of play and what to do next

**Read this first if you are picking the work up.** It says what is done, what is in flight,
what is next, and — most importantly — the traps that already cost time so you do not pay for
them twice. Findings live in `.claude/AUDIT-full-audit-findings.md`; the original scope is
`.claude/HANDOFF-full-audit.md`.

Last updated: 2026-08-13, after wave 2.

---

## Where the branch is

`chore/test-suite-perf-and-quality-overhaul`, **16 commits pushed**, tree clean, **not a PR yet**.

| Measurement | Value |
|---|---|
| backend | **6,729 passed / 0 failed / ~106 s** (from 4,752 / 511 s) |
| frontend | 669 passed / 76 files / 21.6 s |
| pre-merge gate | **exit 0**, all seven *measurable* phases |
| mutation ratchet | **NOT MEASURED** — see "in flight" |
| E2E | 271 passed / 1 pre-existing env failure, since resolved |
| visual regression | 8 baselines failing (issue #447) |

**24 of 34 session tasks complete.** Remaining work is tracked in GitHub, not just here.

---

## GitHub issues — the durable tracking

Filter `label:security` for the launch-blocking set.

| # | Title | Labels |
|---|---|---|
| 440 | DB sessions held across slow work hang Alembic and wedge the DB | security, backend, bug, performance |
| 441 | JWT algorithm duplicated 3×; issuers and verifiers disagree | security, backend, bug |
| 442 | GDPR Art. 17: no erasure ledger, legal-hold never re-erased | security, backend |
| 443 | Audit log: split `user_id` into actor and target | security, backend |
| 444 | `chat_completion` has no SSRF validation at all | security, backend, bug |
| 445 | 41% of modules untested; four authz decorators untested | testing, backend |
| 446 | ~275 of 636 mutation survivors are real gaps | security, testing, backend |
| 447 | Visual regression: 8 baselines failing | testing, frontend |

---

## In flight right now

1. **Mutation re-measurement** (issue #446 depends on it). `spans` is done and genuinely
   ratcheting: **10 survivors of 206, coverage 97% vs floor 95**. The rest need a **re-run
   with `--clean` before each module** — see the trap below.
2. **Session-leak sweep + AST guard** (issue #440) — agent working `summarization.py:490`,
   `speaker_identification_task.py:304`, `watch_source_tasks.py:117`,
   `video_processing_service.py`, `cleanup.py:446`.
3. **JWT unification** (issue #441) — agent working one owner for issuance, one for
   acceptance, with a dual-accept list for migration safety.

---

## Next, in priority order

1. **Finish the mutation re-measure** (`--clean` per module) and lower any baseline that
   improved. `security` should come back **below 133** — its old baseline predates both the
   `verify_token` exp/essential test and `test_verify_token_claims.py`, which was missing from
   `MODULE_TESTS[security]` entirely.
2. **Resolve `password_policy` 91 → 101.** The first (untrustworthy) batch showed a rise. It is
   either a genuine regression from wave 2's new `min_age` and current-password-floor
   predicates — which is the ratchet doing its job — or cache contamination. Do not lower or
   raise the baseline until a clean run says which.
3. **Close the top ~15 mutation findings** (issue #446), not the tail. Diminishing returns
   beyond that, and the ratchet prevents regression.
4. **Visual regression** (#447) — diff each `.actual.png` per surface. Do **not** blanket-refresh.
5. **Coverage holes** (#445), starting with `utils/auth_decorators.py` — four authz decorators
   with zero tests.
6. **Then** the PR.

---

## Traps that already cost time — do not repeat these

- **`--clean` before EACH mutation module.** Running them back-to-back makes mutmut's progress
  counter cumulative (206 → 498 → 820 → 1046), which the cross-check correctly refuses as
  "this log is not one run", and risks stale cached verdicts (the script's own documented trap
  #3). Runs are ~5 min each, not the 30–90 the docs claim, so cleaning between them is cheap.
- **Never run `pre-commit` while E2E is running.** It stashes and restores the whole tree,
  which restarts uvicorn — observed 19 reloads in 5 minutes, `/health` up on 9 of 40 samples.
  The SPA renders everything behind `{#if $authReady}` behind a **60 s** axios timeout, so a
  flapping backend makes *every* login fixture time out at once and reads exactly like
  order-dependent flakiness. It is not.
- **Run the whole backend suite before believing agent work.** Wave 2's agents each ran only
  their own targeted tests; the integrated run surfaced **8 regressions** none of them could
  see, including every `audited[0]` in the proxy suite silently retargeting onto a newly-added
  audit record.
- **Commit agent work as one changeset when the gates are interdependent.** Partial staging
  makes pre-commit stash the *unstaged* files, so the whole-tree auditor sees a tree where some
  files exist and their allowlist entries do not, and fails on a state that never existed.
- **A leaked DB transaction blocks the DDL tests.** If `test_v38x_migration_consistency` fails
  with `LockNotAvailable`, check `pg_stat_activity` for `state='idle in transaction'` before
  debugging the tests. `./opentr.sh restart-backend` clears it with no data loss.
- **Positional indexing is fragile in both DOM and event lists.** `.nth(1)` broke four E2E
  tests when a menu item was inserted; `audited[0]` broke five unit tests when an audit record
  was added. Select by identity.

---

## Standing judgement calls

- **Mutation testing stays scoped to the six security modules.** It is the highest cost per
  finding of any tool here; it earns its place on auth/redaction predicates and nowhere else.
- **The ratchet is the durable value, not the survivor count.** It stops *new* unasserted
  security predicates landing. The counts are a ceiling, not an all-clear.
- **`NOT MEASURED` is a first-class result.** Exit 4, rendered as
  "⊘ proves nothing, not counted as a pass". Never collapse it into pass or fail — five separate
  gates in this repo were silently passing on absent evidence before that distinction existed.
