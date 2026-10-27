# v0.6.0 completion plan (written 2026-10-25, revised 2026-10-27 with owner decisions)

A fresh agent must be able to run this standalone. **v0.6.0 ships Oct 31, 2026 (milestone due date
stands).** The v0.5.2 milestone is folded into v0.6.0: there is no separate 0.5.2 release.

**Gate 0 PASSED 2026-10-26 on master `7f245d5a`** (`pre-commit.yml` workflow_dispatch run
40319994293: pre-commit hooks, change detection, Backend Tests 28m, Frontend Unit Tests all
success). Four green docs/dev-dep PRs were merged after it (#1249, #1248, #1094, #1173), so Wave 1
branches from `2881b9dbc` and **Gate 0b** (a fresh full CI run) is required before Wave 3.

## Owner decisions (2026-10-27)
- All issues below are v0.6.0. Moved OUT to v0.7.0: **#1159** (early-ack worker-loss recovery,
  needs its own plan) and **#1279** (org redaction-floor screen).
- Moved INTO v0.6.0: #1002, #1235, #1185, #1184, #1180.
- #1002: fix the transformers/msgpack/setuptools CVEs WITHOUT a large backend rewrite. whisperx
  caps huggingface-hub<1.0 and gliner caps transformers; prefer the smallest change that clears
  the Trivy fixable-HIGH gate (bump what is unblocked, patch/override the capped path, or isolate
  the vulnerable path). If a bump is truly blocked, STOP and report; do not rewrite ASR/NLP code.
  A recorded `--force-scan "reason"` at release is the owner's call, not the lane's.
- Master is NOT tested on every push. Full CI on master runs only when needed:
  `gh workflow run pre-commit.yml --ref master`. Master already refuses force-pushes and deletions
  and requires 4 status checks plus a PR review. Do not add a push trigger.
- Branch `feat/doc-ingestion` is KEPT as a reference for a future documents-ingestion script (out of
  date vs master, never merge it). Do not delete it.

## Open work (all v0.6.0)

| Issue | What | Plan source | Lane |
|---|---|---|---|
| #1280 | Privacy WP2-WP7: enforcement call sites, org-admin API + screen, user settings, gender notice + off switch, docs | `v060_privacy_controls_plan.md` (WP1 merged as #1300) | D1 then D2 |
| #1281 | Platform act-as grant matrix + tests (governance plan WP-0) | `v060_org_content_governance_plan.md` | A |
| #1002 | 5 HIGH CVEs (see decisions) + curate dependabot #1314/#1316 into one change | issue body | B |
| #1305 | `test_slow_resolver_does_not_stall_the_event_loop` flake on 2-CPU runners | issue body | B |
| #1210, #1207 | chat settings tabs keyboard a11y; user ASR provider disable removes local pinning | issue bodies | C1 |
| #991, #866, #1235, #1184, #1307 | sections_budget; auth_type allowlist; AWS Transcribe explicit bucket (+ upgrade note); native-diar fallback visible/strict; held-file edit audit + tag-name leak | issue bodies | C2 |
| #1185 | Superseded in_progress task-row sweep + DB guard; takes migration **v448** | issue body | E |
| #1306, #1308, #1309 | live checks: deletion-residue seeds, notification flow on real workers (closes #1313), reranker/DOCX live checks | issue bodies | Wave 3 |
| #1180 | Diarization 4-5x ASR time: PROFILE, publish findings, land only a low-risk win | issue body | Wave 3 (needs GPU, quiet host) |
| #1282 | Browser verification, light/dark/ar | checklist below | Wave 3 |
| #1293 | filelock pin DONE; libbrotli1 fixed by the release rebuild (`APT_SECURITY_REFRESH`) + Trivy | n/a | release step |
| #1310 | regenerate roadmap.json, re-check docs-site | issue body | last |
| #1313 | Gender row stuck on Running; likely fixed by #1294; closes when #1308 shows it finishing as skipped | n/a | via #1308 |

## Waves (4 days: Oct 27 -> Oct 31)

**Wave 1 (start now, parallel, one worktree per lane)** lanes A, B, C1, C2, D1, E. Disjoint files
except the shared surfaces (12 locale files, `backend/openapi.json`, CHANGELOG, nested CLAUDE.md,
seam docs); the integrator resolves those at merge. Privacy D1 starts immediately because it is the
critical path. Migrations: only E adds one (v448); privacy needs none after v445. If a lane
believes it needs a migration, it re-derives the head and reports to the integrator first.

**Wave 2:** D2 = privacy WP6-WP7 (user settings, gender notice with off switch, docs, 12 locales),
starts when D1 is merged to the integration branch. Integration PR(s) merge with merge commits
(never squash) once "Backend Tests (pytest)" shows PASSED.

**Gate 0b:** `gh workflow run pre-commit.yml --ref master` on the integrated head; no writers meanwhile.

**Wave 3 (no writers active):** `./opentr.sh start dev --fresh master`; `./scripts/run-integration-tests.sh`,
e2e smoke, #1306/#1308/#1309, #1282 checklist, then #1180 profiling on GPU 1 + idle GPU 2.

**Release:** `./scripts/release.sh preflight 0.6.0` then `run 0.6.0` (rehearse needs the live
stack stopped and a HuggingFace token: ask the owner). Reset any stale ledger first. Then message the
cloud session with the final SHA; the owner types "go" there himself.

## Lane rules (read before starting)
- Create the worktree from the MAIN checkout: `git worktree add .claude/worktrees/<lane> -b <branch> origin/master`.
  Work and commit ONLY in your worktree. Do not push; the integrator pushes. No `--no-verify`.
- **Your tests cannot hit the live stack with your code**: the running `otfresh-master` stack
  bind-mounts the MAIN checkout. Use in-process unit tests; stack-level proof is Wave 3. Before
  trusting a pytest run, check `import app; app.__file__` resolves inside YOUR worktree (the shared
  `backend/venv` must not be editable-installed from the main checkout).
- Watch every new test fail first (red-check on a `git archive HEAD` tree under `/tmp/ot-redcheck.*`).
- Host load: run targeted tests only, never the full suite; no Docker stacks of your own.
- Finish with: `python3 scripts/audit-tests.py tests` at 0 findings; both pre-commit tiers
  (`scripts/safe-precommit.sh run --all-files` and `--hook-stage pre-push`); report the branch name,
  commit SHAs, tests added, and anything you could not verify. Say "not verified" rather than guess.

## Rules for every lane
No emoji in the UI; every user string in all 12 locales (real translations, `ar` RTL checked);
bundle budget never raised; regenerate `backend/openapi.json` for endpoint changes
(`cd backend && venv/bin/python ../scripts/generate-openapi.py --write`); every Celery task needs
a route and `include=` entry; falsifiable tests; mypy checked with a COLD cache in the hook's own
env (a warm cache hid two errors on PR #1304); docs-site, README, nested CLAUDE.md and CHANGELOG
updated with the change; conventional commits; no `noqa`/`type: ignore`/allowlist entries to clear a
finding; never kill processes; never read `.env`; security findings go to the owner privately.

## #1282 checklist
Light + dark + `ar` (RTL) on: upload tray (no emoji), notification card finishing tier, search page
(query bar width, Details, tags/collections, caret), chat scrollbar, About dialog + stale-build
banner, export menu (txt/md/docx/pdf), legal modal/wizard, org collections/audit/holds panels,
reranker status row in admin stats. Record screenshots; fix findings in the same wave.

## Hazards learned Oct 25
- A merge watcher must require Backend Tests to be PASSED (a rerun briefly shows no failing and no
  pending checks, and three PRs merged before their tests finished).
- CI installs unpinned transitive deps fresh: preshed 3.0.14 broke spaCy/Presidio overnight.
- Never resolve conflict markers with a blind replace of `=======`; it also matches comment banners.
- A branch cannot take a seam version already used on master (seam is 12; check before numbering).
- Only one writer per checkout may commit; lanes use their own worktrees for that reason.
