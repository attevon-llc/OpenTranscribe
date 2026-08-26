# backend/tests — the whole test tree (api, unit, e2e, integration, redaction, transcription, onnx)

## Purpose

`./scripts/run-integration-tests.sh` is **THE pre-merge gate**: ungated suite → all `RUN_*`
suites in **both FIPS modes** → `-m integration`. Needs the live stack
(`./opentr.sh start dev`) plus `backend/venv`. GitHub Actions `backend-tests` is a safety net
only — fresh Postgres, CPU-only `backend/requirements-ci.txt` (**never `requirements-dev` in
CI**), `SKIP_S3`/`SKIP_OPENSEARCH` forced `True`. E2E is local-only: `./scripts/e2e/run-e2e.sh`
(3 xdist workers `--dist loadfile`, then `-m visual` serially) and `run-e2e-smoke.sh`.
Per-suite prose lives in `README.md`, `AUTH_TEST_SETUP.md`, `e2e/README.md`.

## ⚠️ A branch that adds a migration must be tested against a stack that has APPLIED it

`conftest` defaults `POSTGRES_PORT` to **5176 — the dev stack**, which runs whatever is checked
out in the *main* repo. A worktree on a feature branch that adds a migration is therefore, by
default, **testing the branch's code against master's schema**.

That is not hypothetical. It produced **13 failures** that read as real breakage and were
reported as pre-existing: 10 in `test_v389_migration_consistency`, 2 ORM-cascade, 1 DDL
divergence — every one of them `relation "file_facts" does not exist`, because the dev stack sat
at `v388` while the branch added `v389`. Re-run against a stack holding the migration and all 13
pass:

```bash
POSTGRES_PORT=5276 OPENSEARCH_PORT=5280 pytest tests/...     # an isolated --fresh stack
```

Check before believing a schema failure:
```bash
docker exec <pg-container> psql -U postgres -d opentranscribe -tAc "SELECT version_num FROM alembic_version;"
```

Same family as everything else in this file: a red number that describes something other than
what it appears to. A green one from the wrong schema is worse.

## Key files

- `conftest.py` — sets env **before** `app.*` imports (DB/MinIO creds via `dotenv_values(.env)`,
  `POSTGRES_HOST=localhost:5176` — see the migration warning above). `db_session` = savepoint
  isolation surviving `commit()`;
  `client` overrides `get_db`; an autouse session fixture patches `Task.apply_async` so
  `.delay()` never reaches a real broker.
- `e2e/conftest.py` (`login_page`, `authenticated_page`, `auth_helper`, `api_helper`,
  ffmpeg-generated `sample_audio`/`sample_video`; creds `admin@example.com`/`password`) and
  `e2e/pytest.ini` (its own marker set, `--browser chromium`).
- `onnx/conftest.py` — `--onnx-device` (named around pytest-playwright's `--device`); skips
  without `models/onnx/{segmentation,embedding}.onnx` or `HF_TOKEN`.
- Golden fixtures: `fixtures/boundary/karpathy_10m.{rawinfer,ref.words,baseline}.json` (frozen
  GPU output replayed through the CPU smoother — WSER/island/DER drift gate) and
  `fixtures/redaction/{segments,expected_label_style}.json`.

## Markers and gates

- Registered (pyproject): `slow`, `unit`, `pki`, `e2e`, `integration`, `gpu`, `models`. `addopts` =
  `-n auto --dist loadgroup --tb=short -q --strict-markers -m 'not integration and not gpu'`;
  `norecursedirs=["tests/e2e"]`. **`--strict-markers` makes an unregistered marker a collection
  error** — register any new marker in `[tool.pytest.ini_options] markers` or collection fails.
  (The e2e markers live in `tests/e2e/pytest.ini`, which is a separate rootdir config.)
- `@pytest.mark.models` = needs Presidio/GLiNER/toxicity weights; those modules also
  `importorskip` + `preload()`-skip, so fast CI passes without weights.
- Module-level `skipif` env gates → suite: `RUN_PKI_TESTS`→`test_pki_auth`, `RUN_MFA_TESTS`→
  `test_mfa_security`, `RUN_LLM_TESTS`→`test_llm_settings`, `RUN_FEDRAMP_TESTS`→
  `test_fedramp_compliance`+`_controls`, `RUN_FIPS_TESTS`→`test_fips_140_3`,
  `RUN_AUTH_CONFIG_TESTS`→`test_auth_config_service`, `RUN_ADVANCED_ADMIN_TESTS`→
  `test_admin_security`, `RUN_SEARCH_QUALITY_TESTS`→`test_search_quality` (self-seeding — injects
  its own 6-meeting corpus via `app/scripts/corpus_injection` through a throwaway `searchqual-`
  user, see `tests/fixtures/search_corpus.py`; still deliberately never in CI, since CI forces
  `SKIP_OPENSEARCH=True`), `RUN_SCHEMA_DRIFT_TESTS`→`unit/test_schema_drift` (needs the live
  migrated DB; now its own phase in `run-integration-tests.sh` — it was previously set only by
  the release pipeline's `warn`-severity `schema-drift` criterion, so it never ran pre-merge),
  `RUN_AUTH_E2E`→`e2e/test_ldap_oidc` + LDAP half of
  `e2e/test_auth_buttons`, `RUN_PKI_E2E`→`e2e/test_pki`.
- **MinIO/OpenSearch tests auto-enable by TCP probe.** Root conftest `_service_reachable`
  (0.3 s) `setdefault`s `SKIP_S3` from `localhost:5178` and `SKIP_OPENSEARCH` from
  `localhost:5180`, then points the clients at those host ports; an explicit shell value wins.
  Stack down → those suites **skip silently**, so a green local run proves less than it looks.

## Test-quality gate — `scripts/audit-tests.py` + `audit-allowlist.txt`

**Runs on every commit** (`.pre-commit-config.yaml`: `audit-tests-selftest` then `audit-tests`)
and in the `backend-tests` CI job. Before this it was in neither — `rg audit-tests` found only
prose. 16 AST detectors; full inventory and the calibration traps live in `scripts/CLAUDE.md`.
What matters when you are writing a test here:

- **`tests/e2e` is scanned too.** It used to be excluded, which hid 21 findings in the only
  suite that drives a real browser.
- **The shapes it rejects**, and the fix each one wants:
  - `assert r.status_code != 403` → assert the exact status. `!=` passes on a **500**, so
    seven "admin can access" tests in `api/endpoints/test_permissions.py` passed against a
    completely broken endpoint.
  - the real assertion inside `if r.status_code == 200:` → assert `== 200` unconditionally
    first, then assert the payload. Two of these survived in `test_permissions.py` after their
    siblings were fixed, because a weak unguarded assert beside them hid them from
    `conditional-only`.
  - every assertion inside `for x in <runtime iterable>` → add a non-emptiness assertion
    *outside* the loop. `redaction/test_presidio.py::test_offsets_slice_back` is the fix shape:
    zero detected spans ran the loop zero times and passed, on the invariant that
    `redaction/spans.py` is a mutation target *for*.
  - `assert some_local` where the local is an `or` chain → assert the value you mean.
  - only `mock.assert_called_once_with(...)` → assert real state as well. See
    `unit/test_dispatch.py`: it patched `update_media_file_status`/`update_task_status`/
    `send_error_notification` — **the effects `on_pipeline_error` exists to produce** — so a
    handler that left a file `processing` forever kept 13 of 15 tests green. It now creates real
    `MediaFile`/`Task` rows and asserts on them, patching only `session_scope` (the handler
    opens its own session, invisible under the savepoint harness) plus the MinIO/Redis/WebSocket
    seams, once, in one fixture.
  - `except ...: pass` in a test body → let it raise. The assertions inside the `try` silently
    stop running and nothing reports it.
- **Allowlist entries are `<file>::<test>::<category>  # reason`, all three segments required.**
  A reason starting `BACKLOG` marks deferred work and is counted and printed separately on
  every run, so a green gate is never read as a clean tree. A **stale** entry — one whose
  finding is gone — **fails the run**: fix a test, delete its line. Never widen an assertion to
  clear a finding.
- **Run `python3 scripts/audit-tests.py --selftest` after touching a detector**, and give any
  new one a must-fire *and* a must-stay-clean fixture. `unit/test_audit_tests_selftest.py` runs
  all 54 cases under pytest for the same reason: a detector that matches nothing reports zero
  findings, which is indistinguishable from a clean suite.

## Safety rules (non-negotiable) — enforced by `unit/test_e2e_data_hygiene.py`

These three rules used to be prose only, and had already been broken: a registration test
with a hard-coded `test@example.com` created a real account on the live stack that had to be
deleted by hand. `unit/test_e2e_data_hygiene.py` now enforces all three by AST over
`tests/e2e/*.py`, in the **fast unit suite** — before any browser touches the stack. Each
check has an allow-list requiring a **written reason** (the `test_ddl_marker_discipline.py`
pattern), and six "guard the guard" tests so a scanner that silently matches nothing cannot
pass everything — one of which caught exactly that: the UI-creation half matched by string
equality against selectors that embed the label (`"button:has-text('Create Account')"`), so it
was finding nothing at all.

- **E2E must never persist changes to dev data.** Upload tests delete what they create (API
  delete, falling back to `/force`); transcript-edit tests use the **cancel path only**.
  Cleanup must be in a `finally`, a fixture teardown that deletes, or an `addfinalizer` — **a
  delete on the happy path does not count**, because the assertion most likely to fail is the
  one *after* the object was created. A fixture's `page.close()`/`context.close()` teardown is
  explicitly not accepted as data cleanup.
- **Never name a persistent object with a fixed identity.** Emails, created-object
  `name`/`title` payloads, and values typed into a name field all need a `uuid4`/random
  suffix. `admin@example.com` / `password`, `nonexistent@example.com`,
  `nosuchuser-e2e@example.com` and the `ldap-*`/`kc-*` IdP fixtures are the allow-listed
  shared identities. `scripts/cleanup-test-users.py`'s `ORPHAN_PATTERNS` is the backstop for
  runs that die mid-flight — **add your prefix there in the same commit** (`mfa-e2e-` was
  missing for as long as `test_mfa.py` leaked one account per run).
- **Negative-login tests must use a nonexistent account** (`nonexistent@example.com`,
  `nosuchuser-e2e@example.com`, `ldap-nosuchuser-e2e`), never a wrong password for
  `admin@example.com` — lockout is keyed on the **resolved account** (`canonical_identifier`
  collapses an `ldap_uid` onto the account's email) and escalates, so one such test poisons
  every later test that logs in as that account. Where the real-user branch genuinely differs
  — an LDAP bind rejection — use an account that never authenticates successfully anywhere in
  the suite (`test_ldap_oidc.py`'s `LDAP_NEGATIVE_USER`), so no local `User` is ever
  provisioned and its lockout bucket belongs to nothing. Registration forms are exempt from
  this check: they have `#email`/`#password` fields but submitting one is not a login.
- Dev relaxes auth limits (`docker-compose.override.yml`: `RATE_LIMIT_AUTH_PER_MINUTE=120`,
  `ACCOUNT_LOCKOUT_THRESHOLD=100`, `DEV_*`-tunable). **Prod never loads that overlay** — don't
  write a test that only passes under the relaxed values. `shared_auth_state`/`gallery_page`
  exist to log in **once per session** for the same reason.

## Before you debug a "flaky" E2E test, rule out the STACK (issue #431)

Three separate mystifying per-test failures all turned out to be the environment, not the
tests. `scripts/e2e/run-e2e.sh` only checked that **ports 5173/5174 were open** — and a
restarting container keeps its published port open, so that check passed throughout. A
session-scoped autouse preflight in `e2e/conftest.py` now catches both causes in ~3.4 s:

- **A flapping backend.** `+layout.svelte` renders the ENTIRE app behind `{#if $authReady}`,
  and `authReady` is set only after `initAuth()`'s `GET /auth/session` resolves — behind a
  **60 s** axios timeout. So any backend stall makes `#email` absent for up to a minute and
  *every* login-page fixture times out at once, with a different subset landing in each stall
  window. That reads exactly like order-dependent flakiness and is not. Observed: uvicorn
  reloading **19× in 5 minutes** (`/health` up on 9 of 40 samples) while a `pre-commit` run
  stashed and restored the tree — **so running `pre-commit` while E2E runs manufactures E2E
  failures.** The preflight requires **3 consecutive** `/health` 200s; one lucky probe cannot
  clear a flapping backend.
- **A split Vite module graph.** ES modules are keyed by URL, so a store served under two
  `?t=` stamps becomes **two independent store instances** — one written by the layout, one
  subscribed by a component that then sees `null` forever. This made a promote button's
  `canPromote` false with correct code, correct data and a correctly-authenticated session;
  a content-free `touch` fixed it. `split_store_modules` detects it; it degrades to a no-op
  on the prod/nginx overlays.

Both have must-fire and must-stay-clean cases in `e2e/test_preflight_guard.py`.

⚠️ **An absence-asserting sibling cannot catch this class.** `test_promote_control_absent_for_
an_already_shared_tag` passed throughout, because a broken store produces absence too.

## Gotchas

- **Subdirectory-conftest fixtures vanish from a mixed file selection (issue #454, pytest 9.1).**
  `pytest tests/unit/a.py tests/b.py tests/unit/c.py` used to give
  `fixture 'run_in_clean_process' not found` — **3 setup ERRORS, not failures**, so the
  fail-closed-environment and Celery-reliability guards silently stopped running. Mechanism:
  pytest ≥ 9.1 matches a conftest's fixtures to tests by **collector-node object identity**
  (`FixtureManager._matchfactories`) and registers each conftest exactly once (`pop` from
  `_pending_conftests`), while `Session.collect` **rebuilds a directory's children** whenever an
  argument ends at a file inside it (`handle_dupes=False`). Leaving a subdirectory and returning
  therefore hangs the later file off a second, unregistered collector.
  **The `__init__.py` asymmetry is NOT the cause** — a minimal repro errors identically with
  both, neither, or either present, and `tests/api/` (no `__init__.py`) breaks the same way.
  Bisected: 8.4.2 and 9.0.3 clean, 9.1.0/9.1.1 broken, 9.1.1 is newest. Worked around by
  `fixtures/dir_collector_memo.py` (memoises directory collectors; registered from the root
  conftest's `pytest_plugins`) and pinned by `unit/test_conftest_fixture_visibility.py`, whose
  must-fire control **skips with removal instructions** once upstream fixes it. Confirmed
  victims were `run_in_clean_process` + `revisions_at_or_after` (`unit/conftest.py`) and
  `org_context` + `organizations_capability_on` (`api/conftest.py`).
- **`tests/` must never gain an `__init__.py`.** Prepend import mode would then root
  `tests/conftest.py` at `backend/`, `backend/tests` would never reach `sys.path`, and
  `pytest_plugins = ["fixtures.mock_llm", ...]` dies with `No module named 'fixtures'` — the
  same reason `--import-mode=importlib` is unusable here.
- **`tests/integration/` is a directory name, not a marker.** Its contents split three ways:
  `integration`-marked (need the live stack), `gpu`-marked (boundary/diarization regression,
  lifecycle, perf gates), and three deliberately service-free tests in
  `test_metering_pipeline.py` that belong in the fast suite. The gate script runs the first two
  as separate phases (`-m integration`, then `-m gpu`); the fast suite and CI deselect both.
  Before #297 `gpu` was unregistered and silenced by a `PytestUnknownMarkWarning` filter, so
  those 17 tests ran in the fast suite *and* CPU-only CI, passing only on their own runtime skip
  guards, while the gate selected none of them.
- `db_session` rolls back the DB, **not MinIO or OpenSearch**. Hence `upload_test_file`'s API
  delete and the forced `AUDIT_LOG_TO_OPENSEARCH=false` — savepoints can't undo index writes
  into the live dev cluster.
- `--dist loadgroup`: tests sharing mutable global state need
  `pytestmark = pytest.mark.xdist_group("<name>")` (`test_auth_config_integration.py`,
  `unit/test_media_mirror_service.py`, `api/test_proxy_auth_endpoint.py`,
  and — group per overlapping `SystemSettings` key namespace, issue #389 — `"backup_system_settings"`
  on `unit/test_backup_metrics.py` + `unit/test_backup_service.py` + `unit/test_backup_alerts.py`,
  `"engine_system_settings"` on `test_engine_settings.py` + `api/test_engine_settings_endpoints.py`)
  or they interleave across workers and can deadlock on `system_settings_key_key`
  (two workers inserting the same overlapping keys in reversed order). User fixtures
  use UUID-suffixed emails for the same reason.
- **DDL tests need more than `xdist_group`, and the marker goes on the TEST, never the module.**
  `DROP TABLE`/`ALTER TABLE ... DROP CONSTRAINT` takes `ACCESS EXCLUSIVE`, and when the dropped
  object is a foreign key Postgres needs that lock on the *referenced* table too (removing the
  FK's enforcement trigger, which lives there) — dropping `scim_token` also locks `user`, and
  `v377`/`v381` `ALTER TABLE "user"` directly. Since nearly every other test touches a `user`
  row, that can deadlock against any worker (issue #389). `xdist_group` cannot fix it: sharing a
  worker only stops DDL tests colliding with *each other*. `@pytest.mark.ddl_exclusive` makes
  `db_session` take a Postgres advisory lock (`tests/db_locks.py`) — SHARED for every ordinary
  test, EXCLUSIVE for a `ddl_exclusive` test — which is real cross-worker mutual exclusion.
  **Every EXCLUSIVE acquisition is a stop-the-world barrier**: it drains all other workers and
  queues every new one behind it. Applying the marker at module scope therefore turns each
  read-only schema assertion in the module into a full-suite barrier — that is how the
  `migration_ddl` group came to be 414 s of a 511 s wall clock with 111 tests marked and ~12
  actually running DDL (issue #431). `unit/test_ddl_marker_discipline.py` enforces both
  directions by AST: DDL without the marker fails, and the marker without DDL fails. It
  discriminates *executed* DDL from DDL merely mentioned in a string, because three suites
  assert on a migration's own source text and one passes `"'; DROP TABLE media_file; --"` as an
  injection payload. `CREATE TEMP TABLE` is exempt (session-private `pg_temp_*` schema).
  A test that opens its **own** connection cannot be reached by the marker — it must call
  `tests/db_locks.py`'s `acquire_ddl_lock_exclusive[_raw]()` itself, as
  `unit/test_uuid7_migration_guard.py` does for the raw v368 guard block.
- E2E runs from the repo root against `backend/tests/e2e/`, so `e2e/pytest.ini` becomes the
  rootdir config — pyproject `addopts` (`-n auto`, `-m 'not integration'`) do **not** apply.

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
(JWT/bcrypt), `api/endpoints/auth/dependencies.py` (the privilege gates — an inverted role
comparison is privilege escalation), `auth/lockout.py`, `auth/session.py`. A whole-codebase run
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
test client) · `lockout` ~30-90 min · `session` ~15-45 min · `--all` is **hours**. Needs
Postgres (5176) and, for lockout/session, Redis (5177). CPU-only — it never touches the GPU,
but it will saturate every core, so don't start one beside a benchmark.

**Two traps the script handles, and you must not undo.** `-n0` is appended via
`PYTEST_ADDOPTS` to override pyproject's `-n auto`: without it every mutant forks one xdist
worker per core against the shared DB. And the `RUN_*` gates are exported, because three of the
selected test files are behind a module-level `skipif` and **a skipped test kills no mutant** —
an ungated run reports false survivors that look exactly like real findings.

**Reading a surviving mutant.** `--results` lists ids; `--show <id>` prints the diff.
`killed` = genuinely checked. `survived` = the finding; add the missing assertion, or conclude
the line is dead and delete it. `timeout` counts as killed but check it is not a real infinite
loop the tests were papering over. `suspicious` = far slower than baseline, usually a mutated
retry/sleep bound. Survivors in *log lines and error strings* are noise — judge by whether a
real caller could observe the difference.

## Running DB-backed tests without the dev stack

Most API/service tests need Postgres but nothing else. When the dev stack is down (or is
being used by someone else), a throwaway instance on a non-conflicting port is enough — it
cannot touch the dev database, its volumes, or the NAS data:

```bash
docker run -d --rm --name ot-testdb \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=testpw -e POSTGRES_DB=transcribe_test \
  -p 127.0.0.1:55432:5432 --tmpfs /var/lib/postgresql/data postgres:17.5-alpine

mkdir -p /tmp/ot-test/{data,models,temp}
export DATA_DIR=/tmp/ot-test/data MODELS_DIR=/tmp/ot-test/models TEMP_DIR=/tmp/ot-test/temp
export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432 POSTGRES_USER=postgres POSTGRES_PASSWORD=testpw \
       POSTGRES_DB=transcribe_test SKIP_S3=True

cd backend && python -m alembic upgrade head && python -m pytest tests/ --ignore=tests/e2e
docker stop ot-testdb   # --rm removes it
```

Why each piece:
- **Port 55432**, not 5176 — conftest only overrides `POSTGRES_PORT` when it is unset or
  `5432`, so any other value is respected, and the dev stack's port stays free.
- **`POSTGRES_HOST` explicitly** — conftest forces `localhost` for pytest, but `alembic`
  does not import conftest and would otherwise resolve the compose service name.
- **`DATA_DIR`/`MODELS_DIR`/`TEMP_DIR`** — `config.py` defaults these to `/app/...` and
  `Settings.__init__` mkdirs them, which fails outside the container.
- **`SKIP_S3=True`** — conftest TCP-probes `localhost:5178` and enables S3-backed tests if
  *anything* answers. An unrelated service on that port produces `SignatureDoesNotMatch`
  failures that look like real bugs.

## `tests/eval/` — the RAG evaluation harness (issue #403 Stage 1)

Not a test suite that gates anything: it is the **instrument** every retrieval-affecting change
reports against (D5), plus the tests that keep the instrument honest. `synthetic/` generates a
corpus with ground truth known by construction; `harness/` measures.

| Module | Owns |
|---|---|
| `harness/metrics.py` | `trec_eval` via `pytrec_eval_terrier`. Tie normalisation, `-c` semantics, linear gain — all three are NOT the library default |
| `harness/qrels.py` | gold turn ranges -> chunk-level graded judgements. **One adapter for QMSum and the synthetic tier** — they share the inclusive turn-range convention deliberately |
| `harness/corpora.py` | queries + gold, remapped onto the uuids the app indexed, with the licence tier attached |
| `harness/index_reader.py` | **settle** (complete + stable + nothing predating the run) -> refresh -> force-merge -> refresh, then read chunks back |
| `harness/runner.py` | drives `retrieve_chunks` (the chat path), never `/api/search`; owns the per-request fusion arm and the 48/12/4 budget |
| `harness/report.py` | the deterministic results document and metric table |

```bash
./opentr.sh bench rag --fresh rag403              # the one command, <5 min
pytest tests/eval -q                              # logic tests, nothing running
OPENSEARCH_PORT=5280 pytest tests/integration/test_rag_eval_harness.py -m integration
```

Three things to know before touching it:

- **The metric engine is an eval-only dependency** (`backend/requirements-eval.txt`) for a
  **licence** reason, not a size one: trec_eval's C sources carry a "research, non-commercial
  purposes" header and we publish images. Never move it into `requirements.txt`. Every module that
  imports it does so lazily and every test `importorskip`s it with that reason.
- **`normalise_run` is load-bearing, not tidiness.** trec_eval breaks ties by docid *descending*,
  our ids are `{uuid}_{chunk_index}` and `{uuid}_digest_{n}`, and RRF produces ties structurally —
  so an untied-broken run lets a stage pass its own gate on document naming. `test_eval_metrics.py`
  swaps the id convention and asserts the metric is unchanged, with a guard test proving the
  hazard is real; it reads the digest id from `index_mapping.digest_document_id` rather than
  spelling it, because it was already guarding a scheme the app had moved past once.
- **A measurement is refused unless the corpus has settled.** `await_settled` requires every
  expected file to carry chunks, the (files, chunks) pair to repeat across two polls, **and** —
  when a dispatch timestamp is passed — nothing in the corpus to predate the run. The first two
  alone are satisfied by a reindex that has been dispatched and has not started, which certifies
  the old index as the new one. Polling the chunk total alone produced phantom deltas of
  223 / 357 / 591 chunks over an unchanged corpus.
- **A sweep arm is one flag combination, and the results file names it.** `--fusion` /
  `--rank-constant` / `--normalization-technique` / `--combination-technique` /
  `--combination-weights` select the hybrid fusion strategy per run (#363); `--size` /
  `--final-chunks` / `--max-per-file` / `--rerank-max-pairs` are the 48/12/4 budget, whose
  defaults are pinned to the shipped `chat.rag.*` constants by `test_eval_fusion_arm.py` (they
  used to be 20/3 — every `--stage rerank` number described a deployment nobody runs).
  `metrics.json`'s `retrieval.fusion` records the *resolved* strategy and pipeline id, so no arm
  can be unattributable; per-query latency lands in `runinfo.json`, outside the deterministic
  claim. **Never quote a single latency run** — one put `rrf-60` at +50% p95 and it did not
  reproduce.
- ⚠️ **`--stage rerank` is scored in the order the PROMPT receives, not by score.**
  `_to_run_docs(..., preserve_order=True)`. `normalise_run` re-sorts by `-score`, which is right
  for `retrieve` and wrong after `diversity_sample`, whose job is to interleave files — the
  re-sort undid that for **40 of 60** measured queries and made the scored top-5 depend on
  `final_chunks`, which provably cannot move the prompt's top-5. It also let the *un-reranked*
  tail outrank reranked hits whenever `candidate_pool > rerank_max_pairs`, because `rerank`
  leaves the tail on RRF scores (small positives) while cross-encoder scores are routinely
  negative. Production was never affected; it walks list order.
- **`scripts/reindex_eval_corpus.py`** dispatches the real `reindex_transcripts` task and waits
  for the settle. Two consecutive runs must report the same chunk count; that equality is the
  precondition for any phase-over-phase delta, and it is measured, not assumed.
- **Baselines under `tests/eval/baselines/` are committed controls.** `metrics.json` and
  `metrics.md` are byte-identical across runs by construction; anything non-deterministic
  (elapsed time, target) lives in `runinfo.json`, outside the claim. Regenerate one only when the
  corpus composition genuinely changes, and say so in the PR.
- **`scripts/probe_chat_rag.py` (issue #72) is the one tool in this family that drives the real
  chat HTTP path** (login -> scoped conversation -> POST message -> SSE -> re-fetch
  `msg_metadata`) against a real LLM, rather than calling `retrieve_chunks` in-process. Its
  question sets are supplied at runtime via `--question-set` (a JSON file, never hardcoded) so the
  tool itself carries no licence-encumbered content; `--metrics-out` writes a metrics-only artifact
  (`harness/probe_metrics.py`, `assert_no_prose` enforced) that IS safe to commit, while `--out`'s
  full-fidelity report (question/reference/answer prose) is NOT. Two environment gotchas
  (vLLM's `docker network connect --alias`, and the CSRF double-submit header) and the committed
  `probe-chat-live-2026-08-20` baseline are documented in
  `docs-site/docs/developer-guide/rag-evaluation.md`'s "Live chat-RAG HTTP probe" section — read it
  before pointing this at a fresh stack, or the first mutating request 403s with no obvious cause.

Methodology, the overlap->relevance rule, and the committed numbers:
`docs-site/docs/developer-guide/rag-evaluation.md`.

## Chat suites (issue #52)

| File | Needs |
|---|---|
| `unit/test_llm_streaming.py`, `unit/test_chat_{prompting,retrieval,citations,redactor,hooks,limits}.py` | nothing — mocks only |
| `unit/test_v374_migration_consistency.py`, `test_chat_{context_resolver,endpoints,user_settings,gdpr_erasure}.py` | Postgres |
| `e2e/test_chat.py` (marker `chat`) | full stack + an LLM provider + a completed transcript |
| `e2e/test_chat_grounding.py` (marker `chat`) | full stack + `--with-mock-llm` + a completed transcript |

`test_chat_grounding.py` covers the #384 invariant end to end: **the citations a
user can click are exactly the excerpts the model was given.** It registers its
own per-test LLM config rather than reusing the shared provider, because the
whole point is to control `max_tokens` — the user-declared context window the
excerpt budget is computed against. A 512-token window (the API minimum) makes
`budget_chars` floor at 0 against the ~2 KB base rules, so *no* excerpt fits and
the `context_dropped` warning fires deterministically. `ROOMY_CONTEXT_WINDOW` is
the control: same code path, opposite outcome, driven only by config — without it
the suite would still pass if the notice were rendered unconditionally.

**Two traps that read-order gets wrong** (both cost a debugging cycle):

- **Retrieval diagnostics are not on screen mid-stream.** A streaming message
  holds only what the SSE frames supplied; `msg_metadata.retrieved` /
  `chunks_used` live on the persisted row and arrive when the thread is fetched.
  Read the Details panel only after `_reload_thread()`, or it has no counts at all.
- **Persisted citations ≠ offered citations.** `_persist_reply` stores
  `used_citations` — the ones the answer actually referenced — so after a reload
  the source cards are a *subset* of what the `sources` frame offered. Count
  offered cards DURING the stream; compare against `chunks_used` read after.

`test_chat_endpoints.py` patches `app.db.session_utils.session_scope` to the test session.
The streaming service deliberately opens its **own** session (it outlives the request's
dependency scope), which under the savepoint harness cannot see uncommitted rows — without
the patch, persistence fails on a foreign key that is perfectly valid in production.

## Mock LLM provider (no GPU, no API key)

`scripts/mock-llm-server.py` is a real OpenAI-compatible server that cans only
token generation. Everything else stays real — retrieval, redaction masking,
citation assembly, SSE parsing, usage recording — which is why it is preferred
over monkeypatching `LLMService`: a patched client proves the mock behaves, not
that the app does.

```bash
./opentr.sh start dev --with-mock-llm     # http://mock-llm:5199/v1 in-network
```

Fixtures live in `tests/fixtures/mock_llm.py` (registered via `pytest_plugins`
in the root conftest, so no per-file import):

| Fixture | Use |
|---|---|
| `mock_llm_url` | URL the TEST process can reach. Reuses the container, else starts a subprocess — never skips |
| `mock_llm_base_url_for_backend` | URL the BACKEND CONTAINER can reach. **Skips** without the container: a host subprocess is invisible to it |
| `mock_llm_completion` | Call the mock directly; returns `{status_code, body}` |
| `register_mock_llm_provider` | Configure the app's `custom` provider at the mock, deleted on teardown |

Scenario models select behaviour by `model` name, so a test picks a failure mode
by *configuring a provider* and the app runs its REAL error handling:

| Model | Behaviour |
|---|---|
| `mock-gpt` | normal reply with `[1]`/`[2]` citations, markdown, code block |
| `mock-echo` | echoes the prompt received — assert what the app **sent** (masking applied? prompt layers ordered?) |
| `mock-empty` | completes with no content |
| `mock-error` | HTTP 500 before any token → `provider_error` frame |
| `mock-slow` | stalls past the first-token watchdog |
| `mock-reasoning` | a "thinking" phase before the same `[1]`/`[2]` answer as `mock-gpt`. ⚠️ **Separates it only when the request activated thinking** (`chat_template_kwargs={"enable_thinking": true}`); unasked it reproduces #439 — the thoughts arrive on `delta.content` with a bare `<channel|>` closer, exactly as vLLM does — so both branches are testable. The field is `delta.reasoning` on vLLM 0.19, not `reasoning_content`; our parser reads both |

**CI needs no setup**: the subprocess fallback means `tests/unit/test_mock_llm_fixture.py`
runs in the GitHub `backend-tests` job with no compose stack. Tests that need the
*backend* to reach the mock must use `mock_llm_base_url_for_backend` and will skip
in CI rather than fail.

**Each new migration breaks the previous suite's detection assertion.** `_detect_schema_version`
returns the newest matching revision, so when you add vNNN, widen the vNNN-1 test to accept
either value and pin the exact one in your own suite.
