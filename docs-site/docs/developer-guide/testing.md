---
sidebar_position: 3
---

# Testing

OpenTranscribe has comprehensive testing at multiple levels: unit tests, integration tests, and end-to-end browser tests.

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
python3 scripts/audit-tests.py backend/tests   # 7 AST detectors, exits 1 on new offenders
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

Allowlists live beside each auditor, keyed `file::test::category` with a **mandatory written
reason**. The category is part of the key on purpose: keyed by test alone, one entry
exempted a test from all six detectors at once.

**Run the self-test after touching any detector.** It caught two detectors in *each*
auditor that matched nothing at all — reporting zero findings, which is indistinguishable
from a clean suite. A new detector needs a must-fire case and a must-stay-clean case.

### Two calibration traps

Both cost a debugging cycle, and both make an auditor over-report by a third:

- Playwright's `expect()` and Testing Library's `getBy*`/`findBy*` **throw** — they *are*
  assertions. Miss that and a third of the E2E/component suite reads as assertion-free.
- `expect.arrayContaining(...)` is a matcher **argument**, not an assertion head.

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
