# scripts — test gates, build tooling, and one-off operations

## Purpose

72 mixed shell/python scripts. **`scripts/README.md` (900 lines) already documents the
build / offline-package / model-download / remote-builder family in depth — read it for those
and don't duplicate it here.** It does not mention the other ~60 files (test gates, e2e,
benchmarks, one-off SQL) and contains **zero destructive-operation warnings**. That is what
this file is for.

## Which script to reach for

- **Pre-merge gate** — `run-integration-tests.sh` (`--coverage --e2e-smoke --search-quality --cleanup`).
  Runs the ungated suite, then all `RUN_*`-gated security suites twice (FIPS off, then `FIPS_MODE=true`),
  then `-m integration`, then `-m gpu`, then **model-vs-schema drift**
  (`RUN_SCHEMA_DRIFT_TESTS=true tests/unit/test_schema_drift.py`). That last phase is new: the
  variable used to be set in exactly ONE place — the release pipeline's `schema-drift`
  criterion, at severity `warn` — so the check never ran pre-merge at all, and its warn
  justification ("4 known pre-existing offenders") was stale. It is deliberately NOT in the
  `GATES` array: that array's variables are exported for the `GATED_FILES` pytest run, which
  does not include the drift file, so adding it there would have looked like coverage and
  changed nothing.
- **E2E** — `e2e/run-e2e.sh`: two phases, non-visual in parallel (`E2E_WORKERS`, default 3) then
  `-m visual` serially. `e2e/run-e2e-smoke.sh` is a 4-file subset of it.
  **pytest exit 5 = "no tests collected" is not a failure for a marker-filtered phase.**
  `resolve_phase()` forgives 5, and *only* 5, and *only* for a caller-selected file subset:
  none of the smoke script's four files holds a `visual` test, so phase 2 collected nothing,
  exited 5, and `run-e2e-smoke.sh` **always exited non-zero** — unnoticed because the gate's
  `--e2e-smoke` calls pytest directly instead of going through here. Exit 1/2/3/4 still
  propagate, so a real phase-2 failure fails the run; and on the WHOLE tree 0 collected still
  FAILS, because there it means the `visual` marker itself selects nothing (a renamed marker
  would otherwise delete the entire screenshot suite from the gate in silence).
- **Test-suite quality tooling** (issue #431) — four scripts whose job is to stop a test that
  cannot fail from looking like a passing one:
  - `audit-tests.py <dir>` — **16** AST detectors. The original seven (permissive-status,
    conditional-only, conditional-skip, no-assertion, failure-masking, mock-heavy,
    fixture-named-test) missed 17 of 18 known evasion shapes, so seven more landed:
    `negated-status` (`assert r.status_code != 403` — **a 500 passes**), `status-guarded-assert`
    (the real assertion lives in `if status == 200:`, invisible to conditional-only because a
    weak unguarded assert sits beside it), `loop-only` (every assertion inside a `for` over a
    runtime iterable — an empty result is a green run; **27** of these, incl. seven in
    `test_search_quality.py`, down from a raw 38 once single-assignment resolution stopped
    counting table-driven tests), `unfalsifiable`, `weak-only`, `mock-only`, and
    `error-swallowed` (`except ...: pass`, failure-masking's untested twin). Two more came from
    the #403 RAG work using the overhaul as its first real consumer:
    - `external-service-mock` — **a test whose id claims integration with a service it
      substitutes.** The decided convention: *"ran against the real engine" and "ran against a
      stand-in" must be different test ids.* Twelve tests for an OpenSearch `delete_by_query`
      body were green having never reached OpenSearch. A claim only counts where the test
      DECLARES one — marker/env gate (`@pytest.mark.integration`, `needs_*`,
      `skipif(SKIP_OPENSEARCH, …)`, module `pytestmark`), a realness word in the test's own name,
      or the module path. **The service's own name is NOT a claim** — it names the subject, and
      counting that tier fired on 20 honestly-named unit tests such as
      `test_blacklist_token_redis_unavailable_fail_secure`.
      ⚠️ **Substitution is resolved through the FIXTURE GRAPH, and it has to be.** The real suite
      installs its stand-in in a fixture called `fake_index` — which names no service — so the
      test body has no `patch` call and the parameter name says nothing. The first draft read
      only those two places, fired on its own synthetic `fake_opensearch` fixture, and **missed
      the real file even when deliberately mislabelled into `tests/integration/`**: a detector
      calibrated to its own fixture. `_fixture_services` resolves each fixture's own
      `patch`/`monkeypatch` targets and inherits them transitively (fixpoint iteration, not
      recursion — conftest override chains contain cycles). 388 tests in this tree substitute a
      service; **264 are visible only that way**. Currently **0 findings** — and that is a
      measured claim, not an absent one: the real suite fires under all three claim tiers when
      mislabelled, and stays clean as written.
    - `readiness-probe-target` — **a health/readiness probe whose target is hardcoded rather
      than derived from the stack under test.** `wait_for_bench_backend_health` polled
      `opentranscribe-backend` (the *dev* stack), so the bench stack's readiness wait reported
      healthy whenever dev was up — green-lighting a benchmark against a stack that may not
      exist. Fires only when **every** argument of the probe call is fixed at parse time (single
      assignments resolved, so `f"{BASE_URL}/health"` counts); one derived argument means the
      target follows the stack under test, which is why `_reachable("127.0.0.1", port)` against
      an allocated port stays clean. 3 real findings, all `BACKLOG` — see below.
    **`--selftest` is not optional.** 29 must-fire + 24 must-stay-clean + 1 fires-exactly-once,
    run in-memory; `backend/tests/unit/test_audit_tests_selftest.py` runs the same cases under
    pytest so a dead detector fails the ordinary suite too, and the tree scan refuses to be
    trusted (prints `SELF-TEST BROKEN`) when a fixture stops firing. Three case tiers, and the
    last two exist because the fires/clean pair could not express what they check:
    `SELFTEST_PATH_CASES` supplies a module path (`external-service-mock`'s weakest claim tier is
    *positional* — `tests/integration/`, `tests/e2e/` — and a fixture scanned as `fixture.py`
    cannot reach it), and `SELFTEST_ONCE` asserts a category fires **exactly once**: a probe
    inside a test *method* was reported twice, once under the method and once under `<module>`,
    because `scan_source` scans every function `ast.walk` reaches and then scans module scope
    separately. A must-fire case and a must-stay-clean case both pass happily while that is
    broken. **Verify a new detector by mutation**, not by reading it: disabling each claim tier
    and the fixture resolution in turn must break the self-test, and every one of those
    mutations was caught only after the cases above were added.
    **`tests/e2e` is now in the DEFAULT scan** (`--no-e2e` opts out). Excluding it hid 21
    findings in the only suite that drives a real browser.
    **The mock-heavy count was wrong by 2×**: `_patch_refs` matched both `Name('patch')` and
    `Attribute(attr='object')`, so every `patch.object(...)` scored 2 and `_MAX_PATCH_REFS = 6`
    really meant "3 calls", while `monkeypatch.setattr` scored 0 and was invisible. Counting
    calls dropped 41 findings to **3** — 38 were artefacts of the double-count, not tests.
    Exits 1 on any finding not in `backend/tests/audit-allowlist.txt`, whose keys are
    `<file>::<test>::<category>` with a mandatory reason. **The category is part of the key on
    purpose** — an entry keyed by test alone exempted one test from all six detectors at once.
    Two allowlist rules that make it safe to run as a gate over a pre-existing backlog:
    a reason starting `BACKLOG` marks **deferred work**, counted and printed loudly on every
    run (`165 finding(s) are DEFERRED WORK, not accepted patterns`) so a green gate is never
    mistaken for a clean tree; and a **stale** entry — one whose finding no longer exists —
    **fails the run**, so the file can only shrink and an exemption cannot outlive its subject.
    The three `readiness-probe-target` entries are all live bugs, deferred because they sit in
    files outside the change that added the detector: `fixtures/mock_llm.py` hardcodes
    `CONTAINER_PORT = 5199` (twice) and `test_selective_reprocess.py` hardcodes
    `BASE_URL = "http://localhost:5174/api"`. **Both ports are ones `--fresh --port-offset N`
    MOVES** (`BACKEND_PORT` in `FRESH_PORT_VARS`, `MOCK_LLM_PORT` in
    `FRESH_MOCK_LLM_PORT_VARS`), so against an isolated stack these probe whichever stack owns
    the base port — and because each failure path is a `skip`, the tests disappear rather than
    fail. The fix is the shape the root conftest already uses for Postgres/MinIO/OpenSearch:
    `os.environ.get("<SVC>_PORT", "<default>")`.
    Wired into `.pre-commit-config.yaml` (`audit-tests-selftest` then `audit-tests`,
    `language: system` + `python3` because the auditor is stdlib-only and the CI runner
    installs only `pre-commit`) and into the `backend-tests` CI job. It was in neither before —
    `rg audit-tests` found only prose, which is what a gate nothing invokes amounts to.
  - `analyze-test-timing.py <junit.xml> [--baseline b.xml]` — wall clock vs Σ durations,
    effective parallelism, per-`xdist_group` totals, and the **duration-cluster detector**:
    unrelated tests from ≥3 files inside a sub-second band are a released lock queue, not a
    coincidence. Cluster chaining must cap total band width — chaining on gap alone runs away
    into a single false 13 s "cluster" that is really just dense work.
  - `audit-route-coverage.py [--list|--json|--prefix P|--selftest]` — API routes with no test
    referencing them. **Not a grep, and the reason matters**: the literal-path version reported
    141 uncovered routes when the answer was 28, because the suites build URLs from a base
    constant (`_BASE = "/api/user-settings"` + `f"{_BASE}/download"`), so the full path appears
    nowhere. It resolves module-level string constants per file and matches **structurally**,
    segment by segment — a substring regex scored `/api/tasks/{task_id}` as covered by a test
    naming `/api/tasks/system/fix-file/x`. Both regressions are `--selftest` cases (9 total).
    It measures REFERENCE, not execution, and says so in every run: an upper bound on
    "untested". Currently **51 of 490**, plus 1 WebSocket route (`/api/ws`) that is reported
    separately and is also unreferenced.

    ⚠️ **It reported `0` for months, and that was wrong in three ways at once** — every one of
    them scoring an untested route as covered. The HTTP method was not part of the match key
    (a POST test "covered" a DELETE route: `test_scim.py` has no `client.put` at all, so the
    RFC 7644 *replace* verb had zero tests and passed); an unresolved f-string wildcard could
    stand in for a **literal** path segment (`/api/files/<wild>/<wild>` matched every 4-segment
    route under `/api/files/`, including from a test asserting those routes are *gone*); and
    any string constant counted as evidence, **including the xfail route-inventory tables** —
    so adding a route to `test_route_has_a_caller.py` marked it covered forever. Evidence must
    now be the URL argument of a real HTTP client call. `--fail-on-uncovered` makes it gate;
    the default stays exit 0 so existing callers are unbroken.
  - `run-mutation-tests.sh` — see the mutation section in `backend/tests/CLAUDE.md`. Opt-in,
    never in the gate or CI. **`--clean` when you are done**: it leaves ~330k lines of
    deliberately corrupted source in `backend/mutants/`, which is gitignored but which
    filesystem-walking tools still see (bandit failed a commit on a finding inside a mutant, and
    needs the `*/mutants/*` exclusion because the hook runs `bandit -r backend/` from the root).

    **This script produced four wrong measurements before it produced a right one, and each
    guard below exists because of one.** Read them before trusting a survivor count:
    1. `MODULE_TESTS` omitted a test file → 41 false survivors in `dependencies`, reported as a
       proxy header-spoofing vulnerability. A test that is never selected kills no mutant, so an
       incomplete list does not shrink the run, it manufactures findings. **Guard:** a coverage
       pre-flight prints how much of the target the selected tests execute, and says in red that
       survivors below 60% measure SELECTION, not weakness. It caught `lockout` at 56% on its
       first real use (missing `test_lockout_atomicity.py`, a file named for the module).
    2. A second `--module` silently replaced the first, so `--module a --module b` ran only `b`
       and `a` read as "no findings". **Guard:** it is an error.
    3. mutmut caches verdicts in `backend/mutants/` and reuses them, so a report can mix results
       from an older `MODULE_TESTS`. **Guard:** the test list is fingerprinted beside the cache;
       a change demands `--clean` before the numbers mean anything.
    4. A survivor is a *claim*, and mutmut's own verdict can be wrong. **`--verify <mutant-id>`**
       applies the mutation to the real source and runs the module's tests, reporting
       CONFIRMED-SURVIVED / KILLED / UNVERIFIABLE. It applies the hunk via the diff's **context
       lines** scoped to the mutated function, because mutmut's `@@` offsets are
       function-relative and the Redis and in-memory lockout paths are near-duplicates — a bare
       search-and-replace picks the wrong one, which is exactly how a survivor got misreported
       as already-tested.

    `--verify` transiently edits the live source file. It holds a per-module `flock` (two
    concurrent verifies each restore from their own backup, so one reinstates the other's
    mutation — observed), restores on INT/TERM/ERR/EXIT, and warns when the target is already
    dirty. **Do not commit while a verify runs**: pre-commit stashes the whole tree and will
    capture the mutation. Nothing survives a SIGKILL of the process group — a stopped batch left
    `now <` mutated to `now <=` in `app/auth/lockout.py`, so check `git diff backend/app/auth/`
    after any interrupted run.
  - `frontend/scripts/audit-frontend-tests.mjs` (`npm run test:audit`) — the vitest sibling,
    10 detectors, TypeScript compiler API. Run `test:audit:selftest` after ANY detector change:
    its 21 cases caught two detectors matching **nothing**, which reports 0 findings and reads
    exactly like a clean suite. Two of its detectors do NOT port to Python as-is:
    `toBeFalsy`-style weakness does not, because `assert not offenders, "<list>"` is this
    repo's standard AST-guard shape and fails on any violation — counting the negated form as
    weak reported 40 of those as findings; and a bare `assert predicate(x)` is real evidence
    where `expect(x).toBeTruthy()` is not. `loop-only` also needs single-assignment resolution
    (`endpoints = [...]` then `for e in endpoints:` is as static as the literal) or 22
    table-driven tests are false positives.
- **Fake LLM** — `mock-llm-server.py`: OpenAI-compatible server so chat/AI features work
  without a GPU or API key. Run it via `./opentr.sh start dev --with-mock-llm` (compose
  service `mock-llm`, in-network `http://mock-llm:5199/v1`) rather than by hand — a bare
  host process binds 5199 and then blocks the container. Scenario models (`mock-echo`,
  `mock-empty`, `mock-error`, `mock-slow`) drive the app's real error paths; fixtures and
  the full table are in `backend/tests/CLAUDE.md`.
- **Frontend gate** — `frontend-check.sh`: `npm ci` → `svelte-kit sync` → ESLint → svelte-check → vite
  build. Also the pre-commit hook (`files: ^frontend/src/`) and the `/fix-frontend` command.
- **Publish images** — `docker-build-push.sh`; prefer the skill at `.claude/skills/docker-build-push/SKILL.md`.
- **Models** — `download-models.sh <cache-dir>` is a host wrapper that runs `download-models.py` inside
  the backend image (`DOWNLOAD_ALL_OPENSEARCH_MODELS`, `OPENSEARCH_MODELS`, `WHISPER_MODEL`).
  `fix-model-permissions.sh` chowns the cache to **1000:1000** (the container's non-root `appuser`).
- **Fixtures** — `seed-fresh-deployment.sh`, `setup-watch-source-test-data.sh`, `test-watch-e2e.sh`.
- **Release rehearsals** — `release-tests/`: `test-fresh-install.sh`, `test-upgrade.sh`
  (both auto-detect FROM/TO — see `lib/versions.sh`), with `lib/guardrails.sh` as the
  safety firewall and `lib/{compose-patch,api-client,assertions,versions}.sh`.
- **Cutting a release** — `release.sh` is the ONLY entry point; never hand-run
  `git tag` / `docker push` / `gh release`. 12 stages in `release/NN-<stage>.sh`,
  each independently runnable (`--skip`, `--only`, `--from`, `--dry-run`), with a
  ledger in `.release/<version>/steps/`. `tag`/`publish`/`promote`/`finish` reach
  outside the repo and need `--yes` plus their `ask` rule. Exit codes: 0 pass,
  1 gate, 2 misuse, 3 precondition, 4 abort. `--force-<stage> "reason"` overrides
  a gate with a MANDATORY reason, recorded as `overridden` (not a pass).
  `reset <version>` clears rehearsal history — do it before a real run, or the
  status table reports stale state as current.
  Full guide: `docs-site/docs/developer-guide/releasing.md`.
- **Harness self-test** — `release-tests/selftest-cleanup.sh` (15 cases). Run it
  after ANY change to `lib/guardrails.sh`: it exercises the code that deletes
  volumes, and caught the live-data marker check deleting a volume it should have
  refused (the marker was read from the host, where the root-owned mountpoint made
  every volume look unmarked).
- **Release gates** — `check-schema-drift.py` (model-vs-schema, report-first),
  `validate-deployments.sh` (~20 compose permutations in ~15 s),
  `release/check-version-consistency.py` (the six version sources + Alembic single head).
- **PKI** — `pki/`: `setup-test-pki.sh` (generates the gitignored `test-certs/`), `start-pki-prod.sh`,
  `test-pki-auth.sh`.
- `common.sh` is sourced **only by `opentr.sh`** (docker checks, model-cache chown, OpenSearch model
  bootstrap). `offline-common.sh` is sourced only by the two offline/Windows builders.
- ⚠️ **Every `$VAR` in `opentr.sh` + `common.sh` must be defaulted — enforced by
  `backend/tests/unit/test_shell_expansion_guards.py`** (static, fast unit suite, no execution).
  Both run under `set -uo pipefail`, so an unguarded optional `.env` variable is a hard abort,
  not a style nit: `common.sh` read a bare `[ -n "$GPU_DEVICE_ID" ]` while `opentr.sh` defaulted
  five *other* optional variables and not that one, so `./opentr.sh` died with
  `GPU_DEVICE_ID: unbound variable` in **any checkout without a `.env`** — i.e. every git
  worktree (`.env` is gitignored and never comes along), which blocked exactly the
  isolated-worktree workflow. Guard at the use site (`${VAR:-default}`) or add
  `: "${VAR:=}"` to the `opentr.sh` prologue block; the prologue runs at top level before any
  function, which is why it also covers references inside `common.sh`. Exemptions are a
  `_ALLOWLIST` dict keyed `<script>::<VAR>` with a mandatory reason, and a **stale entry fails**.
  The `_ALLOWLIST` is currently **empty** — the three `BACKLOG` offenders this file used to
  list (`GPU_DEVICE_ID`, `ENVIRONMENT`, `USER`, all in `common.sh`) were fixed, and the entries
  went with them. Read `_ALLOWLIST` in `test_shell_expansion_guards.py` rather than this
  paragraph: a count transcribed into prose is a measurement that rots, and this one had.
  **An assignment inside a function of the other file does not count as a guard** — that is
  precisely how `ENVIRONMENT` slipped past a first draft that pooled assignments across both
  files. `${#VAR}` and `${VAR%…}` are **not** guards (both still abort); escaped `\$VAR` in help
  text, single-quoted `'$VAR'`, and `$VAR` in a comment are not expansions and must not be
  reported — all three were false positives in a draft, and each now has a must-stay-clean case.

## Fresh deployments (`opentr.sh --fresh`, state in `.fresh/`)

Root `CLAUDE.md` points here for the mechanics. `.fresh/` is gitignored and fully regenerated.

- `.fresh/<name>.yml` — the ONLY generated compose overlay. It re-pins every hard-coded
  `container_name` to `otfresh-<name>-*` (`FRESH_NAMED_SERVICES` in `opentr.sh`, plus the aux
  services below when their flag is passed).
- `.fresh/<name>.offset` — the recorded `--port-offset` (plain integer, absent = 0). Read on re-up,
  `status --fresh`, and `fresh-list`; deleted by `fresh-destroy` and by `--port-offset 0`.
- `.fresh/<name>.aux` — the aux overlay files the deployment was started with, one per line.
  `fresh_compose_chain()` replays them so `stop`/`status`/`fresh-destroy` address the same chain the
  deployment was created with. **Required, not cosmetic:** without it the generated overlay re-pins a
  `container_name` for a service compose can no longer see and the whole chain is rejected.
- **`--port-offset` is env-var driven, never an overlay.** `fresh_apply_port_offset()` exports the
  `*_PORT` variables the compose files already interpolate (`FRESH_PORT_VARS`: FRONTEND 5173,
  BACKEND 5174, FLOWER 5175, POSTGRES 5176, REDIS 5177, MINIO 5178, MINIO_CONSOLE 5179, OPENSEARCH
  5180, OPENSEARCH_ADMIN 5181, DOCS 5183). **Never reintroduce a generated `-ports.yml`:** compose
  *appends* port lists across files, so a second `ports:` entry publishes the base port too and the
  "isolated" stack collides with the main one (issue #343). A shell env var beats `.env` for
  interpolation, and a value already set in `.env` is the base the offset is applied to.
- Adding a published port to any compose file `--fresh` can load? Add its variable to the matching
  `FRESH_*_PORT_VARS` array in the same commit, or `--port-offset` silently leaves that one service
  on the main stack's port.
- The pre-flight bind check runs at **every** offset (not just 0) and refuses to start if any
  resolved port is taken, so a bad offset fails before `compose up`.

### Aux test overlays under `--fresh` (issue #347)

`--with-ldap-test` / `--with-smb-test` / `--with-monitoring` / `--with-keycloak-test` /
`--with-authentik-test` are fully isolated: ports via
`FRESH_{LDAP,SMB,MONITORING,KEYCLOAK,AUTHENTIK}_PORT_VARS`, container names via
`FRESH_{LDAP,SMB,MONITORING}_SERVICES` fed into the generated overlay, volumes via the project
name. Keycloak and Authentik declare no `container_name` in their compose files (the compose
project namespaces them automatically), so neither needs a `_SERVICES` entry — only ports and the
aux-file record.

- **`LDAP_TEST_PORT` / `LDAP_TEST_UI_PORT`, never `LDAP_PORT`.** `LDAP_PORT` is the *application's*
  LDAP client port (`.env` ships `LDAP_PORT=636`); offsetting it would repoint the app's LDAP config.
- **Renaming a container is only safe because in-network DNS uses the SERVICE name.** Compose
  aliases each service name on the network (`backend`, `prometheus`, `postgres`, `smb-test`), which
  is what Prometheus' scrape target, Grafana's datasource URL and the SMB docs all use. LLDAP is the
  exception — it is documented/configured as `ldap://lldap-test`, the *container* name — so
  `docker-compose.ldap-test.yml` pins `lldap-test` as an explicit network **alias**. Never drop that
  alias, and grep before renaming anything else.
- The ldap/smb/keycloak overlays used to declare the default network `external: true` under the name
  `${COMPOSE_PROJECT_NAME:-opentranscribe}_default`. Compose defaults the project name to the
  **directory**, not `opentranscribe`, so that name never existed and `up` died with "network …
  declared as external, but could not be found". They now use the project's implicit default
  network, like the monitoring overlay always has. The cost: they can no longer be run standalone
  with a bare `docker compose -f docker-compose.ldap-test.yml up` — load them through `./opentr.sh`.
- Every aux overlay publishes on **`127.0.0.1` only**. They are throwaway services with published
  credentials (LLDAP admin_password + hard-coded JWT secret, Samba testuser/testpass, Grafana
  admin/admin, Keycloak admin/admin, unauthenticated Prometheus with `--web.enable-lifecycle`).
  Do not move them to `0.0.0.0`; front them with a proxy instead.
- Keycloak needs `KC_HEALTH_ENABLED: "true"` for its healthcheck. Since Keycloak 25 the health
  endpoints moved to the **management port 9000** and are only served when that flag is set — probe
  `/health/ready` on 8080 and you get a 404 forever, so `up --wait` fails on a Keycloak that works.
- `--with-watch` / `--with-backup` are the remaining un-isolatable overlays and the reason the
  combined-flag warning still exists: they bind **live host directories**, which a fresh stack then
  shares with the main one. Nothing in `opentr.sh` can namespace a path the operator asked for.
- `setup-watch-source-test-data.sh` resolves containers through `OT_CONTAINER_PREFIX`
  (default `opentranscribe`) — pass `OT_CONTAINER_PREFIX=otfresh-<name>` to seed a fresh deployment.

## docker-build-push.sh

- **ALWAYS `USE_REMOTE_BUILDER=true`.** It defaults to `false` and `PLATFORMS` defaults to
  `linux/amd64,linux/arm64`, so without it ARM64 builds under QEMU — 2–3 h instead of 15–30 min.
  It hard-exits if the `opentranscribe-multiarch` builder is missing (`setup-remote-builder.sh setup`).
- `SKIP_SECURITY_SCAN=true` for quick iteration; `PLATFORMS=linux/amd64` for single-arch (no remote
  builder needed); `$0 auto` builds only git-changed components.
- **Every path ends in `buildx --push` — there is no local-only mode.** `:latest` and `:vX.Y.Z` hit
  Docker Hub the instant the build finishes, and it then runs `push-security-reports.sh`, which
  **git-commits and pushes** `security-reports/` to whatever branch is checked out.

## DESTRUCTIVE — never run casually

- `reset-retries.sh` — bare `UPDATE media_file SET retry_count = 0` on the live DB: **no WHERE clause,
  no confirmation, no dry-run.**
- `fix-database-issues.sql` / `fix-false-error-files.sql` — mass status UPDATEs; both are stale one-offs
  superseded by `backend/app/tasks/recovery.py`. (`comprehensive-database-review.sql` is read-only.)
- `cleanup-test-users.py` — `DELETE FROM "user"`; dry-run unless `--execute`.
- `uninstall-offline-package.sh` — `docker compose down -v`, `docker rmi`, `rm -rf /opt/opentranscribe`.
- `release-tests/*` — `docker volume rm` on `opentranscribe_*` plus `rm -rf $TEST_ROOT`. They
  **require the live stack to be stopped**: they bind the standard 5173–5180 ports under the stock
  `opentranscribe-*` names and the one-liner's `opentranscribe` compose project, **by design**, so the
  run exercises exactly what a real user's install produces. `TEST_PROJECT_NAME` (`ot-reltest-*`) is a
  cleanup label namespace only, never `COMPOSE_PROJECT_NAME`. Isolation is from live *data*, not live
  *names*: `lib/guardrails.sh` refuses to start if any `opentranscribe-*` container exists (running or
  stopped) or any port is bound, rejects a `TEST_ROOT`/bind-mount under a protected path, never deletes
  the production volumes, and gates on an `I UNDERSTAND` prompt. For a stack that runs *beside* the
  live one, use `./opentr.sh start dev --fresh <name> --port-offset N` instead. (The README claimed
  hard isolation with 6173+/6273+ ports until #303 — it never worked that way.)
- `run_benchmark.py` — `docker compose down -v` on `-p otbench`, plus a legacy cleanup pass aimed at the
  **`transcribe-app` dev project** (suppress with `--no-cleanup-legacy`).
- `test-model-download.sh` — `rm -rf ./test-model-cache`, **relative to cwd**.
- `frontend-check.sh` on failure shells out to the `claude` CLI and then `git add`s every modified file
  under `frontend/` — pass `--no-claude` if you don't want your index touched.
- Everything else writes to the **live dev stack**: benchmarks upload files and never delete them;
  `test-all-auth.sh` / `test-fedramp.sh` register real users and never clean up (that is why
  `cleanup-test-users.py` exists); `run-auth-e2e.sh` mutates admin auth config (restored on an exit
  trap) and stops the frontend container.

## Gotchas

- Stack must be up (`./opentr.sh start dev`) for `run-integration-tests.sh` (probes Postgres on 5176),
  `e2e/run-e2e.sh` (5173+5174), `test-watch-e2e.sh`, `speaker-profiles-backup.sh`, and the recovery
  `test-*.sh`. Others — `reset-retries.sh`, the benchmarks, the `.sql` files — have **no precondition
  check** and just fail with cryptic docker errors.
- `run-integration-tests.sh --cleanup` runs `cleanup-test-users.py` in **dry-run only**; it reports,
  it does not delete.
- `__pycache__/`, `pki/test-certs/` (contains private keys), and `release-tests/.env.test-secrets` are
  gitignored but present on disk.
- Most `benchmark_*` / `vram-probe-*` / `spike-*` files are accreted one-off experiments;
  `run_benchmark.py` (via `./opentr.sh bench`) is the maintained orchestrator.
