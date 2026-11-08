# backend/tests/e2e — E2E safety rules and cleanup

Moved intact from `backend/tests/CLAUDE.md`. Fixtures, markers and the `RUN_*` gates stay in `backend/tests/CLAUDE.md`.

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

- **A test that executes real infrastructure tooling must be scoped to a namespace no real
  object can occupy — and the scoping must be asserted, not intended** (issue #693).
  `unit/test_opentr_stop_container_scoping.py` extracts `opentr.sh`'s straggler-cleanup loop
  and *runs* it, which is the whole value of the test — and, unscoped, ran
  `docker stop && docker rm` over the live dev stack: **17 containers destroyed by a routine
  `pytest tests/`**, from a file in `tests/unit/` with no marker. It now drives the loop through
  `OPENTR_STOP_PROJECT_LABEL`/`_ALT` (defaulting, in `opentr.sh`, to the real project names) at a
  per-test `*-pytest-<uuid4>` compose project, with three guards: a fixture that **refuses** to
  hand the loop to a real-docker test unless every project-label filter is an overridable
  `${...}`; a fake-`docker`-on-`PATH` test asserting the filters it actually issued name only
  that namespace; and a test pinning the unset default to the two real names. Skipping the
  scoping is not a smaller version of this test — it is the incident.
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
  missing for as long as `test_mfa.py` leaked one account per run). For everything that is
  NOT a user email — uploaded media filenames, collection/tag/watch-source/speaker-profile
  names, chat conversation titles — the equivalent backstop is
  `scripts/cleanup-test-data.py` (issue #629), registered in
  `backend/tests/unit/test_cleanup_test_data_safety.py`'s prefix-registration gate: a new
  `..._PREFIX = "foo-"` constant anywhere under `backend/tests` that isn't in that script's
  `MEDIA_FILENAME_SPECS`/`NAME_PREFIXES` fails the fast unit suite, not silently leaked
  forever. Chat conversation titles use `e2e/conftest.py`'s `unique_conversation_title()`
  helper (prefix `E2E_CONVERSATION_PREFIX = "e2e-chat-"`) — deliberately its own helper, not
  a reuse of `test_chat.py`'s `_unique()`, which also names LLM provider configs and would be
  wrong to prefix the same way. Both scripts are unconditionally invoked (Tier A only, via
  `cleanup-test-data.py --execute-unambiguous`) at the start AND, from an EXIT trap, at the
  end of `scripts/run-integration-tests.sh` and `scripts/e2e/run-e2e.sh` (and once, after all
  phases, by `scripts/run-dev-tests.sh`), degrading rather than blocking the run on failure
  and never changing its exit code; `OT_SKIP_TEST_DATA_SWEEP=1` is the escape hatch. See
  "Cleaning up after tests" below for the rest of the rule.
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
  `ACCOUNT_LOCKOUT_THRESHOLD=100`, and — since issue #632 — `MAX_CONCURRENT_SESSIONS=1000`, all
  `DEV_*`-tunable). **Prod never loads that overlay** — don't write a test that only passes
  under the relaxed values. `shared_auth_state`/`gallery_page` exist to log in **once per
  session** for the same reason. `MAX_CONCURRENT_SESSIONS` sits on the `x-dev-environment`
  ANCHOR rather than on `backend` alone, unlike the other two: the nightly `session.cap_sweep`
  runs on `celery-cpu-worker`, and if that process disagreed with `backend` about the cap it
  would evict the dev stack's sessions back to 5 every night. Don't add a session-cap e2e test
  either — it would exercise the relaxation, not the cap; that control is covered at unit/API
  level only (`backend/tests/unit/test_session_ceiling.py`,
  `backend/tests/unit/test_session_cap_sweep.py`).

## Cleaning up after tests (issue #1195)

**A test leaves nothing behind — not on success, not on failure, not when its worker is
killed.** That covers everything OUTSIDE the `db_session` savepoint: OpenSearch indexes,
pipelines and documents, MinIO buckets and objects, Postgres databases and live-committed
rows, docker containers/networks/volumes, Redis keys and `/tmp` trees. Audit and per-file
status: `docs/design/v060_test_hygiene_audit.md`.

Three layers, each needed:

1. **Teardown on every failure path** — enforced by `unit/test_test_artifact_teardown_discipline.py`
   (AST). A creation (`indices.create`, `put_pipeline`, `make_bucket`, `CREATE DATABASE`,
   `docker run -d` / `network create`, `mkdtemp`/`mkstemp`/`NamedTemporaryFile(delete=False)`)
   must sit inside a `try` whose `finally` removes it, or be the statement IMMEDIATELY before
   that `try`. "Create, seed, then try" is the defect it exists for: eight index fixtures
   bulk-seeded between the create and the `try`, so a failed seed leaked the index. A helper
   that creates and returns hands the obligation to its caller. Its `_ALLOWED` dict needs a
   reason per entry and is empty.
2. **A registered shape for everything a killed run can leave** — enforced by
   `unit/test_test_artifact_sweep.py` (every index/pipeline/bucket/tmp-prefix name a test mints
   must match a shape in `scripts/lib/test_artifact_sweep.py`) and
   `unit/test_cleanup_test_data_safety.py` (every uniquified name an e2e test mints — f-strings
   included, not just `_PREFIX` constants — must start with a registered prefix; the leaked
   profile `E2E Propagated <hex8>` was an inline f-string no constant scan could see). Use a
   lowercase registered prefix plus `uuid4().hex[:N]`, never a title-case literal.
   Docker resources a test starts carry `--label ot-test-artifact=1` (`tests/docker_ops.py`
   `TEST_ARTIFACT_LABEL`).
3. **Liveness**: every pytest controller registers a flock marker in the main checkout's
   `.testruns/` (`tests/testrun_marker.py`, via `conftest.py`), so a concurrent sweep never
   removes a store a running test is using, even a bare `pytest` run from another worktree.

Four more rules from issue #1222:

- **Corpus-injected files tear down through `purge_injected`**, not raw ORM deletes (those
  orphan the OpenSearch chunks and speaker embeddings). Mint the seed as `pytest-<hex>`,
  `pytest-synth-<hex10>`, `pytest-searchqual-<hex10>` or `e2e-owned-<hex10>`:
  `INJECTED_TEST_SEED_SHAPE` in `cleanup-test-data.py` is how a killed run's files are found, and
  `unit/test_cleanup_test_data_injected_and_bench.py` fails on an unregistered `seed = f"..."`.
- **An overlay the stack already runs is not the test's to stop** (`e2e/idp_overlay.py`): a
  fixture that starts LLDAP/Keycloak records whether it did, and teardown stops or removes only
  those (`unit/test_idp_overlay_lifecycle.py`).
- **A docker build in a test labels its image** (`ot-test-artifact=1` plus a per-test
  `ot-test-build=<hex>`) and removes images by that label in a `finally`: never by bare name,
  never a prune.
- **Test a bash cleanup trap by running the real script in a throwaway layout** with stub
  `docker`/`opentr.sh` on PATH (`unit/test_pki_leg_teardown.py`); a trap you only read is not a
  trap you have seen fire.

**The sweep** — `scripts/cleanup-test-data.py` (dry run by default; `--execute-unambiguous`
Tier A; `--execute` adds Tier B; `--planes` selects; `--min-age-minutes N`). Rows go through the
app's own DELETE endpoints; store planes re-check each name at the point of deletion and refuse
production names, unregistered names, anything younger than the liveness cutoff, and a run whose
exported `*_PORT`s point at different stacks. Unattended: `./opentr.sh test-cleanup --fresh
<name> [--background]` (scripts/CLAUDE.md). Against an isolated stack always export every port:

```bash
POSTGRES_PORT=5376 OPENSEARCH_PORT=5380 MINIO_PORT=5378 E2E_BACKEND_URL=http://localhost:5374 \
  backend/venv/bin/python scripts/cleanup-test-data.py            # read the dry run first
./opentr.sh test-cleanup --fresh v060 --dry-run                     # same, ports resolved for you
```

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

### E2E (pytest + Playwright)

Tests in `backend/tests/e2e/` (auth, gallery, upload, search, settings, transcript editing, visual). Requires dev environment running (`./opentr.sh start dev`).

```bash
./scripts/e2e/run-e2e.sh                                        # full suite, headless
./scripts/e2e/run-e2e-smoke.sh                                  # quick subset
./scripts/e2e/run-e2e.sh -m upload                              # by marker (upload/search/settings/transcription/gallery/auth/visual)
DISPLAY=:11 pytest backend/tests/e2e/ -v --headed               # visible on XRDP
```
Fixtures, markers, and the `RUN_*` gates: `backend/tests/CLAUDE.md`. Test creds `admin@example.com` / `password`. **E2E suites must never persist changes to dev data** — upload tests delete what they create; edit tests use the cancel path.

Auth-vs-testing rules: the **dev stack relaxes auth security limits** (`docker-compose.override.yml`: rate limit 120/min, lockout threshold 100 — `DEV_*` tunable in `.env`; prod keeps the strict `.env` values since the override is never loaded there). Negative login tests must use a **nonexistent account**, never wrong passwords for `admin@example.com` (progressive per-account lockout poisons the whole suite). Frontend auth is **httpOnly-cookie based** — no JS-readable token; in-page API calls use `fetch(..., {credentials: 'same-origin'})`. `GET /api/auth/session` is the SPA's session probe (200 for anonymous, never 401).

## Failure evidence

`pytest.ini` adds `--tracing retain-on-failure --screenshot only-on-failure` (pytest-playwright options): a failing test leaves a trace zip (per-request network timings, console, DOM) and a final screenshot in `test-results/` (gitignored); open a trace with `playwright show-trace`. For a page that never boots, the trace shows which module request stalled; start the stack with `OT_VITE_DEBUG='vite:*'` to add Vite's transform and dependency re-optimisation log (`./opentr.sh logs frontend`). `test_dev_server_ssr_smoke.py` (first in `run-e2e-smoke.sh`) separates a dev server that does not render `/` or `/login` from a broken test.
