# E2E Tests for OpenTranscribe

End-to-end tests using pytest-playwright to verify frontend and backend work together through real browser automation.

## Prerequisites

1. **Dev environment running:**
   ```bash
   ./opentr.sh start dev
   ```

2. **Python venv with dependencies:**
   ```bash
   source backend/venv/bin/activate
   pip install pytest-playwright
   playwright install chromium
   ```

3. **Services accessible:**
   - Frontend: http://localhost:5173
   - Backend: http://localhost:5174

## Running Tests

### Basic Usage

```bash
# Activate venv
source backend/venv/bin/activate

# Run all E2E tests (headless)
pytest backend/tests/e2e/ -v

# Run specific test file
pytest backend/tests/e2e/test_login.py -v

# Run specific test class
pytest backend/tests/e2e/test_login.py::TestLoginSuccess -v

# Run specific test
pytest backend/tests/e2e/test_login.py::TestLoginSuccess::test_login_with_email -v
```

### With Visible Browser (XRDP)

Use display `:11` (the XRDP session on this machine):

```bash
# Run with visible browser
DISPLAY=:11 pytest backend/tests/e2e/ -v --headed
```

### Runner Scripts

```bash
./scripts/e2e/run-e2e.sh                # full suite (checks the stack first)
./scripts/e2e/run-e2e-smoke.sh          # quick read-mostly subset (~3 min)
./scripts/e2e/run-e2e.sh -m upload      # one marker
./scripts/e2e/run-e2e.sh --headed       # visible browser (DISPLAY=:11)
```

### Markers

```bash
pytest backend/tests/e2e/ -m upload          # upload stepper flows
pytest backend/tests/e2e/ -m search          # search page
pytest backend/tests/e2e/ -m settings        # settings modal
pytest backend/tests/e2e/ -m transcription   # transcript view/editing
pytest backend/tests/e2e/ -m "not slow"      # skip slow tests
# also: gallery, auth, smoke, pki, responsive, visual, api_e2e
```

### Additional Options

```bash
# Screenshot on failure
pytest backend/tests/e2e/ -v --screenshot only-on-failure

# Slow down for debugging (100ms between actions)
pytest backend/tests/e2e/ -v --headed --slowmo 100
```

### Media Fixtures

`sample_audio` / `sample_video` fixtures generate tiny clips with **ffmpeg**
into `backend/tests/e2e/fixtures/` (gitignored, cached) — no external
downloads. Upload tests delete everything they create through the API, so
the dev environment stays clean.

## Test Structure

```
backend/tests/e2e/
├── conftest.py                       # Shared fixtures
├── pytest.ini                        # E2E-specific pytest config
├── README.md                         # This file
├── fixtures/                         # ffmpeg-generated media (gitignored)
├── test_auth_flow.py                 # Combined auth flow tests
├── test_auth_buttons.py              # Auth method button visibility tests
├── test_login.py                     # Comprehensive login tests (~50 tests)
├── test_registration.py              # Comprehensive registration tests (~35 tests)
├── test_mfa.py                       # MFA TOTP tests
├── test_pki.py                       # PKI certificate auth E2E (requires TLS overlay)
├── test_ldap_oidc.py             # LDAP + Keycloak config + login tests
├── test_gallery_actions.py           # File gallery UI action tests
├── test_upload.py                    # Upload stepper flows (@upload)
├── test_search.py                    # Search page (@search)
├── test_settings_modal.py            # Settings modal incl. profile/transcription (@settings)
├── test_file_detail_transcript.py    # Transcript view + segment editing (@transcription)
├── test_speaker_gender_clusters.py   # Speaker gender/cluster UI tests
└── test_speaker_sharing.py           # Speaker sharing UI tests
```

## Available Fixtures

### `login_page`
A page navigated to the login URL, ready for input.

```python
def test_example(self, login_page: Page):
    login_page.fill("#email", "user@example.com")
    login_page.fill("#password", "password")
    login_page.click("button[type=submit]")
```

### `authenticated_page`
A page already logged in as admin user.

```python
def test_example(self, authenticated_page: Page):
    # Already logged in, can interact with protected pages
    authenticated_page.click(".user-button")
```

### `auth_helper`
Helper class for authentication operations.

```python
def test_example(self, auth_helper):
    success = auth_helper.login("user@example.com", "password")
    auth_helper.logout()
    auth_helper.register("username", "email@example.com", "password")
```

### `api_helper`
Helper for making backend API calls alongside browser tests.

```python
def test_example(self, api_helper):
    api_helper.login("admin@example.com", "password")
    user_data = api_helper.get("/api/users/me")
```

### `console_errors`
Captures browser console errors during test.

```python
def test_no_errors(self, page: Page, console_errors: list):
    page.goto("http://localhost:5173")
    page.wait_for_timeout(2000)
    assert len(console_errors) == 0
```

### `base_url` / `backend_url`
URLs for frontend and backend.

```python
def test_example(self, page: Page, base_url: str, backend_url: str):
    page.goto(f"{base_url}/login")
    # base_url = http://localhost:5173
    # backend_url = http://localhost:5174
```

## Test Categories

### Login Tests (`test_login.py`)

| Class | Tests | Description |
|-------|-------|-------------|
| `TestLoginFormValidation` | 3 | Required field validation |
| `TestLoginSuccess` | 4 | Successful login scenarios |
| `TestLoginFailure` | 5 | Failed login scenarios |
| `TestLoginSecurity` | 3 | Password hiding, rate limiting |
| `TestLoginSession` | 2 | Session persistence |
| `TestLoginUI` | 5 | UI elements verification |
| `TestAlternativeAuth` | 2 | Keycloak, PKI options |
| `TestLoginAccessibility` | 3 | Labels, keyboard navigation |
| `TestLoginConsoleErrors` | 2 | No JS errors |

### Registration Tests (`test_registration.py`)

| Class | Tests | Description |
|-------|-------|-------------|
| `TestRegistrationFormValidation` | 5 | All fields required |
| `TestUsernameValidation` | 3 | Username constraints |
| `TestEmailValidation` | 3 | Email format validation |
| `TestPasswordValidation` | 7 | Password complexity, mismatch |
| `TestDuplicatePrevention` | 2 | Duplicate email/username |
| `TestRegistrationSuccess` | 2 | Success flow, can login after |
| `TestRegistrationUI` | 4 | UI elements verification |

### Auth Method Tests (`test_auth_buttons.py`)
Verifies that auth method buttons (LDAP, Keycloak, PKI) appear or are hidden based on server configuration. Always runs (no extra flags needed).

### MFA Tests (`test_mfa.py`)
TOTP setup, QR code scanning, verification, and recovery code flow. Requires dev environment with MFA enabled.

### PKI Tests (`test_pki.py`)
Client certificate authentication E2E. Requires PKI overlay:
```bash
RUN_PKI_E2E=true pytest backend/tests/e2e/test_pki.py -v --headed
```
PKI URL: `https://localhost:5182`

### LDAP/Keycloak Tests (`test_ldap_oidc.py`)
Config UI and login flow for LDAP and Keycloak. Requires running containers:
```bash
RUN_AUTH_E2E=true pytest backend/tests/e2e/test_ldap_oidc.py -v
```
See `tests/AUTH_TEST_SETUP.md` for container setup instructions.

## Writing New Tests

### Basic Test Structure

```python
import pytest
from playwright.sync_api import Page, expect

class TestMyFeature:
    """Test description."""

    def test_something(self, page: Page, base_url: str):
        """Test specific behavior."""
        page.goto(f"{base_url}/some-page")
        page.wait_for_selector("#element", timeout=10000)

        page.fill("#input", "value")
        page.click("button")

        page.wait_for_timeout(2000)

        expect(page.locator("#result")).to_be_visible()
```

### Using Fixtures

```python
class TestWithAuth:
    def test_protected_feature(self, authenticated_page: Page):
        """Test that requires logged in user."""
        # Already logged in
        authenticated_page.click("#some-protected-button")
```

### Handling Async Operations

```python
def test_async_operation(self, page: Page, base_url: str):
    page.goto(base_url)

    # Wait for network to settle
    page.wait_for_load_state("networkidle")

    # Wait for specific element
    page.wait_for_selector("#async-content", timeout=30000)

    # Or wait with explicit timeout
    page.wait_for_timeout(2000)
```

## Test Credentials

- **Admin user:** `admin@example.com` / `password`
- **Test user creation:** Uses unique UUID-based emails to avoid conflicts

## Security limits vs fast testing (IMPORTANT)

Production auth security stays strict (`.env`: 10 logins/min per IP, account
lockout after 5 failures, progressive duration). The **dev stack only**
relaxes these via `docker-compose.override.yml` (never loaded in prod):
`RATE_LIMIT_AUTH_PER_MINUTE=120`, `ACCOUNT_LOCKOUT_THRESHOLD=100`,
`ACCOUNT_LOCKOUT_DURATION_MINUTES=1` (tunable via `DEV_*` vars in `.env`).

Suite rules that keep auth healthy regardless:

- **Never submit wrong passwords for `admin@example.com`.** Negative login
  tests must use a nonexistent account (e.g. `nosuchuser-e2e@example.com`) —
  same 401 path, but it can't trip the per-account lockout and poison every
  test that follows.
- **Prefer the shared-auth fixtures** (`gallery_page`, module-scoped
  storage-state fixtures) over per-test logins — one login per session.
- **Registration tests must delete the users they create** (see
  `_delete_user_by_email` in `test_registration.py`) and wait for the
  post-register NAVIGATION (`page.wait_for_url`), not just the navbar button.
- Auth is **httpOnly-cookie based** — there is no JS-readable token. In
  `page.evaluate`, call APIs with `fetch(url, {credentials: 'same-origin'})`,
  never `localStorage.getItem('token')`.

## Troubleshooting

### Tests fail with "element detached from DOM"
Add explicit waits after navigation:
```python
page.goto(url)
page.wait_for_load_state("networkidle")
page.wait_for_selector("#element")
```

### Browser doesn't open with --headed
Check DISPLAY environment variable:
```bash
echo $DISPLAY
ls /tmp/.X11-unix/
DISPLAY=:11 pytest ... --headed
```

### Timeout errors
Increase timeout or add explicit waits:
```python
page.wait_for_selector("#element", timeout=30000)
```

### Tests pass locally but fail in CI
E2E tests require the full dev environment running. In CI, either:
- Skip E2E tests: `pytest backend/tests/ --ignore=backend/tests/e2e/`
- Set up services in CI pipeline
