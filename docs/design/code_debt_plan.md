# v0.7.0 code-debt plan: quality block on ALL files, guard gaps, then measured efficiency

Plan only. Written 2026-11-08 against `cleanup/wave-c-merge` @ `abd0f1207`, revised the same day
for the owner's final decisions. Nothing here is implemented. A fresh implementation agent with
no conversation history must be able to run any lane from this file alone. The lead commits it
as `docs/design/code_debt_plan.md`.

## 0. Owner decisions (2026-11-08, final: do not re-ask)

1. **Nothing slips.** Every item ships in v0.7.0: all broad-excepts, all hidden `type: ignore`
   errors (tests too), ORM calls out of the 129 routers, all 11 shell splits, style literals onto
   design tokens, duplicate code, frontend and backend. The release date moves if it must.
2. **Legit broad-except handlers are brought to industry standard too**: narrow the exception
   type, log with the traceback, record the failure (section 1.2).
3. **`opentranscribe.sh` and `setup-opentranscribe.sh` are NOT split** (correction, same day).
   User-downloaded installers are one self-contained file by industry practice: Homebrew
   (`curl -fsSL .../Homebrew/install/HEAD/install.sh`), nvm
   (`curl -o- .../nvm-sh/nvm/v0.41.0/install.sh | bash`), rustup (`curl ... | sh` runs the
   `rustup-init.sh` shell installer), and Docker's `get.docker.com` (served from the single
   script in `docker/docker-install`). There is no bundle step. Both files are cleaned
   internally instead (shellcheck clean, small named functions, clear sections, no dead code) and
   go on the file-size allowlist with that reason (lane L16, section 4.5). The other 9 shell
   scripts (internal tooling) are still split by concern.
3a. **Focus**: backend code, frontend code and test files carry the weight of the swarm; the
   internal shell splits run in the fourth (Q) slots and late waves.
4. **`scripts/cleanup-test-data.py` is split.**
5. **Order:** (a) ALL original v0.7.0 milestone code finishes first (scale lanes B4-B9, lane P
   profile, D2 indexes, the live checks); (b) then a swarm of Haiku and Sonnet cleanup lanes,
   3 at a time, 4 when the host is quiet, one sweep per wave; (c) then evaluation (full load
   pass, sweeps, app tests), then release.
6. Inside the swarm the owner's quality order holds: **format/lint, dead code, imports, file
   length, complexity, readability**, on ALL files, ending with every guard green in
   `--all-files` mode with an empty baseline or a legit-only, reasoned allowlist.
7. Efficiency fixes come from measurement only (section 5).

Note on method: section 1's measurements used a throwaway scratch git repo outside every project
checkout, committed once with hooks and signing off. That is acceptable only for that scratch
repo. **No lane may ever do it**: lanes commit with hooks on and signing as configured.

---

## 1. Measured state (one sequential run each, 2026-11-08, HEAD `abd0f1207`)

Scratch copy: `git archive HEAD | tar -x` into `~/ot-cleanup-work/plans/measure/head/`
(the guards enumerate `git ls-files`, hence the scratch repo). Tools: the guard-pinned
`/mnt/nvm/repos/transcribe-app-wt-cleanup-wavec/backend/venv/bin/{ruff 0.17.3, vulture 2.16, mypy}`.
Raw outputs: `~/ot-cleanup-work/plans/measure/`. Helper scripts: `~/ot-cleanup-work/plans/tools/`.

| Ratchet / count | Value | Command |
|---|---|---|
| Broad except (BLE001) | **1,086** in 389 files | `ruff check --isolated --ignore-noqa --no-cache --select BLE001 backend/app scripts backend/alembic` |
| ...by area | services 472, tasks 235, scripts 104 (+5 in `backend/app/scripts`), api 80, core 49, utils 46, transcription 40, auth 35, startup 15, main 4, db 1 | grouped by path (`measure/ble_classified.json`) |
| Complexity >15 | **33** (15 `backend/app`, 18 `scripts/`) | `ruff ... --select C901 --config lint.mccabe.max-complexity=15` |
| Vulture | **165** (72 function, 68 method, 25 class) | `python scripts/guards/check_vulture.py` |
| Suppressions guard | **4** (bare-reason `# noqa: C901`) | `python3 scripts/guards/check_suppressions.py` |
| `type: ignore` comments | **237** (app 121, tests 108, scripts 8) | `rg -c --type py '#\s*type:\s*ignore' <dir>` |
| Real mypy errors those ignores hide | **261** in 157 files (app 150, tests 110, other 1): arg-type 102, attr-defined 64, assignment 37, no-any-return 23, union-attr 9, return-value 6, call-overload 6, misc 4, method-assign 3, typeddict-item 2, list-item 2, other 3 | copy the tree, strip every ignore (`sed -E 's/#\s*type:\s*ignore(\[[^]]*\])?//'`), `mypy --check-untyped-defs --ignore-missing-imports backend` from the copy root. Output `measure/strip.Iuds/mypy_stripped.txt`. Root config has `warn_unused_ignores = true`, so every remaining ignore is a used one |
| slowapi `response: Response = None` | 15 (+1 `request: Request = None`) | `rg 'Response = None'` |
| api_boundary | 148 keys / 513: `orm_call_in_api` 129 files, `fastapi_import` 19 | baseline |
| raw_env | 64 files / 126 reads | baseline |
| file_size over hard cap | Python 4, TS/JS 1, Shell 11, Svelte script 4, CLAUDE.md 0 | `python3 scripts/guards/size_report.py` |
| eslint | 773 (no-restricted-imports 201, no-explicit-any 158, require-each-key 112, no-navigation-without-resolve 73, no-useless-assignment 70, infinite-reactive-loop 41, no-at-html-tags 29, no-unused-expressions 26, no-unused-vars 19, prefer-svelte-reactivity 19, prefer-const 16, other 9) | baseline |
| eslint rules not enabled today | complexity>15 **46** (39 files); max-depth>4 **4**; max-lines-per-function>150 **31** (28 files) | `eslint --no-cache --rule '{"complexity":["warn",15],"max-depth":["warn",4],"max-lines-per-function":["warn",{"max":150,"skipBlankLines":true,"skipComments":true}]}' --format json src` (read-only, wavec `frontend/`) |
| no-unused-vars in `.svelte` | works on most files and finds real unused names (FileUploader 5, +page 6, FilterSidebar 3, SettingsModal 1) but **still crashes** on `src/components/Navbar.svelte` (`@typescript-eslint/eslint-plugin/dist/rules/no-unused-vars.js:573`, `getDefinedMessageData`) | same, rule forced on |
| knip | 48 | baseline |
| style_literals | 1,944 keys / **12,071** (spacing 4,734, colour 2,041, font-size 1,630, ...) | baseline; method in `docs/design/frontend_styling_layer_plan.md` |
| migration_safety | 157 keys / 223 | baseline |
| process_ids / madge | 6 / 0 | baselines |
| fail-open audit | 270 raw, **194 reported, 39 critical-path** | `python3 ~/.claude/skills/fail-open-audit/scripts/audit-fail-open.py backend/app --json` |
| a20 leftovers | 38 of 68 bandit-unused `# nosec` still present; 201 bandit "Test in comment" warnings; noqa_left: only the 4 C901 | re-check of `~/ot-cleanup-work/a20/*` |
| duplicate code | not measured (no tool yet; L01b measures) | — |

### 1.1 Broad-except shapes (AST classifier `tools/ble_classify.py`, all 1,086)

Ruff's BLE001 does **not** flag a handler that re-raises (`raise`, `raise e`, `raise X from e`)
or logs with `exc_info` / `logger.exception` (ruff docs; confirmed with a probe file). So every
one of the 1,086 swallows the exception, and 197 of their log calls are at `debug`.

| Shape | Count |
|---|---|
| Records failure state (status, rollback, notify, retry, failure record) | 347 |
| Return None/False/[]/0/"" (fail-open candidates; 39 critical-path) | 320 |
| Log without traceback, fall through | 170 |
| Return a computed fallback / error payload | 140 |
| Per-item isolation inside a loop | 46 |
| Other fallback assignment, no log | 40 |
| Silent pass/continue | 16 |
| Raise a new exception without `from` | 7 |

### 1.2 The standard every handler is brought to (owner decision 2)

Python tutorial ("Handling Exceptions"): be "as specific as possible with the types of exceptions
that we intend to handle", and "the most common pattern for handling Exception is to ... log the
exception and then re-raise it". Python logging docs: `Logger.exception()` logs at ERROR with the
exception info and "should only be called from an exception handler"; any level method takes
`exc_info=True` to attach the traceback.

Apply in this order, per handler:

1. **Narrow** to the exceptions the try body can raise and the code knows how to handle
   (`S3Error` with a code check, `redis.exceptions.ConnectionError`, `requests.RequestException`,
   `sqlalchemy.exc.OperationalError`, `OSError`, `ValueError`, `json.JSONDecodeError`,
   `cryptography.exceptions.InvalidTag` ...). Everything else propagates.
2. **Log with the traceback**: `logger.exception(...)` for an error; `logger.warning(..., exc_info=True)`
   for an expected degraded path. Keep the existing message text; never log secrets (the message
   already holds the exception text in 849 of the handlers, so the traceback adds no new data class).
3. **Record the failure where the caller or operator sees it**: task row FAILED, a failure count
   in the batch result, a metric, a distinct return value, or a 5xx/503. "Found nothing" and
   "could not look" must not be the same value (fail-open-audit skill).
4. **True top-level boundaries** (a Celery task body, a per-item isolation loop, a startup step,
   an ASGI middleware) legitimately catch broadly, because their job is to stop one failure from
   taking down the worker or the batch. There the standard is: catch `Exception` (never
   `BaseException`/bare), `logger.exception`, record the failure, and re-raise if the framework
   should see it (Celery marks FAILURE and applies its retry policy on a raised exception). Ruff
   exempts that shape by rule, so the BLE001 baseline still reaches 0 with **no** allowlist.

Calibration sample (the broad-except lanes report their verdict on these first):

| Handler | Expected verdict |
|---|---|
| `backend/app/auth/password_history.py:138` returns False | fail-open if False means "not reused": fix |
| `backend/app/auth/rate_limit.py:52` returns False | a Redis outage must not disable limiting: fix |
| `backend/app/utils/encryption.py:233,253,384,412` return None | decrypt failure vs "no value": fix (critical) |
| `backend/app/api/endpoints/files/complete_upload.py:181` returns None | upload path, fix |
| `backend/app/services/erasure_ledger_service.py:198,233` return None | erasure evidence, fix |
| `speakers_merge.py` cache invalidation, `logger.debug("non-critical")` | narrow to the Redis error, `exc_info=True`, keep soft |
| `chat/redactor_digest_plans.py` per-hit "withholding the section" | fail-closed already; narrow + `logger.exception` |
| `tasks/*` bodies that set FAILED and notify | boundary: `logger.exception`, keep the record, re-raise where Celery should retry |

---

## 2. Guard gaps: how debt got through, and the fix

| Gap | How it let debt through | Fix (lane) | Docs |
|---|---|---|---|
| G-a `no-unused-vars` off for `.svelte` | disabled because `@typescript-eslint/no-unused-vars` crashes on svelte-eslint-parser nodes; measured: still crashes on Navbar.svelte, works elsewhere; svelte's compiler only warns on unused exports/props | L01b: reduce the crash to a minimal construct, check upstream trackers and changelogs, bump if fixed and enable the rule for `.svelte`. Else enable `noUnusedLocals`/`noUnusedParameters` for the svelte-check run and add `check_svelte_unused.py` (parses `svelte-check --output machine-verbose`, codes 6133/6192/6196/6198, per-file ratchet). typescript-eslint recommends its rule over the TS options, so the eslint route wins wherever it does not crash | typescript-eslint `no-unused-vars`; svelte-check README; eslint-plugin-svelte user guide |
| G-b no frontend complexity rule | none of `complexity`, `max-depth`, `max-lines-per-function` is configured | L01b: `complexity` 15 (eslint default 20; 15 matches the backend), `max-depth` 4, `max-lines-per-function` 150 (skip blanks/comments), counted by `check_eslint_ratchet.py`; one-time baseline extension for the 81 new keys with a written reason | ESLint rule pages |
| G-c a reasoned `type: ignore` hides real errors | `warn_unused_ignores` only catches UNUSED ignores; `check_suppressions.py` only demands a code; one `[arg-type]` hides any number of errors on its line (rate_limit.py:220 hides 7) | L01a: `type-ignore` count ratchet in `check_suppressions.py` (keyed `path::code`, to 0); `allowlists/type_ignore.txt` only for a genuine stub gap with an upstream URL; push-stage + CI `check_hidden_mypy.py`: strip every ignore in a temp copy with `tokenize`, run mypy, require the hidden (path, code, count) multiset to equal the allowlist exactly. Add `plugins = ["pydantic.mypy"]` | mypy error codes; mypy config (`warn_unused_ignores`); pydantic mypy plugin |
| G-d no duplicate-code detector | nothing measures clones | L01b: `jscpd` (MIT; Python, TS, Svelte) as push-stage ratchet keyed by file; D1/D2 burn it down. pylint `duplicate-code` rejected (Python only) | jscpd README |
| G-e1 two ruff configs disagree | `backend/pyproject.toml` C90 at 16 (a function at 16 needs a noqa while the ratchet baselines it at 15); root config ignores B904 for `scripts/` | L01a: drop C90 from `backend/pyproject.toml` (ratchet is the single owner); remove root B904 ignore, fix with `raise ... from` | ruff settings, C901/B904 |
| G-e2 mypy never sees `scripts/` | hook `files: ^backend/` | L01a: second mypy hook for `scripts/(guards|lib|audit_tests|model_download|cleanup_test_data)/` | pre-commit config |
| G-e3 broad_except keyed per FILE | a fix and a new swallow in one file cancel out | L01a: re-key `path::qualname#n`; prove sum before == count after | `_baseline.py` |
| G-e4 baselines double as permanent allowlists | "legit forever" entries sit beside debt | L01a: `scripts/guards/allowlists/<guard>.txt` (same TSV, reason passes `is_real_reason` and starts with a declared class tag; stale entry fails; `--tighten` never edits it); size report prints debt and allowlisted separately | `_baseline.py` |
| G-e5 bandit nosec reasons parse as test ids | `# nosec B603 fixed argv` -> 201 warnings that drown real ones | L02: reason first (`# fixed argv  # nosec B603`); drop the 38 that suppress nothing | bandit nosec docs |
| G-e6 eslint rules at `warn` | a new warning hides behind a dropped count elsewhere | every rule whose count reaches 0 flips to `'error'` | ESLint configure rules |
| G-e7 single-file installers vs file-size cap | (decision 3) the two user-downloaded installers stay one file by design | L16: `allowlists/file_size.txt` class `single-file-installer`, reason citing the Homebrew/nvm/rustup/get.docker.com practice; the guard still fails if either grows past its allowlisted line count | installers' docs (section 10) |
| G-e8 test code outside the ratchets | vulture, complexity and broad-except guards exclude `backend/tests`; jscpd scope did not include it | L01b adds `backend/tests` to the jscpd scope; lane T1 cleans dead test helpers/fixtures and duplicate test helpers; L21 removes test `type: ignore`s | pytest fixtures docs |

`migration_safety` (157): **stays, confirmed.** A released Alembic revision has already run on
user databases; editing it changes nothing for an upgraded database and makes a fresh install
diverge from an upgraded one (`backend/alembic/CLAUDE.md`). L01a moves those entries to
`allowlists/migration_safety.txt` (`applied-history`); the baseline ends empty, so any new
migration finding fails.

---

## 3. Lane rules (every brief repeats them in its footer)

- One lane = one agent in its own worktree `/mnt/nvm/repos/transcribe-app/.claude/worktrees/<lane>`
  on branch `debt/<lane>`, created by the lead from the integration tip. Commit there with hooks on
  and signing as configured, one commit per concern. Never `--no-verify`, never disable signing,
  never push, never merge, never touch another checkout, never start or use the live dev stack
  (DB tests: `scripts/test-in-worktree.sh -- <pytest args>`).
- `~/ot-cleanup-work/briefs/STANDING_LANE_RULES.md` applies (complete fully, docs as you go,
  CHANGELOG under Unreleased, red on a `git archive HEAD` tree in `mktemp -d -t ot-redcheck.XXXXXX`,
  audit-tests 0 open, per-criterion report).
- Targeted tests only; static checks on touched files; never full suites or `--all-files`.
- No suppressions of any kind. Baselines only shrink (`--tighten`, committed in the same commit).
- Read the official docs for each package touched (pinned version), cite them in the report.
- Batch each `backend/app` file's edits into one write per commit.

**File ownership:** a file belongs to at most one lane per wave. Ordering rule: inside one FILE
the owner's order is strict (split before decompose before type/except fixes), because each step
must work on the final layout. Lanes on disjoint files may run different steps in the same wave;
they do not interact.

**Baseline merge recipe (lead):** sorted TSV, disjoint keys, so most merges are clean. On a
conflict: take either side, run the guard with `--tighten` on the merged tree, require exit 0
(`tighten` only lowers values and drops stale lines). Never edit a value upward. Each
`allowlists/*.txt` has one owning lane per wave.

---

## 4. The swarm

**M** = the day the original milestone code is merged and swept green (scale-w2 tag with B4-B9,
lane P profile, D2, live checks). Estimate **M = Mon Nov 16**; every date below moves with M.
Waves are about 2.5 working days, overlapping: wave N+1 codes while wave N's sweep runs.
"(Q)" = fourth lane, only when the host is quiet (1-min load under 25% of nproc).

### 4.1 Wave table

| Wave | Dates (M = Nov 16) | Step | Lanes (model) |
|---|---|---|---|
| W0 | Nov 17-19 | guards, mechanical | L01a (S), L01b (S), L02 (H), (Q) L16 installers cleaned in place (S) |
| W1 | Nov 19-23 | lint, dead code, imports | L03 (H, fe), L04 (S, be), L05 (S, fe), (Q) L21 test type-ignores (H, tests) |
| W2 | Nov 23-25 | file length | L07 (S, be), L08 (S, scripts .py), L09a (S, fe), (Q) L09b (S, fe) |
| W3 | Nov 25-30 | complexity | L10 (S, be), L11 (S, be), L12 (S, fe), (Q) T1 test code (S, tests) |
| W4 | Nov 30-Dec 2 | complexity, lint judgement, readability | L13 (S, be), L14 (S, be), L06 (S, fe), (Q) D1 duplicates backend+tests (S) |
| W5 | Dec 2-4 | readability | B1 (S), B2 (S), S0 styling infra (S), (Q) D2 duplicates frontend (S) |
| W6 | Dec 4-8 | readability | B3 (S), B4 (S), S1a styling codemods (H), (Q) A7 fastapi imports + raw env (S) |
| W7 | Dec 8-10 | readability | B5 (S), B6 (S), S1b styling codemods (H), (Q) L20 scripts complexity (S) |
| W8 | Dec 10-14 | readability | B7 (S), B8 (S), S2a (S), (Q) A1 (S) |
| W9 | Dec 14-16 | readability | B9 (S), B10 (S), S2b (S), (Q) A2 (S) |
| W10 | Dec 16-18 | readability | A3 (S), A4 (S), S2c (S), (Q) B11 scripts broad-except (S) |
| W11 | Dec 18-22 | readability | A5 (S), A6 (S), S2d (S), SH shell review in place (S) |
| W12 | Dec 22-24 | style | S2e (S), S3a (S), S3b (S) |
| W13 | Dec 28-30 | style | S3c (S), S3d (S), S3e (S) |
| W14 | Dec 30-Jan 4 | style close-out, all-files gate | S3f (S), S4 (H), S5 (S), then the final all-guards `--all-files` check |
| Eval | Jan 5-11 | evaluation | C1 final load pass (#37, ~8 h machine, night window), C3 mutation, C4 integration + GPU gate (GPU 1, +2 if idle, never 0), rehearse (all four scenarios, incl. D for the split downloader), C5 review, C6 docs, C7 browser/locale pass; small fixes in one lane |
| Release | Jan 12-13 | release | `./scripts/release.sh run` stages, owner approvals |

**Release estimate: about Tue Jan 12, 2027.** Best case (4 lanes in every wave, M on time) is
about Jan 4; if most waves run 3 lanes, the (Q) column becomes three extra waves and the date is
about Jan 20. Total work: about 114 lane-days in 59 lanes (section 8).

If a wave's (Q) lane cannot run, it moves to the next wave's (Q) slot; nothing is dropped.

### 4.2 Lane ownership

| Lane | Model | Owns (exclusive for its wave) | Size (lane-days) |
|---|---|---|---|
| L01a Python guards | sonnet | `scripts/guards/{_baseline,check_ruff_ratchet,check_suppressions,check_vulture}.py`, new `check_hidden_mypy.py`, `scripts/guards/allowlists/` (new), `baselines/{broad_except,migration_safety,suppressions}.txt` + new `type_ignore.txt`, `backend/pyproject.toml` (ruff select), root `pyproject.toml` (ruff ignore, mypy plugin), `.pre-commit-config.yaml` python guard block + mypy hooks, `.github/workflows/pre-commit.yml`, `backend/tests/unit/test_guard_*.py` for those guards, the 8 `scripts/` ignores, `scripts/` B904 fixes | 2 |
| L01b frontend guards | sonnet | `frontend/eslint.config.js`, `frontend/tsconfig*.json` (svelte-check route only), new `scripts/guards/{check_svelte_unused,check_duplicates}.py`, `baselines/{eslint,knip}.txt` + new `svelte_unused.txt`, `duplicates.txt`, knip config, `frontend/package.json`/lock (jscpd), `.pre-commit-config.yaml` frontend guard block, their tests | 1.5 |
| L02 mechanical | haiku | 16 slowapi signature lines, 6 process_ids lines + baseline, every `# nosec` line in `backend/`+`scripts/` (.py), the 7 raise-without-`from` handlers | 1 |
| L03 eslint mechanical + import aliases | haiku | files whose findings are only `no-restricted-imports`, `prefer-const`, `no-useless-mustaches`, `no-empty`, `svelte/no-unused-svelte-ignore`, `svelte/no-navigation-without-resolve` (lead lists, minus L05's) | 1 |
| L04 backend dead code | sonnet | files of the 165 vulture entries, their tests, `vulture_whitelist.py`, `baselines/vulture.txt` | 2 |
| L05 frontend dead code | sonnet | knip's 48, svelte-unused findings, `no-unused-vars` (19) and `no-unused-expressions` (26) files | 1.5 |
| L07 backend splits | sonnet | `core/config.py`, `services/llm_service.py`, new modules (4.4) | 1 |
| L08 script splits | sonnet | `scripts/download-models.py`, `scripts/cleanup-test-data.py`, `scripts/download-models.sh`, `release-manifest.txt`, `backend/Dockerfile.*` lines naming the downloader, tests loading either by path | 1.5 |
| L09a / L09b frontend splits | sonnet | a: `lib/services/uploadService.ts`, `components/FileUploader.svelte`; b: `components/FilterSidebar.svelte`, `components/SettingsModal.svelte`, `routes/+page.svelte`; new modules (4.4) | 1.5 + 1.5 |
| L16 installers cleaned in place | sonnet | `opentranscribe.sh`, `setup-opentranscribe.sh` (internal cleanup only, still one file each), `allowlists/file_size.txt`, their tests | 1.5 |
| SH shell review in place | sonnet | every `*.sh` (installers, `scripts/common.sh`, `opentr.sh`, build/release/e2e scripts): fix errors and issues in place, no split, `allowlists/file_size.txt` entry per file | 2 |
| T1 test code | sonnet | `backend/tests/fixtures/**`, `conftest.py` files, test helper modules (`backend/tests/**/helpers*.py`, `_*.py` support modules), frontend `src/test-mocks/**` and `*.test.ts` helpers flagged by vulture/knip/jscpd | 2 |
| L10 A22 | sonnet | `api/endpoints/asr_settings.py`, `services/asr/{aws,gladia,pyannote}_provider.py`, tests, `baselines/{complexity,suppressions}.txt` | 1.5 |
| L11 backend complexity | sonnet | the other 11 `backend/app` complexity functions' files (4.3) | 2 |
| L20 scripts complexity | sonnet | the 18 `scripts/` complexity functions' files | 1.5 |
| L12 frontend complexity | sonnet | files of the 81 new eslint complexity/depth/length findings | 1.5 |
| L06 eslint judgement | sonnet | files with `no-explicit-any`, `require-each-key`, `no-useless-assignment`, `infinite-reactive-loop`, `no-at-html-tags`, `prefer-svelte-reactivity`, other svelte rules (disjoint from L12 that wave) | 2 |
| L13 hidden mypy: api/schemas/main | sonnet | every `api/**`, `schemas/**`, `main.py` file with an ignore | 1.5 |
| L14 hidden mypy: rest of app | sonnet | `services/`, `tasks/`, `utils/`, `auth/`, `core/`, `transcription/` files with an ignore | 2.5 |
| L21 hidden mypy: tests | haiku | the 108 `backend/tests` ignores (110 hidden errors) | 2 |
| B1-B11 broad except | sonnet | see 4.6 | 11 x 2.5 |
| A1-A6 ORM out of routers | sonnet | see 4.7 | 6 x 2 |
| A7 fastapi imports + raw env | sonnet | the 19 `fastapi_import` files, the 64 `raw_env` files, both baselines + allowlists | 2 |
| D1 / D2 duplicates | sonnet | backend+scripts / frontend clone groups from L01b's report, `baselines/duplicates.txt` | 2 + 2 |
| S0-S5 styling | S1a, S1b, S4 haiku; rest sonnet | lanes of `docs/design/frontend_styling_layer_plan.md` (4.8) | 1 + 1.5 + 2 + 6 x 1.5 + 6 x 2 + 2 + 1.5 |

Before dispatch the lead runs `git worktree list` and removes from a lane any file another live
worktree holds.

### 4.3 Complexity targets

L10: `asr_settings.py::update_asr_config` 26, `aws_provider.py::AWSTranscribeProvider.transcribe` 43,
`gladia_provider.py::GladiaProvider.transcribe` 26, `pyannote_provider.py::PyAnnoteProvider.transcribe` 24.
L11: `files/complete_upload.py::complete_upload` 16, `files/media_delivery.py::download_stream` 16,
`prompts.py::get_prompts` 16, `users_self_service.py::update_current_user` 16,
`asr/google_provider.py::GoogleASRProvider.transcribe` 16, `opensearch_service/repair.py::rebuild_speaker_index` 30,
`opensearch_service/speaker_read.py::iter_speaker_embeddings` 17,
`profile_embedding_service.py::ProfileEmbeddingService.update_profile_embedding` 16,
`tasks/recovery.py::periodic_health_check_task` 16, `tasks/recovery_tasks.py::media_duration_backfill` 16,
`tasks/transcription/core.py::transcribe_gpu_task` 16.
L20: the 18 `scripts/` rows of `baselines/complexity.txt` (`audit_tests/{cli,scan,selftest}`,
`bench_parallel/{cli,console_reports,runner}`, `benchmark_upload_matrix`, `cleanup-test-data` (its
new package path after L08), `e2e_bench/{cli,reports,stages}`, `generate-sbom-report`,
`mutation-verify-apply`, `pyannote_direct/pipeline`, `rag_bench/cli`,
`session_lifetime_audit/{cli,scan}`, `vram-probe-diarization`).

### 4.4 New module layouts (Python, TS, Svelte)

Each split moves code verbatim (`git diff -M` shows moves), keeps the import paths callers use,
deletes the moved code from the original, and keeps no shim without an outside caller.

- **`backend/app/core/config.py`** (1,457, one `Settings` class). Follow the existing
  `settings_*.py` mixin pattern: `settings_security.py` (FIPS, password policy, rate limits,
  tokens, lockout, audit, banner, account expiry, session limits, SMTP, abuse intake: ~234-402),
  `settings_storage.py` (database, object storage: ~403-562), `settings_search.py` (OpenSearch,
  search/RAG and their validators: ~563-830), `settings_identity.py` (LDAP, OIDC, generic OIDC,
  OIDC admission, SAML, MFA, PKI: ~1009-1251), `settings_providers.py` (AI models, diarization,
  Bedrock, ASR, OpenSearch toggle, YouTube, watch sources: ~978-1008, 1252-1457). `config.py` keeps
  module constants, `is_relaxed_environment`, `resolve_ldap_search_filter`, API/env fields, CORS,
  the cross-cutting `model_validator`, the `settings` instance.
- **`backend/app/services/llm_service.py`** (1,098): `llm_transport.py` (`LLMTransportMixin`:
  endpoints, headers, pinned session, `_send_llm_request`, `_resolve_endpoint`, ~181-410) and
  `llm_factory.py` (`LLMFactoryMixin`: every `create_from_*`, user/system resolution, credentials,
  `_get_provider_config`, ~582-1072). `llm_service.py` keeps errors, `__init__`, the completion
  methods, Bedrock call, context manager, `is_llm_available`.
- **`scripts/download-models.py`** (1,367) -> `scripts/model_download/`: `ui.py`, `whisper.py`,
  `pyannote.py`, `nltk.py`, `embeddings.py` (sentence-transformers, chat reranker, speaker
  attributes), `opensearch.py`, `redaction.py`, `diar_native.py`, `manifest.py`, `groups.py`
  (`DOWNLOAD_GROUPS`, `GROUPS_WITHOUT_HF_TOKEN`); `download-models.py` keeps `main()`. Mount the
  package (`download-models.sh:363`), list each file in `release-manifest.txt`, update Dockerfile
  COPY lines; unit test that every `scripts/model_download/*.py` is in the manifest.
- **`scripts/cleanup-test-data.py`** (1,171) -> `scripts/cleanup_test_data/`: `settings.py`,
  `cutoff.py`, `shapes.py`, `finders.py`, `api.py`, `owners.py`, `planes.py`, `cli.py`; the script
  stays as the entry point (called by path from `release-tidy.sh`, `cleanup-test-leftovers.sh`).
- **`frontend/src/lib/services/uploadService.ts`** (1,400) -> `lib/services/upload/`: `queue.ts`,
  `transfer.ts` (beside `multipartUploader.ts`), `completion.ts`, `urlIngest.ts`,
  `persistence.ts`, `format.ts`; `uploadService.ts` keeps the `UploadService` facade and emitter.
- **`FileUploader.svelte`** (script 827): `lib/upload/wizardSteps.svelte.ts`,
  `lib/upload/uploadPreferences.ts`, `lib/upload/bulkExtraction.ts`, `lib/upload/submitUpload.ts`.
- **`FilterSidebar.svelte`** (750): `lib/gallery/filterData.ts` (fetches), `lib/gallery/filterChips.ts`.
- **`SettingsModal.svelte`** (982): `lib/settings/settingsSearch.ts`,
  `lib/settings/recordingSettings.ts`, `lib/settings/adminLoaders.ts`.
- **`routes/+page.svelte`** (1,362): `lib/gallery/fileMapUpdates.ts`, `lib/gallery/bulkActions.ts`,
  finish moving `buildQueryParams` into `lib/gallery/galleryQuery`.
- Svelte splits never touch `<style>` (styling plan D15) and are browser-checked light and dark.
- Mixed-concern files not over cap, split by the lane already in them (L06/L12 or the styling
  lane for that surface): `settings/PromptSettings.svelte` (1,598), `routes/login/+page.svelte`
  (1,544), `UserManagementTable.svelte` (1,461), `settings/LLMSettings.svelte` (1,377),
  `transcript/SpeakerEditorPanel.svelte` (1,355). Long single-job files stay.

### 4.5 Shell: installers cleaned in place, internal tooling split by concern

**User-downloaded installers stay single files** (decision 3; Homebrew `install.sh`, nvm
`install.sh`, `rustup-init.sh`, `get.docker.com`). L16 cleans them in place, keeping each a single
self-contained file and its download path in `release-manifest.txt` unchanged:

- `shellcheck` clean with no `disable` directives (fix the finding).
- Clear `# ===== <section> =====` sections in this order, matching the concerns already present.
  `opentranscribe.sh`: ref/URL resolution; environment checks; hardware detection; deployment
  pins; compose file selection (incl. diar-native); upgrade (backup advice, pull/down, phased
  restart); model download; access info; command dispatch. `setup-opentranscribe.sh`: output
  helpers; platform and GPU detection; prerequisite checks; project files and manifest download;
  `.env` creation and hardware settings; interactive prompts; model download and image pull;
  summary; `main`.
- Small named functions: decompose any function over ~60 lines (and the command dispatch
  `case` arms) into named steps; no behaviour change.
- No dead code: every function has a caller inside the file (check by name with `rg`, including
  callers in `opentr.sh`, CI workflows, docs and tests that invoke it by name), no unreachable
  branches, no commented-out code.
- File-size allowlist entry per file, class `single-file-installer`, reason: "user-downloaded
  installer kept as one self-contained file (industry practice: Homebrew install.sh, nvm
  install.sh, rustup-init.sh, get.docker.com); internally sectioned, shellcheck clean", value =
  its line count after cleanup, so it cannot grow unreviewed.
- Proof: `bash -n`; `shellcheck`; existing tests that read these scripts
  (`rg -l "setup-opentranscribe|opentranscribe.sh" backend/tests`) green; `--help`/`help` output
  and `opentranscribe.sh`'s compose selection identical to HEAD for a matrix of env inputs
  (add a test that sources the file with `OT_SOURCE_ONLY`-style guard if one exists, or runs the
  read-only subcommands); the rehearsal (fresh install, upgrade, lite) in evaluation is the
  end-to-end proof.

**Internal tooling is split by concern** (the remaining 9 over-cap scripts). Layouts:
- `opentr.sh` (dev only, modules sourced at runtime) -> `scripts/lib/opentr/`: `help.sh`,
  `env.sh`, `hardware.sh`, `overlays.sh`, `fresh.sh` (fresh_*, port probes, preflight_ports_or_die),
  `health.sh`, `data_paths.sh`, `build.sh`, `start.sh` (`start_app`, 982 lines, decomposed into
  named steps), `reset.sh` (`reset_and_init`, 520 lines, decomposed), `restart.sh` (restart/stop/
  remove/purge). Proof: `./opentr.sh <cmd> --dry-run` output for a matrix of flag combinations
  (dev, prod, `--fresh`, `--gpu-scale`, `--with-*`, `--nas`/`--no-nas`) is byte-identical to HEAD.
  `--dry-run` prints and starts nothing (root `CLAUDE.md`).
- `scripts/common.sh` -> `scripts/lib/common/` (each module added to `release-manifest.txt`, since installs fetch `common.sh` and it sources them; a unit test asserts every module is listed): `env.sh` (check_docker, read_env_value, env
  permissions, hostname/LAN IP, MinIO KMS secret, required dirs), `permissions.sh` (model cache,
  nltk, scratch, architecture, deployment mode, backend image, opensearch models/corpora), `pg.sh`
  (`pg_*`, compose_run_migrate), `voiceprint.sh` (`voiceprint_*`, `os_*`, consistency report),
  `backup.sh` (backup/restore_database), `info.sh` (access info, help, wait_for_backend_health).
- `docker-build-push.sh` -> `scripts/lib/docker_build/`: `checks.sh`, `platforms.sh`,
  `scan.sh`, `legs.sh` (tag/cache/output/identity args, build_one_leg, capability index),
  `components.sh` (backend, blackwell, lite, frontend, docs), `cli.sh`.
- `security-scan.sh` -> `scripts/lib/security_scan/`: `components.sh`, `tools.sh` (installers),
  `scanners.sh` (trivy, grype, dockle, hadolint, syft/SBOM), `targets.sh`, `report.sh`.
- `release-tests/test-upgrade.sh` -> `release-tests/upgrade/phases_00_05.sh`, `phases_06_09.sh`,
  `phases_10_13.sh`, `phases_14_18.sh`, `hops.sh`, `report.sh`; `test-upgrade.sh` keeps arg
  parsing and phase dispatch.
- `release-tests/lib/guardrails.sh` -> `lib/guardrails/{log,paths,checks,stock,fingerprints,preflight}.sh`.
- `e2e/update-visual-baselines.sh` -> `scripts/e2e/visual_baselines/{ports,stack,capture,provenance,cli}.sh`.
- `fetch-rag-eval-data.sh` -> `scripts/lib/rag_eval_fetch/{select,archive,manifest,beir}.sh`.
- `run-mutation-tests.sh` -> `scripts/lib/mutation/{evidence,verify,preconditions}.sh`.

Shell proof per lane: `bash -n` and `shellcheck -x` on every file; existing unit tests that read
these scripts (`rg -l "<script name>" backend/tests`) stay green; `--help` and dry-run output
unchanged; the end-to-end proof is the rehearsal in the evaluation phase (fresh install, upgrade
across all 18 phases, lite, scenario D).

### 4.6 Broad-except lanes (B1-B11, 1,086 handlers, standard of section 1.2)

| Lane | Files | Handlers |
|---|---|---|
| B1 | `backend/app/auth/**`, `backend/app/core/**` (incl. critical: `password_history`, `rate_limit`, `mfa`, `oidc/flow`, `pki_*`, `ldap_auth`, `lockout_store`, `session`, `audit`, `core/tenant_limits`) | 84 |
| B2 | `backend/app/utils/**` (incl. `encryption.py`), `startup/**`, `main.py`, `db/**`, `backend/app/scripts/**` | 71 |
| B3 | `backend/app/api/**` (incl. `auth/sessions.py`, `files/complete_upload.py`) | 80 |
| B4 | `backend/app/transcription/**`, `backend/app/tasks/transcription/**` (GPU-heavy: typing-safe narrowing, fakes) | 107 |
| B5 | `backend/app/tasks/*.py` from `a` through `rename_propagation_task.py` | 88 |
| B6 | `backend/app/tasks/*.py` after `rename_propagation_task.py` (incl. `tenant_backfill_task.py`) | 80 |
| B7 | `backend/app/services/*.py` from `a` through `opensearch_erasure.py` (incl. `auth_config_service`, `erasure_ledger_service`) | 129 |
| B8 | `backend/app/services/*.py` after `opensearch_erasure.py` (incl. `storage_recovery_service`) | 118 |
| B9 | `backend/app/services/search/**`, `services/opensearch_service/**` (incl. `search/index_admin.py`) | 108 |
| B10 | `services/{asr,chat,clustering,diarization,erasure_receipt,ingest_artifacts,local_models,privacy,protected_media_plugins,redaction,speaker_matching,watch_sources}/**` (incl. `chat/redactor.py`, `redaction/device.py`, `redaction/spans.py`) | 117 |
| B11 | `scripts/**` | 104 |

Each lane does its critical-path handlers first, then the rest. The lane recomputes its list on
its own tree with `tools/ble_classify.py` (paths shift after splits). End state: BLE001 baseline
0, no broad-except allowlist.

### 4.7 ORM calls out of routers (A1-A7)

Pattern: the router keeps HTTP concerns (parsing, auth dependency, status codes, response model);
the query and write logic moves to a function in an existing `backend/app/services/` module for
that domain (create one only when none exists), taking `db: Session` and plain arguments,
returning ORM rows or domain values, raising domain exceptions the router maps to HTTP (that is
also what `fastapi_import` forbids outside the API layer). No behaviour change; the router's API
tests are the characterization tests and must pass unchanged.

| Lane | Routers (`backend/app/api/endpoints/`) | Occurrences |
|---|---|---|
| A1 | `files/**` (22 files), `transcript_segments.py`, `comments.py`, `tags/**` | 83 |
| A2 | `speaker*`/`speakers*` (13 files), `topics.py`, `summarization.py`, `prompts*.py` | 98 |
| A3 | `media_collections*.py`, `chat/**`, `groups.py`, `search_reindex.py`, `embedding_migration.py`, `speaker_attribute_migration.py` | 79 |
| A4 | `auth/**`, `scim/**`, `admin*.py`, `users.py`, `users_self_service.py`, `auth_config.py` | 71 |
| A5 | `asr_*.py`, `llm_*.py`, `user_settings*.py`, `diarization_settings.py`, `engine_settings.py`, `redaction_settings.py`, `custom_vocabulary.py` | 90 |
| A6 | `legal.py`, `erasure_receipts.py`, `org_admin*.py`, `privacy.py`, `support_access.py`, `usage.py`, `user_files.py`, `watch_sources*.py`, `tasks*.py`, the 2 top-level `api/` files | 65 |
| A7 | the 19 `fastapi_import` files (`auth/{audit_export,cookies,provider_registry,rate_limit,saml/sp}.py`, `core/{capabilities,locked_settings,validation_errors}.py`, `services/{account_security_service,asr_switch_plan,auth_config_service,chat/hooks,content_audit,content_hold_service,diarization/factory,pipeline_plan_inputs}.py`, `tasks/transcription/hooks.py`, `utils/{client_ip,pagination}.py`) and the 64 `raw_env` files | 19 + 126 |

A7: a `raw_env` read moves into the settings module; bootstrap reads that must run before
settings load (logging setup, `config.py` itself) go to `allowlists/raw_env.txt` (class
`bootstrap-before-settings`) with the reason. A `fastapi_import` that is genuinely an HTTP adapter
living outside `api/` (e.g. `utils/client_ip.py` reading a `Request`) moves into `api/`.

### 4.8 Styling lanes (S0-S5) from `docs/design/frontend_styling_layer_plan.md`

That plan is the method of record (tokens, A/B screenshot tool, modes `zero`/`intended`, D1-D15,
browser-verified light/dark/`ar`). This plan only schedules its lanes:

| Lane | Styling-plan lanes | Model |
|---|---|---|
| S0 | Phase 0 (infrastructure, A/B tool, ui-kit fixture, guard wiring) | sonnet |
| S1a | Phase 1 lanes 1a-1c (R1 fallbacks, R2 role colours, R4/R8/R9) | haiku (codemod, zero-diff A/B) |
| S1b | Phase 1 lanes 1d-1e (spacing, radius, type) | haiku |
| S2a-S2e | Phase 2: 2.0a+2.0b, 2.1a-c, 2.2a-c, 2.3a-c, 2.4a-c + 2.5a-d + 2.6 (sequential: shared `form-elements.css`/`theme.css`) | sonnet |
| S3a-S3f | Phase 3: 3.1+3.2, 3.3+3.4, 3.5+3.6, 3.7+3.8, 3.9+3.10, 3.11+3.12 (disjoint surfaces, parallel) | sonnet |
| S4 | Phase 4 (snap off-scale values, D8) | haiku |
| S5 | Phases 5-6 (RTL logical properties, close-out, guard at 0 except the CUI banner exemption) | sonnet |

A whole-tree codemod lane (S1a, S1b, S4) never shares a wave with another frontend lane.

---

## 5. Efficiency: from measurement to fix lanes

No speculative review. Sources and how each becomes a lane:

1. **Lane P py-spy profile (B3 in `V070_FINISH_LIST.md`)**, part of the milestone code before M:
   its `~/ot-cleanup-work/scale/profile/SUMMARY.md` top self-time frames each become an E-lane
   (Sonnet, file ownership as above) scheduled into the swarm's (Q) slots or W14. Proof: the same
   load scenario re-profiled on the same host, before/after flame graph and latency numbers; a
   change that does not move its frame is reverted.
2. **Final load pass C1 (#37)** in the evaluation phase: each missed SFP verification line goes
   into `RESULTS.md`; small misses are fixed in the evaluation fix lane, larger ones become a
   v0.8.0 issue (C2 rule). pg_stat_statements `calls` per request type gives N+1s with numbers.
3. **GPU pipeline tests** in C4 (`run-diarization-gpu-tests.sh`, `-m gpu`, `--gpu-scale` on GPU 1
   +2): a stage time regressing against the last recorded benchmark becomes a lane.
4. **Test-suite timing**: `scripts/analyze-test-timing.py` on the junit every wave sweep already
   writes (captured once). A lane only for a measured cluster or regression wave-over-wave.

Candidate to confirm with pgss in lane P or C1 (no fix without the number):
`services/chat/redactor_digest_plans.py:_gather_digest_plans` batches the digest read but still
runs one `MediaFile` redaction-status query per digest hit (25 files = 25 extra round trips per
masked chat turn). Fix if confirmed: one `MediaFile.id.in_(...)` query beside `_load_digest_rows`,
keeping per-sentence fail-closed behaviour.

---

## 6. Verification per lane (lead checks before merge)

- Every behaviour change: test RED on a `git archive HEAD` tree in `mktemp -d -t ot-redcheck.XXXXXX`,
  GREEN on the lane tree; report both with test names.
- Moves/splits: `git diff -M --stat` shows moves; existing tests pass; inventory tests where a
  move could drop something (Settings fields and defaults, LLMService methods, UploadService
  public API); shell: byte-identical `--help`/`--dry-run` output, `bash -n`, `shellcheck -x`.
- `backend/venv/bin/mypy` on every touched `.py`; `ruff check` + `ruff format --check` on touched
  files; frontend `npx eslint <files>`, `npm run check`, targeted `npx vitest run`.
- The affected guards pass after `--tighten`; `git diff` on `baselines/` shows only removed lines
  or lowered values; allowlist additions carry a class tag and a reason that fits the class.
- Dead code: per deleted name, an untruncated `rg -n "\bNAME\b"` over the whole repo (backend,
  frontend, scripts, alembic, docs, yaml/toml/json/sh, compose); checks for string dispatch,
  Celery task names and beat schedules, decorators, `__all__`, PEP 562 `__getattr__`, entry points,
  subclass overrides, Alembic imports; extension seams (`set_*_resolver`, `register_*`) are
  allowlisted, never deleted (`~/.claude/skills/dead-code-audit/SKILL.md`, incl. the
  same-named-twin limitation).
- Broad-except: per handler, the outage is injected (the dependency raises) and the test asserts
  it is visible (propagates, 503, failure status, failure count, distinct return), and that the
  log record carries `exc_info` (pytest `caplog`: `record.exc_info is not None`).
- Provider and network code uses fakes only: AWS via `botocore.stub.Stubber`, Gladia via
  `backend/tests/unit/test_gladia_provider.py` / `tests/fixtures/mock_asr.py`, pyannote.ai via
  `test_pyannote_provider.py`.
- Frontend behaviour changes: opened in a browser (`DISPLAY=:11`) light and dark (and `ar` where
  layout changes) on a fresh stack the lead provides; screenshots in the report.

---

## 7. End state per ratchet at release (everything to 0 or legit-only allowlist)

| Ratchet | Now | Baseline at release | Allowlist (what stays, why) |
|---|---|---|---|
| complexity | 33 | 0 | none |
| broad_except | 1,086 | 0 | none (top-level boundaries log with traceback + re-raise/record, which ruff exempts) |
| vulture | 165 | 0 | `vulture_whitelist.py`: ~20 extension seams consumed outside this repo, test-observation hooks a production invariant depends on |
| suppressions | 4 | 0 | none |
| type-ignore (new) | 237 | 0 | stub gaps with an upstream issue URL only |
| hidden mypy (new) | 261 | — | equals the type-ignore allowlist exactly |
| api_boundary | 148 | 0 | none expected |
| raw_env | 64 | 0 | `bootstrap-before-settings` reads |
| file_size | 20 | 0 | `single-file-installer`: `opentranscribe.sh`, `setup-opentranscribe.sh` (cleaned in place; value = post-cleanup length) |
| eslint | 773 (+81 new rules) | 0, every rule at `error` | none |
| svelte unused (new) | measure in L01b | 0 | none |
| knip | 48 | 0 | test-mock entry points are knip config, not findings |
| duplicates (new) | measure in L01b | 0 | `intentional-parallel` only where two copies must stay independent (e.g. a migration's frozen copy of a helper), each with a reason |
| style_literals | 12,071 | 0 | `permanent: regulatory CUI banner colours` (existing) |
| migration_safety | 157 | 0 | `applied-history` (immutable released revisions) + the reasoned erasure_ledger index |
| process_ids | 6 | 0 | none |
| madge cycles | 0 | 0 | none |

Final check (W14): every guard run with no baseline content except the allowlists, through
`scripts/safe-precommit.sh run --all-files` and `--hook-stage pre-push`, plus `check_hidden_mypy.py`.

---

## 8. Sizing

About **114 lane-days in 59 lanes**: guards 3.5, mechanical 2, dead code 3.5, Python/TS/Svelte
splits 7, shell (9 splits + 2 installers cleaned in place) 8.5, test code (T1) 2, complexity 6.5, eslint judgement 2, hidden mypy 6, broad
except 27.5, ORM/fastapi/raw-env 14, duplicates 4, styling 29 (its own plan's 35 sub-lanes grouped
into 16). At 3.5 lanes average with overlapping sweeps that is about 33 working days of swarm
(W0 to W14), then about 5 working days of evaluation and 2 of release: **release about Jan 12,
2027** with M = Nov 16. Each day M slips moves the release a day. The Dec 24-27 holiday gap is
already in the table (W12 to W13).

---

## 9. Risks

- **Hot reload**: every `.py` under `backend/app` restarts a bind-mounted dev backend, and startup
  dispatches `search_index_maintenance`. Lanes work in their own worktrees, which the dev stack
  does not mount, and never start the stack; the lead merges between measurements, never during.
- **GPU-only paths** (`transcription/`, `vram_budget.py`, `cuda_health.py`,
  `tasks/transcription/core.py`, `nvml_monitor.py`): lanes change typing, decomposition and
  exception narrowing there only with CPU unit tests and fakes; the `-m gpu` and diarization GPU
  tests run in C4 on GPU 1 (+2 if idle), never GPU 0.
- **Provider code** (AWS, Gladia, pyannote.ai): characterization tests through fakes BEFORE
  refactoring (no AWS provider unit test exists today).
- **Narrowing a handler can turn a soft failure into a crash**: each narrowing lists the
  exceptions the try body can raise (library docs), and the outage-injection test shows the new
  behaviour; anything uncertain stays at the documented top-level-boundary shape (section 1.2,
  rule 4) rather than guessed narrow.
- **Distributed files**: a missed `release-manifest.txt` line breaks fresh installs silently.
  Guarded by the manifest tests (L08 for `scripts/model_download/`)
  and the rehearsal in evaluation. The two installers are not split, so their download path is
  unchanged.
- **ORM extraction across 129 routers**: transaction boundaries must not move (a router that
  commits once must still commit once); each A-lane asserts commit count in one test per router
  group and keeps the API tests unchanged.
- **Styling**: visual change only through the styling plan's A/B tool and decisions D1-D15.
- **New guards' one-time baselines** (G-b, G-c, G-d): written once with `--force` and a reason;
  the lead checks only new keys were added and no existing value rose.
- **Schedule**: the date rests on M = Nov 16 and on (Q) lanes running; three-lane waves move
  release to about Jan 20 (section 4.1).

---

## 10. Docs consulted

- ruff BLE001: https://docs.astral.sh/ruff/rules/blind-except/ (re-raise and `exc_info` exempt; confirmed with a probe)
- Python tutorial, Errors and Exceptions: https://docs.python.org/3/tutorial/errors.html (specific handlers, log and re-raise, `raise ... from`)
- Python logging: https://docs.python.org/3/library/logging.html (`Logger.exception`, `exc_info`)
- mypy error codes: https://mypy.readthedocs.io/en/stable/error_codes.html
- mypy type narrowing: https://mypy.readthedocs.io/en/stable/type_narrowing.html (cast is a hint only)
- pydantic mypy plugin: https://pydantic.dev/docs/validation/latest/integrations/dev-tools/mypy/
- slowapi: https://slowapi.readthedocs.io/en/latest/
- svelte-check: https://github.com/sveltejs/language-tools/tree/master/packages/svelte-check
- typescript-eslint no-unused-vars: https://typescript-eslint.io/rules/no-unused-vars/
- eslint-plugin-svelte user guide: https://sveltejs.github.io/eslint-plugin-svelte/user-guide/
- ESLint complexity: https://eslint.org/docs/latest/rules/complexity
- jscpd: https://github.com/kucherenko/jscpd
- Single-file installer practice: Homebrew https://github.com/Homebrew/install (`install.sh`, run via `curl -fsSL .../install.sh`); nvm https://github.com/nvm-sh/nvm (`curl -o- .../install.sh | bash`); rustup https://rust-lang.github.io/rustup/installation/other.html (shell installer `rustup-init.sh`); Docker https://github.com/docker/docker-install ("Home of the script that lives at get.docker.com")
- shellcheck directives: https://github.com/koalaman/shellcheck/wiki/Directive
- Skills: `~/.claude/skills/fail-open-audit/SKILL.md`, `~/.claude/skills/dead-code-audit/SKILL.md`
- Repo: root `CLAUDE.md`, `docs-site/CLAUDE.md` (generated content + drift checks),
  `scripts/guards/_baseline.py`, `.pre-commit-config.yaml`, `release-manifest.txt`,
  `docs/design/frontend_styling_layer_plan.md`, `~/ot-cleanup-work/V070_FINISH_LIST.md`.
- Each lane also reads and cites what it touches: mypy Protocols, TypedDict, Any; SQLAlchemy 2.0
  ORM typing and session/transaction docs; FastAPI responses, dependencies, response models;
  pydantic-settings; boto3 `Stubber`; Svelte 5 runes and `.svelte.ts`; SvelteKit `$app/paths`;
  bandit nosec; redis-py, minio, requests, cryptography exception references.

---

## 11. Lane briefs (paste one per agent, with the footer)

**Footer (paste under every brief verbatim):**

> **Standing rules.** Work only in your own worktree
> `/mnt/nvm/repos/transcribe-app/.claude/worktrees/<lane>` on branch `debt/<lane>`, created by the
> lead from the integration tip they name. Commit there with hooks on and signing as configured,
> one commit per concern, conventional commits. Never `--no-verify`, never disable signing, never
> push, never merge, never touch another checkout, never start or use the live dev stack (DB
> tests: `scripts/test-in-worktree.sh -- <pytest args>`). Follow
> `~/ot-cleanup-work/briefs/STANDING_LANE_RULES.md`. Targeted tests only: red on a
> `git archive HEAD` tree in `mktemp -d -t ot-redcheck.XXXXXX`, then green on yours. Static checks
> on touched files only (`backend/venv/bin/mypy`, `ruff check`, `ruff format --check`,
> `npx eslint`, `npm run check`, the guard your lane affects). No full suites, no `--all-files`.
> No suppressions of any kind (root `CLAUDE.md`, "Fix the finding, never silence it"). Baselines
> only shrink: run the affected guard with `--tighten` and commit the lowered baseline in the same
> commit. Read the official docs for each package you touch at its pinned version and cite the
> pages. Batch each `backend/app` file's edits into one write. Before reporting:
> `cd backend && python3 ../scripts/audit-tests.py tests` shows 0 open. Report: commits,
> per-criterion evidence (red and green test names), docs pages touched, docs consulted, anything
> open. Plan of record: `docs/design/code_debt_plan.md` (sections 1-7).

### L01a Python guards, allowlists, hidden-mypy check (sonnet, W0)
Goal: every Python guard can end at an empty baseline with a legit-only allowlist, and a
`type: ignore` can hide only what it declares. Read plan sections 2 and 7, `scripts/guards/_baseline.py`,
`check_ruff_ratchet.py`, `check_suppressions.py`, `check_vulture.py`, `backend/tests/unit/test_guard_*.py`,
`.pre-commit-config.yaml` guard block. Do, one commit each:
1. `scripts/guards/allowlists/` + `_baseline.load_allowlist(path, classes)`: same TSV; reason must
   pass `audit_tests.allowlist.is_real_reason` and start with a declared class tag
   (`applied-history`, `stub-gap <url>`, `extension-seam`, `test-observation`,
   `bootstrap-before-settings`, `single-file-installer`, `intentional-parallel`); `compare()` passes
   allowlisted keys, fails on stale allowlist entries; `--tighten` never edits allowlists; the size
   report shows debt and allowlisted separately. Must-fire and must-stay-clean tests.
2. Re-key BLE001 to `path::qualname#n` (reuse `_qualnames`); prove sum(old) == count(new);
   `--write-baseline --force --only broad_except --reason "re-keyed per handler so a fix and a new swallow cannot cancel out"`.
3. Move migration_safety entries to `allowlists/migration_safety.txt` (`applied-history`, plus
   the reasoned erasure_ledger index); baseline empty.
4. `check_suppressions.py`: `type-ignore` count ratchet keyed `path::code`, new baseline written once with a reason.
5. `scripts/guards/check_hidden_mypy.py` (push stage + a CI step in `.github/workflows/pre-commit.yml`):
   copy tracked tree to a temp dir, strip ignores with `tokenize`, run the pinned mypy with the
   repo config, compare the (path, code, count) multiset to baseline + allowlist; exit 2 on any
   mypy crash. Unit-test the comparison with fixtures; run the whole-tree check once.
6. Ruff parity: drop `C90` from `backend/pyproject.toml`; drop the root `B904` ignore and fix
   `scripts/` with `raise ... from exc`; fix the 8 `scripts/` type-ignores.
7. `plugins = ["pydantic.mypy"]` in the root mypy config; fix new findings in your files; report
   the count elsewhere for L13/L14/L21 (do not baseline them silently; add them to the type-ignore
   baseline only if they are pre-existing errors the plugin now sees, with that reason).
8. Second mypy hook over `scripts/(guards|lib|audit_tests|model_download|cleanup_test_data)/`: measure, baseline.
Docs: ruff BLE001/C901/B904/settings; mypy error codes + config; pydantic mypy plugin; pre-commit config. **[footer]**

### L01b frontend guards (sonnet, W0)
Goal: unused locals in `.svelte`, function complexity and duplicate code measured and ratcheted on
all files. Read plan sections 1-2 (G-a, G-b, G-d, G-e6), `frontend/eslint.config.js`,
`scripts/guards/check_eslint_ratchet.py`, `check_knip.py` and tests.
1. Reduce the `no-unused-vars` crash (`npx eslint --no-cache --rule '{"@typescript-eslint/no-unused-vars":"warn"}' src/components/Navbar.svelte`)
   to a minimal construct; check typescript-eslint, eslint-plugin-svelte and svelte-eslint-parser
   trackers/changelogs. Fixed upstream: bump, enable for `.svelte`, delete both "off" blocks. Not
   fixed: tsconfig `noUnusedLocals`/`noUnusedParameters` for the check run, and
   `check_svelte_unused.py` over `svelte-check --output machine-verbose` (codes 6133, 6192, 6196,
   6198), per-file ratchet, push stage. Record the count.
2. Add `complexity` 15, `max-depth` 4, `max-lines-per-function` 150 (skip blanks/comments) as
   `warn`; extend `baselines/eslint.txt` once for the new keys only (expected 81) with a reason;
   prove no existing value rose.
3. `jscpd` (pinned devDependency; `python3 scripts/check-licenses.py --npm-only` passes) and
   `scripts/guards/check_duplicates.py` (push stage) over `backend/app`, `backend/tests`, `scripts`, `frontend/src`,
   json reporter, ratchet keyed by file; pick `--min-tokens` from the docs and justify; baseline
   once; write the clone-group report to `~/ot-cleanup-work/plans/measure/duplicates.json` for D1/D2.
4. knip: make `src/test-mocks/*` a knip entry if vitest setup uses them (verify).
Docs: svelte-check README; typescript-eslint no-unused-vars; eslint-plugin-svelte guide; ESLint
complexity/max-depth/max-lines-per-function; jscpd README; knip configuration. **[footer]**

### L02 mechanical batch (haiku, W0)
Four commits:
1. slowapi: `response: Response = None,  # type: ignore[assignment]  # required by slowapi` ->
   `response: Response,` in `api/endpoints/{llm_model_discovery.py x2, users.py, search.py,
   asr_local_model.py x2, diarization_settings.py, llm_connection_tests.py, asr_connection_tests.py x2,
   llm_local_endpoints.py, local_models.py x2}` and `api/endpoints/files/{voiceprint_consent.py,
   listing.py}`; `request: Request = None` in `api/endpoints/files/__init__.py:170` (if anything
   calls it without a request, stop and report). If a non-default parameter would follow a
   defaulted one, move `response` before the defaulted parameters. slowapi docs: `response` is
   required for header injection when the endpoint returns a non-Response; FastAPI injects it.
   Verify with mypy per file and a rate-limit header test (`rg -l "X-RateLimit" backend/tests`;
   add one for one endpoint if none exists, stated as a pinning test).
2. `baselines/process_ids.txt`: reword the 6 lines; tighten to empty.
3. nosec: move every free-text reason before the directive (`# <reason>  # nosec Bxxx`); remove
   the lines in `~/ot-cleanup-work/a20/nosec_unused.tsv` bandit still reports as suppressing
   nothing (check each with `bandit <file>` before and after); report warning counts before/after.
4. The 7 handlers that raise a new exception without `from` (category "raise-new-exception" in
   `~/ot-cleanup-work/plans/measure/ble_classified.json`): add `from exc`; tighten the ruff ratchet.
Docs: slowapi; FastAPI Response parameter; bandit nosec; Python tutorial exception chaining. **[footer]**

### L03 eslint mechanical + import aliases (haiku, W1)
Zero findings for `no-restricted-imports`, `prefer-const`, `no-useless-mustaches`, `no-empty`,
`svelte/no-unused-svelte-ignore`, `svelte/no-navigation-without-resolve`; then flip each to
`'error'`. Only the files the lead lists. Imports: `../` -> the `$lib`/`$components`/`$stores`
alias resolving to the same file (check the alias config); `../assets`, `../scripts` stay.
Navigation: wrap with `resolve()` from `$app/paths` per SvelteKit docs, targets unchanged. One
commit per rule; verify `npx eslint <files>`, `npm run check`, related vitest, `check_eslint_ratchet.py --tighten`.
Docs: ESLint no-restricted-imports; eslint-plugin-svelte rule pages; SvelteKit `$app/paths`. **[footer]**

### L04 backend dead code (sonnet, W1)
`baselines/vulture.txt` to empty: each of the 165 entries deleted with its tests, or whitelisted
in `scripts/guards/vulture_whitelist.py` with a class (`extension-seam` for the `set_*_resolver`,
`register_*`, `reset_*_resolver` functions in `core/capabilities.py`, `core/tenant_limits.py`,
`auth/provider_registry.py`, `services/privacy/policy.py`, `legal/registry.py`, `chat/hooks.py`,
`tasks/transcription/hooks.py`, `db/audit_protection.py`, `erasure_receipt/inventory.py`,
`privacy/consent.py`; `test-observation` only where a test asserts a production invariant through
it). `reset_for_tests` helpers: move the reset into the test fixture and delete the helper. Method:
`~/.claude/skills/dead-code-audit/SKILL.md` plus plan section 6. Write the exact deletion list in
the first commit body before deleting; one commit per module group. Verify targeted tests,
`check_vulture.py --tighten`, mypy on touched files. **[footer]**

### L05 frontend dead code (sonnet, W1)
knip 0, svelte-unused (L01b) 0, `no-unused-vars` and `no-unused-expressions` 0 then `'error'`. A
name used only in markup, a `$store` auto-subscription, `bind:`/`use:` or a snippet is not dead:
read the whole `.svelte` file. Delete dead code and its tests. Verify `check_knip.py`, the
svelte-unused guard, `npm run check`, vitest, browser check for any behaviour-bearing change.
Docs: knip; typescript-eslint rule pages. **[footer]**

### L06 eslint judgement rules (sonnet, W4)
Burn down `no-explicit-any` (158), `require-each-key` (112), `no-useless-assignment` (70),
`infinite-reactive-loop` (41), `no-at-html-tags` (29), `prefer-svelte-reactivity` (19), the other
svelte rules, then flip each to `'error'`. Real types (from `$lib/types`, or `unknown` + guard),
stable each keys (`uuid`/`id`), runes conversion where `no-useless-assignment` flags the legacy
reactive idiom, every `{@html}` proven DOMPurify-sanitised (fix any that is not), reactive loops
fixed with a vitest. Never a file L12 holds that wave. Browser check each behaviour change. Docs:
typescript-eslint no-explicit-any; eslint-plugin-svelte rules; Svelte 5 runes. **[footer]**

### L07 backend file splits (sonnet, W2)
`core/config.py` and `services/llm_service.py` under 1,000 lines with the plan 4.4 layouts
(existing `settings_*.py` and `llm_*` mixin patterns). Tests first: `sorted(Settings.model_fields)`
and every default equal the values captured on HEAD; one validator per new mixin still fires;
`LLMService` exposes the same method set as on HEAD. Verify mypy, config/LLM tests,
`check_file_size.py --tighten`, `size_report.py --write-snapshot`. Docs: pydantic-settings; pydantic
validators on inherited models. **[footer]**

### L08 script splits (sonnet, W2)
`scripts/download-models.py` -> `scripts/model_download/`, `scripts/cleanup-test-data.py` ->
`scripts/cleanup_test_data/` (plan 4.4); both stay as entry points. Downloader: mount the package
in `download-models.sh`, every new file in `release-manifest.txt`, Dockerfile COPY lines updated,
unit test that the manifest lists every package file. Keep every test that loads either script by
path green (`rg -l "download-models|cleanup-test-data" backend/tests`). Verify those tests,
`--help` of both, `check_file_size.py --tighten`. Never run the downloader for real. **[footer]**

### L09a / L09b frontend splits (sonnet, W2)
a: `uploadService.ts`, `FileUploader.svelte`; b: `FilterSidebar.svelte`, `SettingsModal.svelte`,
`routes/+page.svelte`. Layouts in plan 4.4; plain `.ts` (or `.svelte.ts` for runes state) under
`$lib`; never touch `<style>`. Existing vitest stays green; a test per extracted module. Verify
`npm run check`, `npm run build`, targeted vitest, `check_file_size.py --tighten`, and a browser
pass (upload flow / gallery / filters / settings, light and dark, `ar`) on a fresh stack the lead
provides; screenshots in the report. Docs: Svelte 5 `.svelte.ts` and runes; SvelteKit `$lib`. **[footer]**

### L16 installers cleaned in place (sonnet, W0 (Q))
`opentranscribe.sh` and `setup-opentranscribe.sh` stay ONE self-contained file each (industry
practice for user-downloaded installers: Homebrew `install.sh`, nvm `install.sh`,
`rustup-init.sh`, `get.docker.com`; plan decision 3). Do NOT split them and do NOT add a bundle
step. Clean each internally per plan 4.5: shellcheck clean with no `disable` directives; clear
`# ===== section =====` blocks in the order 4.5 lists; functions over ~60 lines and long `case`
arms decomposed into named steps; no dead code (every function has a caller: `rg` by name across
the repo, CI workflows, docs and tests; no commented-out code, no unreachable branches). First
capture golden outputs on HEAD for every read-only path (`--help`/`help`, `version`, the compose
file selection for a matrix of `.env` inputs, if a test harness for it exists; otherwise add one
that runs the function under `bash` with a temp `.env`) and prove them identical after. Add the
two `allowlists/file_size.txt` entries (class `single-file-installer`, reason from plan 4.5,
value = the cleaned line count). Keep `release-manifest.txt` paths unchanged. Verify `bash -n`,
`shellcheck`, existing tests that read these files (`rg -l "setup-opentranscribe|opentranscribe.sh" backend/tests`),
`check_file_size.py`. Never run an install or upgrade; the lead's rehearsal in evaluation is the
end-to-end proof. Docs: shellcheck wiki (each SC code you fix); bash manual for any construct you
change. **[footer]**

### SH shell review in place (sonnet, W11) — replaces L17, L18 and L19 (owner decision 2026-11-09)
Shell scripts are reviewed and fixed, but they are NOT split and NOT under the file-size cap. Scripts that ship to users must stay single files, so an install never has to fetch many loose files. Scope: every tracked `*.sh`: `opentranscribe.sh`, `setup-opentranscribe.sh`, `scripts/common.sh`, `opentr.sh`, `scripts/docker-build-push.sh`, `scripts/security-scan.sh`, `scripts/release-tests/**`, `scripts/e2e/*.sh` and the rest.

For each file:
- `shellcheck -x` clean, with no `disable` directives unless one is justified on that line;
- `bash -n`;
- no dead functions (`rg` by name across the repo, CI, docs and tests);
- long `case` arms and functions broken into named steps where that genuinely helps reading;
- `set -e` semantics unchanged. The upgrade test once lost phases to a `set -e` harness bug: see `scripts/CLAUDE.md`.

Proof: `--help`/usage and the `./opentr.sh start <mode> --dry-run` outputs are byte-identical to HEAD, and the existing tests that read these scripts stay green. Add each file to `scripts/guards/allowlists/file_size.txt` with class `single-file-installer` (shipped) or `shell-script` (repo-only). Never run an install, upgrade, build or scan. **[footer]**

### L10 A22 complexity (sonnet, W3)
`update_asr_config` (26), `AWSTranscribeProvider.transcribe` (43), `GladiaProvider.transcribe`
(26), `PyAnnoteProvider.transcribe` (24) to <= 15; remove their `# noqa: C901`; tighten
complexity and suppressions baselines. Characterization tests first through fakes:
`botocore.stub.Stubber` for AWS (no AWS provider unit test exists), the existing Gladia and
pyannote.ai test patterns, the API tests for `update_asr_config`. Cover every extracted branch
(upload, job start, poll with timeout, parse, error mapping). Same exceptions and messages; keep
`test_no_exception_echo_in_response.py` green. Docs: boto3 Stubber; AWS Transcribe
StartTranscriptionJob/GetTranscriptionJob; Gladia API; SDK pins in `backend/requirements*.txt`. **[footer]**

### L11 backend complexity (sonnet, W3)
The 11 functions in plan 4.3 to <= 15. Characterization tests first (add missing branch tests),
then extract named steps, no behaviour change. `transcribe_gpu_task` is GPU-only: decompose only
pure orchestration, prove with CPU tests and fakes, list what only `-m gpu` can prove. **[footer]**

### L20 scripts complexity (sonnet, W7 (Q))
The 18 `scripts/` functions in plan 4.3 to <= 15 (cleanup-test-data's function at its new L08
path). Same method; these tools have tests under `backend/tests/unit/` (find with `rg`). **[footer]**

### L12 frontend complexity (sonnet, W3)
The 81 new findings (complexity 46, max-depth 4, max-lines-per-function 31) to 0, then the three
rules to `'error'`. Extract pure helpers into `$lib` with vitest tests; markup behaviour
identical; browser check each changed component. Never a file L06 holds. **[footer]**

### L13 hidden mypy: api, schemas, main (sonnet, W4)
Remove every ignore in `backend/app/api/**`, `backend/app/schemas/**`, `backend/app/main.py` by
fixing what it hides (regenerate the list on your tree with the plan section 1 method). Order: fix
the bug; correct the annotation; build response models with `model_validate`/explicit keywords
instead of `**dict`; narrow `X | None` explicitly; a `Protocol` for duck-typed parameters
(`create_media_file_record`'s `file`, fed `FileMetadata` at `files/prepare_upload.py:252`);
Starlette's declared handler signature for `main.py` exception handlers. Prove red first:
`files/prepare_upload.py:252`, `websockets.py:515` (`WebSocket | None` passed as `Request`),
`topics.py:239` (fixed by the pydantic plugin: confirm). Allowlist only a stub gap with an
upstream URL. Verify mypy per file, API tests, `check_suppressions.py --tighten`, one
`check_hidden_mypy.py` run. Docs: mypy narrowing, Protocols, TypedDict; FastAPI response models;
pydantic `model_validate`; Starlette exception handlers. **[footer]**

### L14 hidden mypy: rest of backend/app (sonnet, W4)
As L13 for `services/`, `tasks/`, `utils/`, `auth/`, `core/`, `transcription/`. Prove red first:
`services/watch_sources/processing.py:490` (`delete_file` not on `BaseWatchSourceClient`; narrow
to the local client), `services/speaker_status_service.py:329-330` (response-only attributes on an
ORM row: return a schema object), `tasks/embedding_migration_v4.py:426,429` (callbacks return
`bool`, declared `None`), `utils/nvml_monitor.py:66` (`bool` into a `CDLL | None`). Recurring
shapes: cryptography `ExtensionType` iteration (narrow with `isinstance(ext.value, x509.<Concrete>)`),
`int()` on `int | None`, `Literal` parameters fed `str`, `no-any-return` from SQLAlchemy/redis
(annotate the local), generic `type[T]` without `.id`/`.uuid` (bound `T` to a Protocol). GPU-only
files: typing only. Docs: mypy generics with bounded TypeVar, Protocols; SQLAlchemy 2.0 typing;
cryptography x509. **[footer]**

### L21 hidden mypy: tests (haiku, W1 (Q))
The 108 ignores in `backend/tests` (110 hidden errors). Typical fixes: type test fakes with the
Protocol or class they stand in for; `monkeypatch.setattr` instead of assigning to a module/method
(`method-assign`); `cast` is not allowed to hide a mismatch (mypy narrowing docs), use a real
fake. Run each changed test file; mypy per file; tighten the type-ignore baseline. Anything
needing a production change: stop and list it for the lead. **[footer]**

### T1 test code: dead helpers, dead fixtures, duplicate helpers (sonnet, W3 (Q))
Goal: test support code is as clean as production code. Scope: `backend/tests/fixtures/**`, every
`conftest.py`, test helper/support modules, frontend `src/test-mocks/**` and shared test helpers.
1. Dead helpers: run `vulture --min-confidence 60 backend/tests` once and the dead-code-audit
   script with `--scan backend/tests`; a pytest **fixture is used by parameter name**, by
   `@pytest.mark.usefixtures`, by `autouse=True`, or by another fixture's parameters, so check each
   candidate fixture name with `rg -n "\bNAME\b" backend/tests` (untruncated) and
   `pytest --fixtures-per-test` on the files that should use it before deleting. Delete only
   proven-dead code; write the list in the first commit body.
2. Duplicate helpers: from L01b's `duplicates.json` entries under `backend/tests` and frontend
   tests, extract a shared fixture/helper into the nearest `conftest.py` or `tests/fixtures/`
   module (or a shared `src/test-mocks/` module), and delete the copies. Never change what a test
   asserts.
3. Prove each test still runs and still fails when it should: for every test whose helper you
   replaced, run it green, then once on a deliberately broken copy of the code under test in a
   `mktemp -d -t ot-redcheck.XXXXXX` tree (red), and report both.
Verify `python3 scripts/audit-tests.py backend/tests` 0 open, `cd frontend && npm run test:audit`,
targeted pytest/vitest runs, `check_duplicates.py --tighten`. Docs: pytest fixtures ("How to use
fixtures", `usefixtures`, `autouse`, conftest sharing); vitest mocking. **[footer]**

### B1-B11 broad-except lanes (sonnet, W5-W10)
Your lane id `Bn` and file set: plan section 4.6. Goal: every BLE001 handler in your files meets
plan section 1.2 (narrow; log with traceback via `logger.exception` or `exc_info=True`; record the
failure visibly; top-level boundaries catch `Exception`, log with traceback, record, re-raise
where the framework should see it). Critical-path handlers in your set first (section 1 list),
then the rest. Start by listing your handlers with
`python3 -I ~/ot-cleanup-work/plans/tools/ble_classify.py <worktree> <ruff-json> <out>` (ruff json:
`ruff check --isolated --ignore-noqa --no-cache --select BLE001 --output-format json <your dirs>`),
and report your verdict on any section 1.2 calibration rows in your set. Per handler: read the try
body, list the exceptions it can raise from the library docs, read every caller and what it does
with the negative value. Per fix: an outage-injection test, red first, asserting the failure is
visible and `caplog` shows `exc_info`. Commit per module. Verify targeted tests, mypy, ruff on
touched files, `check_ruff_ratchet.py --tighten`, and the fail-open skill script before/after on
your dirs (`~/.claude/skills/fail-open-audit/SKILL.md`). B4 (GPU paths): fakes only, list what
only `-m gpu` proves. B11 (`scripts/`): same standard; scripts' own tests under `backend/tests/unit/`.
Docs: Python tutorial errors; Python logging (`Logger.exception`, `exc_info`); exception references
of each library you narrow to (minio `S3Error`, redis-py, requests, SQLAlchemy `exc`, opensearch-py,
cryptography, botocore `ClientError`, Celery task retry docs). **[footer]**

### A1-A6 ORM out of routers (sonnet, W8-W11)
Your lane id `An` and routers: plan section 4.7. Move every query and write out of your routers
into functions in the existing domain service module (create one only if none exists); the router
keeps parsing, auth dependencies, status codes and response models; services raise domain
exceptions the router maps to HTTP. Transaction boundaries do not move: one test per router group
asserts the commit count is unchanged. The router's existing API tests are the characterization
tests and must pass unchanged; add service-level unit tests for each new function. Verify mypy,
targeted API tests, `check_api_boundary.py --tighten`. Docs: SQLAlchemy 2.0 session basics and
transactions; FastAPI dependencies and bigger-applications. **[footer]**

### A7 fastapi imports out of non-API layers + raw env (sonnet, W6 (Q))
The 19 `fastapi_import` files and the 64 `raw_env` files (plan 4.7). Services raise domain
exceptions instead of `HTTPException`, mapped in the routers; HTTP adapters living outside `api/`
move into `api/`. Env reads go through the settings module; only reads that must run before
settings load go to `allowlists/raw_env.txt` (`bootstrap-before-settings`, with the reason).
Prove with tests that a changed env read still honours its variable. Tighten both baselines.
Docs: pydantic-settings; FastAPI exception handlers. **[footer]**

### D1 / D2 duplicate code (sonnet, W4 (Q), W5 (Q))
D1 backend + scripts + `backend/tests`, D2 frontend (incl. its tests), from L01b's `~/ot-cleanup-work/plans/measure/duplicates.json`.
For each clone group: if the copies must agree, extract one shared function (in the module the
callers already import from) and delete the copies; if they disagree, that is a bug: write a test
showing which behaviour is right, then unify (dead-code-audit skill, codebase-audit section 4); if
they must stay independent (e.g. a migration's frozen copy), allowlist with `intentional-parallel`
and the reason. Tighten `baselines/duplicates.txt`. Never a file another lane holds. **[footer]**

### S0-S5 styling (S1a, S1b, S4 haiku; others sonnet; W5-W14)
Your lane id `Sn` and the styling-plan lanes it covers: plan section 4.8. The method of record is
`docs/design/frontend_styling_layer_plan.md`: read it fully (decisions D1-D15, the A/B screenshot
tool, modes `zero`/`intended`, the self-check and negative control) and execute your lanes exactly
as it specifies, including its per-lane screens, light/dark, `ar` RTL and the ui-kit fixture.
Codemod lanes (S1a, S1b, S4) must show a zero-diff A/B for `zero` mode. Tighten
`baselines/style_literals.txt` per commit. Never touch `<script>` logic. **[footer]**

---

## 12. Lead checklist per wave

1. Dispatch 3 lanes (4 if the host is quiet), file sets disjoint, `git worktree list` checked.
2. When the wave's lanes report: review, merge `--no-ff` into the integration branch, resolve
   baseline conflicts with the section 3 recipe.
3. ONE sweep on the merged tree, zero writers: `scripts/safe-precommit.sh run --all-files`,
   `... --hook-stage pre-push`, `./scripts/run-integration-tests.sh` (junit kept for
   `analyze-test-timing.py`), frontend `npm run check && npm run build && npm run test`. Green: tag
   `debt-wN`. The next wave codes during the sweep.
4. Update `V070_ISSUE_STATUS.md` and the board; append defects found to `~/ot-cleanup-work/RETRO_LEDGER.md`.
5. After W14: evaluation (section 4.1 row "Eval"), then `./scripts/release.sh`.
