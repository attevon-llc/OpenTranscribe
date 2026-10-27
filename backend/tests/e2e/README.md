# E2E Tests for OpenTranscribe

End-to-end tests using pytest-playwright to verify frontend and backend work together through real browser automation.

## Prerequisites

1. **Dev environment running:**
   ```bash
   ./opentr.sh start dev
   ```

2. **Python venv with dependencies:** `pytest-playwright`, `playwright` and `axe-playwright-python`
   come from `backend/requirements-dev.txt` (see `backend/CLAUDE.md` for the venv setup). Then
   install the browser once:
   ```bash
   source backend/venv/bin/activate
   playwright install chromium
   ```

3. **Services accessible** (defaults; `E2E_FRONTEND_URL` / `E2E_BACKEND_URL` or
   `run-e2e.sh --fresh <name>` retarget an isolated stack):
   - Frontend: http://localhost:5173
   - Backend: http://localhost:5174

4. **ffmpeg** on the host (the media fixtures are generated with it; tests skip without it).

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
./scripts/e2e/run-e2e-smoke.sh          # quick read-mostly subset (five test files, listed in the script)
./scripts/e2e/run-e2e.sh -m upload      # one marker (single pytest run, no skip ceilings)
DISPLAY=:11 ./scripts/e2e/run-e2e.sh --headed   # visible browser
./scripts/e2e/run-e2e.sh --fresh myname # target an isolated `--fresh --port-offset` deployment
./scripts/e2e/update-visual-baselines.sh --reason "<why>"   # the supported way to refresh visual baselines
```

Without `-m`/`-n`, `run-e2e.sh` runs three phases: (1) everything except `visual`, `chat` and
`support_access` in parallel (`E2E_WORKERS`, default 3, `--dist loadfile`), (2) `-m chat`
serially, (3) `-m visual` serially. Each phase writes a junit XML (printed at the end) and has
a skip ceiling; over the ceiling the run exits 4 (NOT MEASURED) instead of passing. Prefer the
runner to a bare `pytest` for the full suite.

Visual baselines in `__screenshots__/` must not be refreshed by running
`UPDATE_SCREENSHOTS=1 pytest` against the shared dev stack: the image then measures that stack's
library, not the UI. Use `update-visual-baselines.sh`.

### Markers

The registered markers are the `markers =` list in [pytest.ini](pytest.ini) (it is the
authority; `--strict-markers` is on, and a unit test keeps the list and the usage in sync):

```bash
pytest backend/tests/e2e/ -m upload          # upload stepper flows
pytest backend/tests/e2e/ -m search          # search page
pytest backend/tests/e2e/ -m settings        # settings modal
pytest backend/tests/e2e/ -m transcription   # transcript view/editing
pytest backend/tests/e2e/ -m "not slow"      # skip slow tests
# also: slow, auth, pki, keycloak, ldap, watch, e2e, gallery, responsive, visual, chat,
#       pipeline_smoke, tags, collections, support_access, a11y
```

Several markers need a deployment shape: `keycloak`/`ldap` need the test IdP containers
(`./opentr.sh start dev --with-keycloak-test` / `--with-ldap-test`), `watch` needs `--with-watch`,
`chat` needs an LLM provider (`--with-mock-llm` works) and a completed transcript,
`support_access` needs `TENANCY_MODE=multi`, and `pipeline_smoke` is strictly opt-in
(`RUN_PIPELINE_SMOKE=1`, needs `--with-llm-test`; `run-dev-tests.sh --with-pipeline-smoke`
arranges it).

### Additional Options

```bash
# Screenshot on failure
pytest backend/tests/e2e/ -v --screenshot only-on-failure

# Slow down for debugging (100ms between actions)
pytest backend/tests/e2e/ -v --headed --slowmo 100
```

### Media Fixtures

`sample_audio` / `sample_video` fixtures generate tiny clips with **ffmpeg**
into `backend/tests/e2e/fixtures/` (created on first use, gitignored, cached) — no external
downloads. Upload tests delete everything they create through the API, so
the dev environment stays clean.

## Test Structure

```
backend/tests/e2e/
├── conftest.py                       # Shared fixtures and the stack preflight
├── pytest.ini                        # E2E-specific pytest config (separate rootdir config)
├── README.md                         # This file
├── a11y-allowlist.txt, a11y_lib.py   # axe-core allowlist (count-aware, per surface) and its helpers
├── __screenshots__/                  # committed visual-regression baselines (light/dark)
├── screenshot-provenance/            # provenance records for those baselines
├── fixtures/                         # ffmpeg-generated media (gitignored)
├── stack_urls.py, timeouts.py, frontend_warm.py, idp_overlay.py,
│   ldap_fixture_users.py, owned_corpus.py, _visual_diff.py   # support modules (not tests)
└── test_*.py                         # one file per surface (below)
```

Test files: `test_a11y`, `test_audio_extraction`, `test_auth_buttons`, `test_auth_flow`,
`test_chat`, `test_chat_grounding`, `test_chat_trace_panel`, `test_collection_management`,
`test_collection_sharing`, `test_file_detail_transcript`, `test_file_status_nav`,
`test_full_pipeline_smoke`, `test_gallery_actions`, `test_gallery_list_alignment`,
`test_language_switch`, `test_ldap_oidc`, `test_local_models`, `test_login`,
`test_media_download`, `test_media_download_ui`, `test_mfa`, `test_pki`, `test_preflight_guard`,
`test_redaction_e2e`, `test_registration`, `test_responsive`, `test_search`,
`test_settings_modal`, `test_settings_pipeline`, `test_shared_file_permissions`,
`test_speaker_gender_clusters`, `test_speaker_sharing`, `test_support_access`,
`test_tag_management`, `test_upload`, `test_visual_regression`, `test_watch_sources_e2e`.
`ls backend/tests/e2e/test_*.py` is the current list.

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

### Login and Registration Tests (`test_login.py`, `test_registration.py`)

Form validation, success and failure paths, security (password hiding, rate limiting), session
persistence, UI elements, alternative-auth buttons, accessibility and console errors for login;
field validation, duplicate prevention, success flow and UI for registration. Registration tests
delete the users they create. Classes and counts change; read the files.

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
Config UI and login flow for LDAP and Keycloak. Requires running containers (`--with-ldap-test`, `--with-keycloak-test`):
```bash
RUN_AUTH_E2E=true pytest backend/tests/e2e/test_ldap_oidc.py -v
```
See `backend/tests/AUTH_TEST_SETUP.md` for container setup instructions.

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

        # expect() polls until the assertion passes (or times out) — no fixed
        # wait needed before it.
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

    # Or, preferred: let expect() poll for the state you actually care about
    # instead of a fixed wait_for_timeout()
    expect(page.locator("#async-content")).to_have_text("Ready")
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

### Tests pass locally but are not run in CI
E2E is local-only by design (there is no GitHub Actions e2e job); it needs the full dev stack. The
default `pytest` run in `backend/` already skips this directory (`norecursedirs`), so the unit/API
suite never starts a browser.

### Many tests time out at once, or `#email` never appears
Suspect the stack, not the tests: a restarting backend keeps its port open while the SPA waits on
a 60 s auth probe. The suite's preflight (see `backend/tests/CLAUDE.md`, "Before you debug a flaky
E2E test") checks for that; also do not run `pre-commit` or edit `backend/app` while E2E runs.
