# #431 audit — state of play and what to do next

**Read this first if you are picking the work up.** It says what is done, what is in flight,
what is next, and — most importantly — the traps that already cost time so you do not pay for
them twice. Findings live in `.claude/AUDIT-full-audit-findings.md`; the original scope is
`.claude/HANDOFF-full-audit.md`.

Last updated: 2026-08-13, after the quick-wins round (waves 1-5 complete).

---

## Where the branch is

`chore/test-suite-perf-and-quality-overhaul`, **102 commits pushed** (head `8f330d7e`), tree
clean, **not a PR yet**.

| Measurement | Value |
|---|---|
| backend | **6,906 passed / 0 failed / 146 skipped / ~96 s** (from 4,752 / 458 skipped / 511 s) |
| frontend | 669 passed / 76 files / 21.0 s, `test:audit` clean, `test:audit:selftest` 27/27 |
| pre-merge gate | **exit 0**, all phases, including the two this audit added |
| mutation ratchet | **MEASURED 6/6** — holds at 149 / 86 / 175 / 95 / 73 / 10 |
| E2E | 271 passed / 1 env failure, diagnosed and resolved |
| visual regression | 2 suite holes FIXED; 8 baselines still red (issue #451) |
| auditors | audit-tests 74/74 selftest, session-lifetime 23/23, frontend 27/27 |

**31 of 38 session tasks complete.** Remaining work is tracked in GitHub, not just here.

## GitHub issues — the durable tracking

Filter `label:security` for the launch-blocking set.

| # | Title | Labels | State |
|---|---|---|---|
| 440 | DB sessions held across slow work hang Alembic and wedge the DB | security, backend, bug, performance | **resolved**, commented |
| 441 | JWT algorithm duplicated 3x; issuers and verifiers disagree | security, backend, bug | **resolved**, commented |
| 442 | GDPR Art. 17: no erasure ledger, legal-hold never re-erased | security, backend | open (task #14) |
| 443 | Audit log: split `user_id` into actor and target | security, backend | open |
| 444 | `chat_completion` has no SSRF validation at all | security, backend, bug | **resolved**, commented |
| 445 | 41% of modules untested; four authz decorators untested | testing, backend | open (task #17) — NOTE the four decorators are now DELETED (#450) |
| 446 | ~275 of 636 mutation survivors are real gaps | security, testing, backend | partial — ratchet real, triage open (task #19) |
| 447 | Visual regression: 8 baselines failing | testing, frontend | superseded by #451 |
| 449 | Chunk boundaries depend on NLTK punkt + a 5-min global cooldown | testing, backend | open |
| 450 | ~650 LOC of dead code (`auth_decorators`, `transcription_service`) | backend | **DONE** `ae64124f`, commented |
| 451 | Visual baselines depend on live mutable dev data; cannot be refreshed | testing | **NEW** — holes fixed, redesign open |
| 452 | Nav overflows at 1280px, clips user menu; 1280 untested | bug | **NEW** — real UI bug |

---

## In flight right now

Nothing. The tree is clean and pushed. Pick up from "Next" below.

---

## Next, in priority order

Everything cheap has been taken. What remains is genuinely substantial — these are the
"difficult ones", in the order that buys the most.

1. **Triage the top mutation findings** (issue #446, task #19). The ratchet is now real and
   measured 6/6, so this is pure gap-closing rather than harness repair. Work the `logic`
   bucket only — `lockout`'s 149 includes 77 log-message edits and 12 log-only branch flips,
   unobservable by this repo's own rule. Re-derive the "real gaps" count from the CURRENT
   `triage-mutants.py`; the figure in the issue title predates three rule corrections.
   ⚠️ `--verify` transiently edits live source and holds a per-module `flock`. Never commit
   while one runs.
2. **Coverage holes** (issue #445, task #17). Note the issue's headline example is now
   obsolete: the four untested authz decorators were **deleted** in #450, not tested. Re-derive
   the module list before planning.
3. **GDPR erasure completeness** (issue #442, task #14) — request ledger, legal-hold
   re-erasure, and the fact that a backup restore resurrects erased data. The largest remaining
   compliance gap.
4. **English-only RAG/chat scope** (task #37). The chat services currently have **zero**
   language handling — `rg language backend/app/services/chat/*.py` returns nothing — while
   `MediaFile.language` already records a detected code and the embedding registry contains
   multilingual models. The scope is neither enforced nor documented anywhere, so today the
   product silently produces poor answers on non-English transcripts rather than saying so.
   Surface it at scope resolution, in the shape of the existing `context_dropped` warning.
5. **Visual regression redesign** (issue #451). The two suite holes are fixed; what remains is
   making the four stabilisable surfaces deterministic (seeded data + masked live gauges) and
   redesigning the two that cannot be (`gallery`, `file_detail` render newest-first live data).
6. **The 1280px nav overflow** (issue #452) — a real user-facing bug with a one-line test gap.
   Smaller than the rest; a reasonable warm-up.
7. **Then** the PR.

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
