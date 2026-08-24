# Handoff — OpenTranscribe audit remediation, 2026-08-24

**Single handoff.** Paste the PROMPT block below into a fresh **Sonnet, high effort** session.
`.claude/handoff-env-audit.md` is background reference it will read itself.

---

## PROMPT

You are picking up OpenTranscribe (`/mnt/nvm/repos/transcribe-app`) to work **autonomously for a
full day**.

**Your work list is `/home/superdave/.claude/plans/checkout-the-master-branch-async-mccarthy.md`.**
Read it in full first — start with **"⚠️ STATUS UPDATE — 2026-08-24"** and
**"TODAY'S EXECUTION PLAN"**. Then read `.claude/handoff-env-audit.md`, which records what NOT to
redo: four verified false positives and seven deliberate decisions that look like obvious cleanups.

Repo: `master` clean at `a85bfb03`. Dev stack up, 16/16 healthy. No worktrees, no open PRs.

---

## 🎯 THE GOAL, and the order of priority

> **Fix the audit findings → re-audit → fix what that finds → loop until the audits are clean —
> WHILE THE APP STILL WORKS.**

**"The app still works" outranks "the audits are clean."** They can diverge, and the trap is
obvious once stated: *the fastest way to make a dead-code auditor return zero is to delete things.*
A clean audit on a broken app is a failed day. If you cannot have both, **keep the app working and
leave the finding open with a written reason.**

**"Works" means, concretely:** the app **transcribes a real file end to end and produces a real
transcript**, and the **LLM-backed features (chat with citations, summarization, topics) run** —
with a real model on a GPU for the end-of-day check. All GPUs are free today, so there is no excuse
for skipping it. Details in the health gate below.

---

## 🤖 AUTONOMOUS OPERATION

**Standing authorization to run the whole loop unattended.** Do not stop to ask about individual
commits, tests, or judgement calls the plan already decided. The plan front-loads the judgement
precisely so you can execute without a human in the loop.

**✅ Just do it, repeatedly, all day:**
- Create the branch; commit and re-commit as often as needed; push the branch whenever
- Edit any source, test, config or doc file
- Run tests, linters, auditors, `svelte-check`, the full gate; iterate on failures
- `./opentr.sh start|stop|restart-backend|restart-frontend|logs|shell` on the **dev** stack
- `./opentr.sh rebuild-backend|rebuild-frontend`
- Use **any GPU** for building and testing (today-only relaxation)
- `scripts/safe-precommit.sh run --all-files` when the tree is quiet

**⛔ STOP and ask — these reach outside the repo or destroy state:**
- `./scripts/release.sh tag|publish|promote|finish` — Docker Hub / GitHub, **David's call**
- Any `--force-scan` override (needs a written reason **and** David's approval)
- **Opening the PR.** Push the branch, write the summary, stop.
- `git push --force` to a shared branch; any rebase or squash of shared history
- `./opentr.sh reset`, `fresh-destroy`, dropping the DB, deleting volumes, deleting **`.env`**
  (gitignored — no recovery)
- Deleting or mutating **dev data** (recordings, users, speakers)
- `gh issue close`, or editing issues beyond adding a comment

**🚫 Never:**
- `pkill` / `killall` / `kill -9` / `nvidia-smi --gpu-reset` — a killed CUDA context has wedged this
  machine **twice**, needing a full restart. Stop the **container** instead.
- `--no-verify` or any hook bypass. If a hook fails, **read the finding** — roughly half the failures
  blamed on tooling were real.
- Silencing a finding with `noqa` / `type: ignore` / `eslint-disable` / a relaxed assertion / an
  allowlist entry you could have fixed.

**When blocked:** do **not** stall. Record it in the progress log, skip the task, continue. One stuck
task must not cost the day.

---

## 🩺 THE HEALTH GATE — run after every group, before every commit

**This is the most important mechanic in this handoff.** Nothing gets committed unless the app still
works.

```bash
# ── TIER 1 · seconds · after every edit ─────────────────────────────
cd frontend && npx svelte-check --tsconfig ./tsconfig.json    # baseline: 0 errors, 0 warnings
cd backend  && venv/bin/python -m pytest -o addopts="" -q <the tests you touched>

# ── TIER 2 · ~2 min · before every commit ───────────────────────────
cd backend  && ../scripts/run-backend-tests.sh --summary       # compare to the pre-change count
cd frontend && npm run test                                    # baseline: 164 files / 1585 tests
./opentr.sh status                                             # 16/16 healthy

# ── TIER 3 · ~10 min · after every GROUP ────────────────────────────
./opentr.sh restart-backend && ./opentr.sh logs backend | tail -50   # no tracebacks on boot
./scripts/e2e/run-e2e.sh -m upload                             # 25 tests, REAL processing

# ── TIER 4 · before the final push ──────────────────────────────────
./scripts/run-integration-tests.sh                             # the pre-merge gate
```

### The functional smoke — "WORKS" has a specific meaning here

Tests passing is not the same as the app working. **"Works" = it can TRANSCRIBE properly, and the
LLM-backed features run end to end.** Run this after groups 1, 2, 3 and 7, and once at the end.

**Part 1 — transcription must actually complete (the core product):**

1. `./opentr.sh start dev` → 16/16 healthy
2. Log in (`admin@example.com` / `password`)
3. **Upload a short audio file and let it run to completion.** Not "the upload returned 200" —
   watch it through the pipeline: `./opentr.sh logs celery-worker`. A long transcription is usually
   still running; **do not kill it.**
4. **Read the transcript.** Real words, sensible segments, speakers assigned. A transcript of empty
   segments is a failure that returns HTTP 200.
5. The gallery card shows a **correct duration** — that is B7. A 2-hour recording must read
   `2:05:00`, not `125:00`.
6. Search finds a phrase from that transcript; the file detail page opens.

⚠️ **The nltk fix (`244d26ad`) is the reason to watch step 3 closely** — an unreadable corpus used
to fail the whole transcription silently, and it had been breaking `rehearse` for weeks.

**Part 2 — LLM features, on a GPU (all GPUs are free today):**

```bash
./opentr.sh start dev --with-llm-test     # real vLLM on LLM_TEST_GPU_DEVICE_ID
#   …or, when you only need the plumbing exercised and not real generation:
./opentr.sh start dev --with-mock-llm     # OpenAI-compatible mock, no GPU, no API key
```

Then verify: **chat answers over the transcript with working citations**, **summarization
produces a real summary**, and **topic extraction returns topics**.

- Use `--with-llm-test` (real GPU model) for the **end-of-day** smoke — it is the honest test.
- Use `--with-mock-llm` for the per-group smokes: retrieval, redaction masking, citations, SSE and
  usage recording all still take their **real** paths; only token generation is canned. Much faster.
- ⚠️ **Never start the mock LLM as a bare host process** — it binds 5199 and then blocks the
  container.
- ⚠️ `LLM_TEST_GPU_DEVICE_ID` selects a **physical card**. All GPUs are free today, but never
  renumber it as if it were a port.
- ⚠️ **Group 3 (redaction) is exactly the path that feeds the LLM.** If masking regresses, chat still
  answers — it just answers with text that should have been masked, or with `""` for every chunk.
  Check the answer has real content **and** that PII is masked.

⚠️ **Groups 1, 2 and 3 change the upload, storage and redaction paths — the three most likely to
break silently, and the three where "the API returned 200" hides the failure.** Smoke after each.

### If the health gate fails

**Revert the change that broke it; do not push forward.** Then either fix it properly or record the
finding as "attempted, reverted, here is why" and move on. A red gate is information — never
disable, skip, or relax an assertion to get past it.

---

## 🔁 THE LOOP

```
PHASE A — implement
  for each group 1..10:
      implement the tasks
      HEALTH GATE (tier 1→2, tier 3 at group end)
      commit   (conventional commit, one per group, independently revertible)
      push

PHASE B — re-audit and converge
  iteration = 1
  repeat:
      run ALL auditors (below)
      if every auditor is clean-or-explained AND the health gate passes:  DONE
      fix what they found
      HEALTH GATE
      commit + push
      iteration += 1
  until converged OR iteration == 4

PHASE C — close out
  full health gate (tier 4) + functional smoke
  write the summary
  STOP — do not open the PR
```

**Cap the loop at 4 iterations.** If it has not converged, that is a finding, not a failure — report
what is left and why. Auditors that keep producing new findings usually mean the detector needs an
allowlist entry with a written reason, not more code changes.

### Convergence — what "clean" actually means

An auditor is **clean-or-explained** when every remaining finding either (a) is gone, or (b) has a
written reason in its allowlist. **These tools emit candidates, not verdicts** — the plan and
`handoff-env-audit.md` list findings already verified as false positives. **Do not "fix" those to
make a number go down.**

```bash
# self-test first — a detector that matches nothing looks identical to a clean tree
python3 ~/.claude/skills/env-audit/scripts/audit-env.py             --selftest   # 14 cases
python3 ~/.claude/skills/fail-open-audit/scripts/audit-fail-open.py --selftest   # 12 cases
python3 ~/.claude/skills/dead-code-audit/scripts/audit-dead-code.py --selftest   # 10 cases

python3 ~/.claude/skills/env-audit/scripts/audit-env.py . \
  --exclude-prefix OIDC_ --exclude-prefix SAML_ --exclude-prefix LDAP_ \
  --exclude-prefix PKI_  --exclude-prefix PROXY_
python3 ~/.claude/skills/fail-open-audit/scripts/audit-fail-open.py backend/app --critical-only
python3 ~/.claude/skills/dead-code-audit/scripts/audit-dead-code.py . --scan backend/app

# the repo's own gates
python3 scripts/audit-tests.py backend/tests
python3 scripts/audit-session-lifetime.py backend/app
cd frontend && npm run test:audit && npm run check:i18n
```

**Baselines to beat** (from the plan): fail-open **231 raw / 169 reported / 42 critical**;
dead-code **79 DEAD / 27 TEST-ONLY**; env **54 INERT / 3 ORPHANED** (measured on the *old* 410-key
template — re-derive against the current 140-key one). B1 should vanish from the fail-open critical
list, C1–C4 from INERT, E1/E3/E6 from DEAD. **Anything that does not move is a fix that did not
land.**

---

## 🧠 ESCALATE TO OPUS — four tasks are above this session's pay grade

You are running on Sonnet. Most of this plan is explicit enough to execute directly — that is why it
was written the way it was. **But four items are security- or crypto-critical, where a subtly wrong
fix is worse than no fix.** For those, spawn an **Opus subagent** with the `Agent` tool
(`model: "opus"`), rather than implementing them yourself.

| Task | Why Opus |
|---|---|
| **A1 — authenticated SSRF** | The only finding an unprivileged remote user can trigger. Address-class handling, DNS rebinding, and defence-in-depth placement are easy to get subtly wrong, and a wrong fix ships a security hole that looks fixed. |
| **B3 → B6 — the redaction chain** | These four must **cohere**. An earlier design of this exact fix was internally contradictory (part 1 made part 3 dead code, and mapped `"fra"` → `"en"`, running English PII over French and reporting full coverage). |
| **C5 / C6 — FIPS crypto** | C5 has a **data-loss trap**: the ciphertext envelope records no algorithm, so naively honouring the setting orphans every existing ciphertext. C6 must fail closed without breaking boot. |
| **J4 — Tier-4 test triage** (only if you reach it) | Requires reading 92 tests and judging "is the outgoing request the contract?" — **~half are detector false positives**, and the obvious repair makes them strictly worse. |

### How to dispatch — and how to stay safe doing it

⚠️ **ONE subagent at a time. Wait for it to finish before dispatching another or editing anything
yourself.** `git commit` and `pre-commit` stash the **entire** worktree regardless of pathspec
(issue #434) — concurrent writers in one checkout destroy each other's work. **You own the commit;
the subagent only edits.**

```
Agent(
  subagent_type: "general-purpose",
  model: "opus",
  description: "<task id> <short title>",
  prompt: """
    Implement <TASK ID> from /home/superdave/.claude/plans/checkout-the-master-branch-async-mccarthy.md
    Read that task in full — it has bug, evidence, root cause, fix, traps, tests, done-when.
    Also read .claude/handoff-env-audit.md for what NOT to redo.

    Repo: /mnt/nvm/repos/transcribe-app, branch fix/audit-remediation. Dev stack is up.

    RULES
    - Implement the fix AND its red test AND the paired control. Watch the test FAIL first in a
      `git archive HEAD` tree — never revert files in the shared checkout.
    - DO NOT COMMIT and DO NOT run pre-commit. I own the commit. Leave the working tree edited.
    - Do not touch files outside this task's scope.
    - `rg -r` is --replace, NOT recursive — use `rg -n`. Never pipe a verification search
      through `head`.
    - Never pkill/kill -9/reset a GPU. Do not delete .env or dev data.
    - Honour every ⚠️ trap in the task. They are measured, not theoretical.

    When done, report: files changed, the test names, the red-then-green evidence, and anything in
    the plan's task you concluded was WRONG.
  """
)
```

**After it returns:** run the health gate yourself, review the diff, then commit. If the subagent's
change fails the gate, **revert it** and record why — do not push forward on a red gate.

**Everything else — groups 2, 4, 6, 7, 8, 9, 10 and the audit loop — do yourself.** They are
mechanical against explicit instructions, and delegating them costs more than it buys.

---

## 📋 ORDER OF WORK — one commit per group

| # | Group | Tasks | Watch out |
|---|---|---|---|
| 1 | **Security** | **A1** SSRF · A2 quarantine · A3 retry ceiling · A4 admin inversion | A1 is the only finding an unprivileged remote user can trigger. **Smoke after.** |
| 2 | **Data loss + user-visible** | **B1** storage-delete · B2 imohash · **B7** duration · **G1** unbounded retry loop · **G2** mic left open | B1 has a `inspect.getsource` test that pins 5 literal substrings. **Smoke after.** |
| 3 | **Redaction fails open** | B3 → B4 → B5 → B6 | ⚠️ **B3 first** — B5's fail-closed branch is incoherent otherwise. **Smoke after.** |
| 4 | **Inert controls** | C1 · C2 (+ stale compose comment) · C3 · C4 · C10 SAML mapping · E5 | The #567 overlap |
| 5 | **Config contract** | C5 · C6 · C7 · C8 · **K2** | K2 is the guard that stops it rotting |
| 6 | **Env residual** | **C9 phase 2** · verify + file the 3 orphaned `Settings` fields on #567 · **K3b** two skill improvements | ⚠️ **Read `.claude/handoff-env-audit.md` FIRST.** Phase 1 (template 1937→200) is **done — do not redo** |
| 7 | **Frontend** | G3–G10 · H1–H7 | G8 needs the widen step first; G9 needs the split first. **Smoke after.** |
| 8 | **Deletions** | E1 · E3 · E4 · E6 · E7 · E8 | ⚠️ **E2 default is KEEP** (6 test modules import it). E8 needs its list enumerated first. **Highest risk of breaking the app — health gate twice.** |
| 9 | **Docs** | L1–L8 | Cheap; clears the stale warnings |
| 10 | **Tests** | J1 · J2 · J3 | ⚠️ Whole-tree: `audit-tests` blocks **every** commit while any finding is open |

**Not today:** #566 env→UI (C12 — separate project, worthy) · D-group performance (needs measured
before/after) · I1 translations (needs a native speaker) · ~~#559~~ **dropped, David accepts the Perl
CVEs**; #415 stays **open** as the accepted-risk ledger.

---

## ⚙️ Working constraints

- **All GPUs available** for building and testing — today-only. Do **not** write it into config or
  docs, and it is not a reason to leave `GPU_CLUSTERING_DEVICE` unfixed.
- **Work directly on the dev instance.** No `--fresh` stack needed.
- **E2E must not persist changes to dev data** — upload tests delete what they create; edit tests use
  the cancel path.
- **Use `./opentr.sh`, never bare `docker compose`** — the overlay chain decides which DB and storage
  you attach to.
- **One writer commits per checkout.** `git commit` and `pre-commit` stash the **entire** worktree
  regardless of pathspec (issue #434). If you fan out agents, wait for every writer to finish, then
  make one clean commit pass.
- **`.env` is gitignored — no git safety net.** Back it up; prove any change with a resolved-value
  diff, never by assertion:
  ```bash
  dump() { bash -c 'set +u; set -a; . "$1" >/dev/null 2>&1; set +a; env|sort' _ "$1" | rg '^[A-Z_]+='; }
  diff <(dump .env.backup) <(dump .env)
  ```

## 🔬 Evidence standard

- **Watch every test fail first**, in a `git archive HEAD` tree — never by reverting files in the
  shared checkout.
- **Every red test needs its paired control.** Without one, "reject everything" (A1, C6) and "return
  everything" (B5) both pass.
- A green run you cannot attribute to your own code is **not** a measurement — check the test names
  in the output match what you just wrote. A suite can pass having tested the *old* code inside a
  pre-commit stash window.
- `rg -r` is `--replace`, **not** recursive — use `rg -n`. **Never** pipe a verification search
  through `head`.

## 🚫 Do not re-litigate — already decided

E2 default is **KEEP** · #559 is **dropped** · C12 is **not today** · the seven env-only decisions in
C9 stay env-only · the four false positives in `handoff-env-audit.md` are **not** to be re-reported
(`PROXY_*` dynamic, `UPLOAD_DIR` unreachable, 4 COMPOSE-BARE in vendored gitignored code, and the
deleted "quoted examples" detector).

---

## 📝 End-of-day deliverable

Keep `.claude/progress-2026-08-24.md` updated as you go: task ID → commit SHA → what you measured
→ anything skipped, with the reason. Finish it with:

- Which groups completed / partial / not started
- **Auditor before→after numbers** (the proof fixes landed)
- **Test counts before→after** — backend, frontend, `svelte-check`
- **Functional smoke result** — say it explicitly: did a real file **transcribe end to end**, and did
  **chat / summarization / topics** work against a real GPU-backed LLM? "Tests passed" is not an
  answer to this question.
- Anything you found that is **not** in the plan
- Anything in the plan you concluded was **wrong** — say so plainly; several findings have already
  been refuted this way and that is a success, not a failure
- The proposed PR title and body, ready for review

**Push the branch. Do not open the PR.** Branch pushed + summary written = done.

---

## ⛔ SEPARATE — v0.5.0 is blocked, and it is NOT your task today

Do not attempt the release. Recording it so you do not trip over it: the built images are **stale** —
`244d26ad` changed four baked `backend/app/` files *after* the `build` stage ran, so
`build → scan → rehearse` must be re-run before tagging. `tag`/`publish`/`promote`/`finish` are
David's call. Triage of the 20 unfixable CRITICALs is issue #415.
