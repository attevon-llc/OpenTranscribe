# scripts/audit_tests — test-suite quality tooling (issue #431)

Moved intact from `scripts/CLAUDE.md`. Entry point: `scripts/audit-tests.py` (the detectors, allowlist, selftest cases and CLI live in this package). Siblings covered here: `analyze-test-timing.py`, `run-mutation-tests.sh`, the session-lifetime and error-disclosure auditors. The gate's own contract is summarised in `backend/tests/CLAUDE.md`.

- **Test-suite quality tooling** (issue #431) — four scripts whose job is to stop a test that
  cannot fail from looking like a passing one:
  - `audit-tests.py <dir>` (thin entry point; the detectors, allowlist, selftest and CLI live in the `scripts/audit_tests/` package) — **21** AST detectors. The original seven (permissive-status,
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
      service; **264 are visible only that way**. Currently **0 open findings** (11 allowlisted, each with a written reason) — and that is a
      measured claim, not an absent one: the real suite fires under all three claim tiers when
      mislabelled, and stays clean as written.
    - `readiness-probe-target` — **a health/readiness probe whose target is hardcoded rather
      than derived from the stack under test.** `wait_for_bench_backend_health` polled
      `opentranscribe-backend` (the *dev* stack), so the bench stack's readiness wait reported
      healthy whenever dev was up — green-lighting a benchmark against a stack that may not
      exist. Fires only when **every** argument of the probe call is fixed at parse time (single
      assignments resolved, so `f"{BASE_URL}/health"` counts); one derived argument means the
      target follows the stack under test, which is why `_reachable("127.0.0.1", port)` against
      an allocated port stays clean. Currently 0 findings: the three it first found were fixed.
    **Five more landed since**, past the seven-then-two count above (16): `broad-raises`,
    `bare-return`, `self-comparison`, `sleep-sync`, `skipped-test` — bringing the total to the
    21 named at the top of this bullet. Per the repo's "ask the tool, don't transcribe counts"
    convention, don't trust that number either as it ages: `python3 scripts/audit-tests.py
    --help` shows the live set via its `--category` flag's `choices=CATEGORIES`
    (`--list` prints every finding, not the detectors). There is no `--list-detectors` flag.
    **`--selftest` is not optional.** 82 cases (must-fire, must-stay-clean, path-tier,
    fires-exactly-once and allowlist; the run prints the live count), run in-memory; `backend/tests/unit/test_audit_tests_selftest.py` runs the same cases under
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
    run (`N finding(s) are DEFERRED WORK, not accepted patterns`) so a green gate is never
    mistaken for a clean tree; and a **stale** entry — one whose finding no longer exists —
    **fails the run**, so the file can only shrink and an exemption cannot outlive its subject.
    The three `readiness-probe-target` entries were live bugs, first deferred because they sat in
    files outside the change that added the detector, and have since been FIXED (no entries remain):
    `fixtures/mock_llm.py` hardcoded `CONTAINER_PORT = 5199` (twice) and `test_selective_reprocess.py`
    hardcoded `BASE_URL = "http://localhost:5174/api"`. **Both ports are ones `--fresh --port-offset N`
    MOVES** (`BACKEND_PORT` in `FRESH_PORT_VARS`, `MOCK_LLM_PORT` in
    `FRESH_MOCK_LLM_PORT_VARS`), so against an isolated stack these probe whichever stack owns
    the base port — and because each failure path is a `skip`, the tests disappear rather than
    fail. The fix, now applied, is the shape the root conftest already uses for Postgres/MinIO/OpenSearch:
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
    naming `/api/tasks/system/fix-file/x`. Both regressions are `--selftest` cases (29 total).
    It measures REFERENCE, not execution, and says so in every run: an upper bound on
    "untested". Measured 2026-10-27: **17 of 570** routes unreferenced
    (`audit-route-coverage.py` prints the live figure), plus 1 WebSocket route (`/api/ws`) reported
    separately and now referenced.

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
    never in the gate or CI (except `--check-baseline`, which reads results rather than
    producing them). ⚠️ **`--check-baseline` now exits 4 on a PARTIAL measurement, not only on
    zero.** "0 of 6 is not a pass" was half the rule: on 2026-09-06 it measured **1 of 6**,
    printed `⊘ NOT MEASURED (5/6)` naming the other five, and exited **0**, so the gate rendered
    the phase as `✓ Mutation ratchet passed` with five sixths of a security-critical ratchet
    unmeasured. Since the other five are unmeasured *because their source or test list changed
    since the last run*, expect the gate to report NOT MEASURED here until someone measures
    them (30-90 min each) — or sets `MUTATION_RATCHET_PARTIAL_OK=1` **deliberately**, which is
    recorded in the output rather than assumed in the exit code.
    **`--clean` when you are done**: it leaves ~330k lines of
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

    **Parallel mutation testing, across modules only (2026-09-12) — the serial-only framing above
    is about ONE database, not about modules.** `backend/tests/CLAUDE.md`'s objection to
    cosmic-ray-style distributed execution is specifically that "every mutant's tests share the
    one live Postgres, and cross-worker concurrency on it is exactly what must not happen here"
    (the #389/#431 deadlock shapes) — that is a statement about many workers contending on one
    shared DB, and it does not forbid running the six `MODULE_TESTS` modules in parallel *lanes*
    each pointed at its **own** throwaway stack. Serial `--all` is hours (`lockout` ~30-90 min,
    `dependencies` ~20-60 min, `session` ~15-45 min); this host is 48 cores and mostly idle, so
    running the slow modules concurrently — each isolated, never sharing a DB — collapses the
    wall clock without reintroducing the #389/#431 hazard:

    ```bash
    # one worktree + one --fresh stack per module, unique ports, run --module inside each
    for m in lockout dependencies session security password_policy spans; do
      git worktree add ".claude/worktrees/mutate-$m" -b "mutate/$m" HEAD
    done
    # in each worktree: its OWN --fresh deployment (own Postgres/Redis, own ports) —
    ./opentr.sh start dev --fresh "mutate-$m" --port-offset <unique-per-lane>
    # then, inside that worktree, against ITS OWN stack:
    POSTGRES_PORT=<lane-port> REDIS_PORT=<lane-port> ./scripts/run-mutation-tests.sh --module "$m"
    ```

    ⚠️ **`-n0` still applies WITHIN each lane — the parallelism is ACROSS lanes, never inside
    one.** `run-mutation-tests.sh` already forces `-n0` via `PYTEST_ADDOPTS` (the trap two
    paragraphs up: without it, every mutant forks one xdist worker per core against whatever DB
    that lane owns). Running six lanes of `-n0` on a 48-core host is the parallel design; running
    one lane at `-n auto` against a shared DB is the exact deadlock this was built to avoid.

    🔴 **Copy the evidence back into the MAIN checkout, or the whole exercise measures nothing
    for the gate.** `OUT_DIR` defaults to `$REPO_ROOT/.mutation` — and in a worktree,
    `REPO_ROOT` is *the worktree*. So each lane's `<module>.{log,meta,testhash}` lands there and
    the main checkout's `--check-baseline` still reports NOT MEASURED. **Merging the lane's
    `mutation-baselines.tsv` row does not fix this**: the `.meta` sidecar is what makes a `.log`
    admissible (see this script's own EVIDENCE header), and the TSV is only the ratchet's
    threshold. After each lane finishes:

    ```bash
    cp .claude/worktrees/mutate-$m/.mutation/$m.{log,meta,testhash} .mutation/
    ./scripts/run-mutation-tests.sh --check-baseline      # must print "✓ holding at N"
    ```

    Measured 2026-09-12: with the rows merged but the sidecars left behind, the gate still said
    `⊘ NOT MEASURED (3/6)`; copying them flipped `lockout` and `session` to `✓ holding at 72` /
    `✓ holding at 46`. `.mutation/` is gitignored (`.mutation/` in `.gitignore`), which is correct — this is
    per-machine measurement evidence, not something to commit.

    Two further operational traps found running this recipe, both real and both bit a background run:
    - **Give the throwaway Redis a password (`--requirepass`), and leave the test env
      unauthenticated.** A *passwordless* throwaway Redis is EASIER to reach than the real dev
      stack, and that silently changes test semantics: `lockout`'s `_get_store` re-probe branch
      (`elif _redis_client is None`) genuinely reconnects, so the memory-fallback tests that force
      `_redis_client = None` stop exercising "Redis is down" and FAIL. Six phantom failures before
      it was root-caused. The live stack's Redis is password-protected — match its *failure mode*,
      not merely its port number.
    - **Each worktree needs its own venv**, so a first parallel run pays a full
      `pip install -r requirements.txt` (CUDA wheels, multi-GB) per lane before a single mutant
      runs. Budget for it; subsequent runs in the same worktree are much faster. Two specifics
      that cost a lane a wasted cycle: build it with **Python 3.12**, not 3.11 (`scipy==1.18.1`
      in `requirements.txt` requires >=3.12 and will not resolve), and install
      **`requirements-dev.txt` as well** — `pytest-cov` lives only there, and without it the
      coverage pre-flight fails as `could not measure coverage ... NOT a measurement` (exit 4)
      rather than saying the plugin is missing. That message reads like a finding about the
      module; it is a finding about the venv.
    - **`./opentr.sh fresh-destroy <name>` prompts `Proceed? (y/N)`** — a backgrounded/non-interactive
      call without an answer cannot confirm (EOF now fails with nothing destroyed). Pass `--yes`:
      `./opentr.sh fresh-destroy mutate-lockout --yes`.
    - **The ratchet fingerprints each module's *test file* content, not just the source.**
      `run-mutation-tests.sh`'s cache-staleness guard (item 3, above) fires on a changed
      `MODULE_TESTS` entry — so editing any test file belonging to a module mid-wave sends that
      module's next run to NOT MEASURED (stale fingerprint) rather than silently reusing old
      verdicts; it needs a fresh `--module` run (and often `--clean`) before its numbers count
      again, exactly like any other lane's baseline after a test edit.
  - `frontend/scripts/audit-frontend-tests.mjs` (`npm run test:audit`) — the vitest sibling,
    10 detectors, TypeScript compiler API. Run `test:audit:selftest` after ANY detector change:
    its 32 cases caught two detectors matching **nothing**, which reports 0 findings and reads
    exactly like a clean suite. Two of its detectors do NOT port to Python as-is:
    `toBeFalsy`-style weakness does not, because `assert not offenders, "<list>"` is this
    repo's standard AST-guard shape and fails on any violation — counting the negated form as
    weak reported 40 of those as findings; and a bare `assert predicate(x)` is real evidence
    where `expect(x).toBeTruthy()` is not. `loop-only` also needs single-assignment resolution
    (`endpoints = [...]` then `for e in endpoints:` is as static as the literal) or 22
    table-driven tests are false positives.
