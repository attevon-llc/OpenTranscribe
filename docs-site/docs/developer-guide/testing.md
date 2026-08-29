---
sidebar_position: 3
---

# Testing

OpenTranscribe has comprehensive testing at multiple levels: unit tests, integration tests, and end-to-end browser tests.

## The bar, and where it does not apply

**Production code ships with tests that would actually fail if the code were wrong.** Not tests
that execute the code — tests that *notice*. The difference is the subject of
[Test quality](#test-quality-a-test-that-cannot-fail-is-worse-than-no-test) below, and it is not
theoretical here: this repo has shipped an assertion that passed against an empty index, a marker
that selected nothing, 240 security tests gated off behind stale environment variables, and a
parser reporting `14/14 ok` while extracting two characters per document.

Three rules follow from that:

1. **Watch the test fail before the fix.** A test you have never seen red is not evidence that it
   is checking anything. When a fix is already in place, break it deliberately, confirm the test
   goes red, and restore.
2. **Assert the outcome, not the absence of an exception.** `n/N succeeded` is not a measurement
   if a success can be empty. Assert *characters extracted*, *rows written*, *the exact status
   code* — something a broken implementation could not produce.
3. **Never loosen a test to make it pass, and never allowlist a finding you could fix.** If the
   test is wrong, fix it for the stated reason. If the product is wrong, fix the product. See
   [Fix the finding, never silence it](https://github.com/attevon-llc/OpenTranscribe/blob/master/CLAUDE.md)
   in the repository guide.

:::tip Quick prototypes and spikes are explicitly exempt
Throwaway code exploring whether an approach works does **not** need a test harness, and imposing
one is a good way to make exploration expensive. The bar scales with the project: a spike answers
a question and is deleted or rewritten; production code is depended upon. If it is genuinely
ambiguous which mode a change is in, ask — do not guess in either direction.
:::

## Test Structure

```
backend/tests/
├── conftest.py              # Shared fixtures for unit/integration tests
├── api/endpoints/           # API endpoint tests
├── e2e/                     # End-to-end browser tests
│   ├── conftest.py          # E2E fixtures
│   ├── test_login.py        # Login tests (~50 tests)
│   ├── test_registration.py # Registration tests (~35 tests)
│   └── test_auth_flow.py    # Combined auth flow tests
└── test_*.py                # Other unit tests
```

## Running Tests

### The one-command dev-cycle check

`scripts/run-dev-tests.sh` chains the backend gate, the full E2E suite, and the frontend check
into one command with a single consolidated pass/fail report — the fast "does my current branch
work" loop for ordinary development. It is **not** the same tool as
[`scripts/test-matrix.sh`](full-test-matrix.md), which is the exhaustive deployment-mode
rehearsal (dev/prod/lite/PKI/GPU-scale/fresh-install/upgrade) run before cutting a release, not
during ordinary development.

```bash
./opentr.sh start dev                     # live stack must be up first

./scripts/run-dev-tests.sh --full         # backend gate + full e2e + frontend check
./scripts/run-dev-tests.sh --fast         # backend gate (e2e smoke subset) + frontend check
./scripts/run-dev-tests.sh --backend-only # just scripts/run-integration-tests.sh
./scripts/run-dev-tests.sh --e2e-only     # just the full e2e suite
./scripts/run-dev-tests.sh --frontend-only # just the frontend check, no live stack needed
```

Mode flags are composable — pass more than one to union their phases (e.g.
`--backend-only --frontend-only`).

Measured on this host: `--fast` ≈ 22 minutes end to end (integration-marked tests dominate at
~9 min; e2e-smoke, GPU-marked tests, and the unit/API suite are each 2–3 min; everything else
combined is under a minute). `--full` runs the entire e2e suite instead of the smoke subset and
takes correspondingly longer. These numbers rot — re-run and read the per-phase report rather
than trusting them.

#### Overlay auto-orchestration

The backend/e2e phases need certain auth/LLM test containers up — `run-dev-tests.sh` starts and
stops them for you, the same way it already did for the mock-LLM overlay alone before this was
generalized:

```bash
./scripts/run-dev-tests.sh --all-overlays    # also bring up --with-watch / --with-mock-asr
./scripts/run-dev-tests.sh --with-gpu-scale  # exercise the multi-GPU worker topology;
                                              # auto-skips with a clear message on a
                                              # single-GPU deployment — never auto-started
                                              # under any other flag
./scripts/run-dev-tests.sh --no-overlays     # escape hatch: stack is already configured
                                              # as desired, skip all overlay auto-detection
./scripts/run-dev-tests.sh --list-overlays   # print the resolved overlay set, start nothing
./scripts/run-dev-tests.sh --dry-run         # + the exact opentr.sh command, start nothing
```

Which overlays get resolved depends on the requested phase — `--mock-llm`, `--with-keycloak-test`,
and `--with-ldap-test` come up automatically when the tests that need them are in scope. Each
overlay this run started is torn back down on exit, and any `auth_config` DB setting it flipped
(`oidc_enabled`/`ldap_enabled`) is restored to its prior value — an overlay already running before
this script started is left alone (not this run's to stop). `scripts/lib/dev-test-overlays.sh`'s
overlay table is the source of truth for exactly what's managed; every other `opentr.sh --with-*`
flag not in that table is either intentionally out of scope (documented inline with a reason —
e.g. `--with-pki` needs the prod/nginx overlay, not the dev stack this script targets) or a gap a
unit test (`test_run_dev_tests_overlay_coverage.py`) will fail the build over.

Per-phase logs are written to a fresh `/tmp/ot-run-dev-tests.*` directory and the path is
printed in the final report. Exit codes match `scripts/release.sh`'s convention: `0` pass, `1`
gate failed, `2` misuse, `3` precondition unmet (e.g. the dev stack isn't up).

### Prerequisites

```bash
# Activate virtual environment
source backend/venv/bin/activate

# Install test dependencies (if needed)
pip install pytest pytest-asyncio pytest-cov pytest-playwright
playwright install chromium
```

### Unit Tests

Unit tests run against the backend without requiring the full dev environment:

```bash
# Run all unit tests
pytest backend/tests/ --ignore=backend/tests/e2e/ -v

# Run specific test file
pytest backend/tests/test_auth_config_service.py -v

# Run with coverage
pytest backend/tests/ --ignore=backend/tests/e2e/ --cov=app -v
```

### E2E Tests

End-to-end tests require the dev environment running:

```bash
# Start dev environment
./opentr.sh start dev

# Run all E2E tests (headless)
pytest backend/tests/e2e/ -v

# Run with visible browser (for debugging)
DISPLAY=:13 pytest backend/tests/e2e/ -v --headed

# Run specific test
pytest backend/tests/e2e/test_login.py::TestLoginSuccess -v
```

## E2E Test Categories

### Login Tests

| Test Class | Description |
|------------|-------------|
| `TestLoginFormValidation` | Required field validation |
| `TestLoginSuccess` | Successful login scenarios |
| `TestLoginFailure` | Failed login scenarios |
| `TestLoginSecurity` | Password hiding, rate limiting |
| `TestLoginSession` | Session persistence |
| `TestLoginUI` | UI elements verification |
| `TestLoginAccessibility` | Keyboard navigation, labels |

### Registration Tests

| Test Class | Description |
|------------|-------------|
| `TestRegistrationFormValidation` | All fields required |
| `TestUsernameValidation` | Username constraints |
| `TestEmailValidation` | Email format validation |
| `TestPasswordValidation` | Password complexity rules |
| `TestDuplicatePrevention` | Duplicate email/username |
| `TestRegistrationSuccess` | Success flow |
| `TestRegistrationUI` | UI elements |

## Test quality: a test that cannot fail is worse than no test

It buys false confidence and hides the defect it was written to catch. This repo has
shipped every variant of that, so four tools now check for it — and each was calibrated
against a real instance found here (issue #431).

```bash
python3 scripts/audit-tests.py backend/tests   # 16 AST detectors, exits 1 on new offenders
cd frontend && npm run test:audit              # the vitest sibling, 10 detectors
npm run test:audit:selftest                    #   ...and ITS self-test
python3 scripts/analyze-test-timing.py <junit.xml> [--baseline baseline.xml]
./scripts/run-mutation-tests.sh --module spans # opt-in, never in the gate or CI
```

### The shapes the auditors reject

| detector | why it matters |
|---|---|
| `permissive-status` | `assert code in (200, 403)` accepts success *and* authorization failure. One real case accepted `400` — literally "could not export" — in a super-admin export test. |
| `conditional-only` | every assertion inside an `if` with no `else` is vacuous whenever the condition is false, and nothing reports it. |
| `conditional-skip` | `if cond: <asserts> else: pytest.skip()` *looks* honest, but a guard that can never be true is a permanent skip. The only test of `GET /tasks/{task_id}` skipped on every run for 11 months this way, while the endpoint returned a hardcoded progress value. |
| `no-assertion` | "it did not raise" is an assertion only when written as one — use `tests/helpers.does_not_raise`, whose reason string is mandatory. |
| `failure-masking` | `except ...: pytest.skip()` turns a *failure* into a *skip*, so a genuine regression reads as "skipped" forever. |
| `mock-heavy` / `mock-only` | asserting mock wiring instead of behaviour. A client that built a perfect request and dropped the response passed nine such tests. |
| `fixture-named-test` | a `@pytest.fixture` named `test_*` never runs as a test but corrupts every count of tests-without-assertions. |
| `external-service-mock` | a test whose **id claims integration with a service that is actually substituted**. See the convention below. |
| `readiness-probe-target` | a health/readiness probe aimed at a **hardcoded** host/port instead of the stack under test. `wait_for_bench_backend_health` polled `opentranscribe-backend` — the *dev* stack — so the bench stack's readiness wait reported "healthy" whenever dev was up, green-lighting a benchmark against a stack that might not exist. |

Allowlists live beside each auditor, keyed `file::test::category` with a **mandatory written
reason**. The category is part of the key on purpose: keyed by test alone, one entry
exempted a test from all six detectors at once.

**Run the self-test after touching any detector.** It caught two detectors in *each*
auditor that matched nothing at all — reporting zero findings, which is indistinguishable
from a clean suite. A new detector needs a must-fire case and a must-stay-clean case.

### The real-vs-stand-in convention

**"Ran against the real engine" and "ran against a stand-in" must be different test ids.** A
stand-in is never quietly substituted under the same id.

Twelve tests for an OpenSearch `delete_by_query` body once passed without ever reaching
OpenSearch: no stack was up, so they ran against a well-built in-memory stand-in, under ids
that read as real-engine coverage. Nothing was *wrong* with the stand-in — it cannot simply
prove that OpenSearch 3.4 executes that `bool`/`filter`/`range` body, and the suite was green
on an assumption about the engine. Under one id, that is invisible to JUnit history and to
`analyze-test-timing.py`.

So:

- **using a stand-in is fine** — name or locate the test honestly (`tests/unit/`, a `fake_*`
  fixture) and `external-service-mock` stays silent;
- **claiming the real service is a declaration**, and the auditor holds you to it. A claim
  counts when it comes from a marker or env gate (`@pytest.mark.integration`,
  `@pytest.mark.needs_opensearch`, `skipif(SKIP_OPENSEARCH, …)`, a module `pytestmark`), from a
  realness word in the test's own name (`real`, `live`, `actual`, `end_to_end`), or from the
  module path (`tests/integration/`, `tests/e2e/`);
- **on CI, skip explicitly with a reason.** Never silently pass, and never swap the stand-in in
  under the same id.

The service's own name in a test id is deliberately **not** a claim — it names the subject, not
the engine. `test_blacklist_token_redis_unavailable_fails_secure` is a correct unit-test name,
and counting that tier fired on 20 honestly-named tests.

Substitution is detected through the fixture graph, not just the test body: the suite above
installed its stand-in in a fixture called `fake_index`, which names no service, so reading
only the test body (no `patch` call at all) or only the parameter name saw nothing. The auditor
resolves a fixture's own `patch`/`monkeypatch` targets and inherits them transitively — 388
tests in this tree substitute a service and **264 are visible only that way**.

### Two calibration traps

Both cost a debugging cycle, and both make an auditor over-report by a third:

- Playwright's `expect()` and Testing Library's `getBy*`/`findBy*` **throw** — they *are*
  assertions. Miss that and a third of the E2E/component suite reads as assertion-free.
- `expect.arrayContaining(...)` is a matcher **argument**, not an assertion head.

### The shell scripts are tested too

`backend/tests/unit/test_shell_expansion_guards.py` asserts that every `$VAR` expansion in
`opentr.sh` and `scripts/common.sh` is **defaulted** — `${VAR:-x}`, or `: "${VAR:=}"` in the
`opentr.sh` prologue. Both scripts run under `set -uo pipefail`, so an unguarded optional
`.env` variable is not a style nit: it is a hard abort. `common.sh` read a bare
`[ -n "$GPU_DEVICE_ID" ]` while `opentr.sh` defaulted five *other* optional variables and not
that one, so `./opentr.sh` died with `GPU_DEVICE_ID: unbound variable` in **any checkout
without a `.env`** — every git worktree, since `.env` is gitignored and never comes along. The
crash therefore blocked precisely the isolated-worktree workflow it was needed for.

It is static (a parse, not an execution), so it costs milliseconds and runs in the fast unit
suite before anything tries to start a stack. Exemptions live in a `_ALLOWLIST` dict keyed
`<script>::<VAR>` with a mandatory reason, and a **stale entry fails** — default the variable,
delete the line.

Precision matters more than reach here, and three shapes each produced a false positive in a
draft: an escaped `\$USER` in help text (printed as instructions, not expanded), a `$VAR` inside
single quotes, and a `$VAR` named in a comment. `${#VAR}` and `${VAR%…}` are **not** guards —
both still abort under `set -u`. Positional parameters are out of scope by design: a missing
argument wants a usage message, not a default.

### Timing: look for barriers, not slow tests

`analyze-test-timing.py` reports wall clock vs Σ durations, effective parallelism,
per-`xdist_group` totals, and duration **clusters**. Unrelated tests from several files
sharing a sub-second band is a released lock queue, not a coincidence — that is how a
single worker was found owning 81% of the wall clock. A test named
`test_timing_unauthorized`, one GET asserting 401, took 40.56 s; no such test contains 40
seconds of work.

Profile before theorising. `python -m cProfile -o out.prof -m pytest <test>` found a
71-second `time.sleep` inside a Redis retry policy in one pass, after two plausible
hypotheses had each cost a full measurement cycle.

### Mutation testing

Coverage says a line **ran**. Mutation testing says the suite would **notice** if the line
were wrong: it edits the source (flips `<` to `<=`, drops an `and`, returns `None`) and
re-runs the tests. A surviving mutant is a line the suite executes and asserts nothing
about — for a security predicate, a control that can be deleted with the suite still green.

Scoped to six security-critical modules. The first run on the PII-masking module found
that flipping `char_start < last.char_end` to `<=` survived, meaning nothing decided
whether two *exactly adjacent* redaction spans merge into one placeholder or stay two.

A survivor is a finding: add the missing assertion, or conclude the line is dead and
delete it. **Never loosen a test to kill one.** Survivors inside log lines and error
strings are noise — judge by whether a real caller could observe the difference.

## Writing Tests

### Unit Test Example

```python
import pytest
from fastapi.testclient import TestClient

def test_login_success(client, normal_user):
    """Test successful login returns tokens."""
    response = client.post(
        "/api/auth/token",
        data={"username": normal_user.email, "password": "password123"},
    )
    assert response.status_code == 200
    assert "access_token" in response.json()
```

### E2E Test Example

```python
import pytest
from playwright.sync_api import Page, expect

class TestMyFeature:
    def test_feature_works(self, authenticated_page: Page):
        """Test feature with logged in user."""
        authenticated_page.click("#feature-button")
        authenticated_page.wait_for_selector("#result")
        expect(authenticated_page.locator("#result")).to_be_visible()
```

## Available Fixtures

### Unit Test Fixtures

| Fixture | Description |
|---------|-------------|
| `db_session` | Database session with transaction rollback |
| `client` | FastAPI TestClient |
| `normal_user` | Created normal user |
| `admin_user` | Created admin user |
| `user_token_headers` | Auth headers for normal user |
| `admin_token_headers` | Auth headers for admin user |

### E2E Test Fixtures

| Fixture | Description |
|---------|-------------|
| `page` | Fresh Playwright page |
| `login_page` | Page navigated to login |
| `authenticated_page` | Already logged in as admin |
| `auth_helper` | Login/logout/register helper |
| `api_helper` | Backend API call helper |
| `console_errors` | Captured browser console errors |
| `base_url` | Frontend URL (localhost:5173) |
| `backend_url` | Backend URL (localhost:5174) |

## Browser Automation (Claude Code)

For ad-hoc browser testing and debugging, Claude Code can use:

```bash
# Open browser and take screenshot
node ~/bin/browser-tools/browse.js http://localhost:5173

# With visible browser on XRDP
node ~/bin/browser-tools/browse.js http://localhost:5173 --display=:13

# Perform actions
node ~/bin/browser-tools/browse.js http://localhost:5173 \
  'fill:#email:admin@example.com' \
  'fill:#password:password' \
  'click:button[type=submit]' \
  'screenshot:result'
```

## Test Credentials

- **Admin user:** `admin@example.com` / `password`
- **Test users:** Created with unique UUIDs to avoid conflicts

## CI/CD Integration

```yaml
# Example GitHub Actions workflow
test:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v3
    - name: Run unit tests
      run: |
        pip install -r requirements.txt
        pytest backend/tests/ --ignore=backend/tests/e2e/ -v
```

E2E tests are typically run separately as they require the full environment.

## Debugging Tips

1. **Use `--headed` flag** to watch browser tests
2. **Add `page.wait_for_timeout(5000)`** to pause and inspect
3. **Check `~/bin/browser-tools/screenshots/`** for screenshots
4. **Use `--screenshot only-on-failure`** for failed test screenshots
5. **Check browser console** with `console_errors` fixture
