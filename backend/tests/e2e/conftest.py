# mypy: disable-error-code="redundant-cast"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures that declare Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object. Declared once here rather than as a cast at every call site — casts
# bury the assertion, and widening a production signature to suit a test is worse.
"""
E2E Test Configuration with Playwright

These tests run against the actual running dev environment (frontend + backend).
They test real user flows through the browser.

Requirements:
- Dev environment running: ./opentr.sh start dev
- Frontend at localhost:5173
- Backend at localhost:5174

Run E2E tests only:
    pytest backend/tests/e2e/ -v

Run with visible browser (XRDP):
    pytest backend/tests/e2e/ -v --headed --browser chromium

Run with specific base URL:
    pytest backend/tests/e2e/ -v --base-url http://localhost:5173
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest
from playwright.sync_api import Page


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add ``--backend-url`` to pair with pytest-base-url's ``--base-url``.

    Without it, ``--base-url`` could move the browser to an isolated stack while the API
    helpers kept talking to the default one.
    """
    parser.addoption(
        "--backend-url",
        action="store",
        default=None,
        help="Backend API base URL (default: $E2E_BACKEND_URL, else http://localhost:5174)",
    )


# Default URLs for dev environment
FRONTEND_URL = os.environ.get("E2E_FRONTEND_URL", "http://localhost:5173")
BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://localhost:5174")

# Test user credentials (these should exist in dev database)
TEST_ADMIN_EMAIL = "admin@example.com"
TEST_ADMIN_PASSWORD = "password"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Configure browser context for all tests."""
    return {
        **browser_context_args,
        "viewport": {"width": 1920, "height": 1080},
        "ignore_https_errors": True,
    }


@pytest.fixture(scope="session")
def base_url(request: pytest.FixtureRequest) -> str:
    """Frontend base URL: ``--base-url`` > ``E2E_FRONTEND_URL`` > the dev default.

    This unconditionally returned ``FRONTEND_URL``, which SHADOWED pytest-base-url's
    ``base_url`` fixture and made the ``--base-url`` flag do nothing — the same flag this
    module's own docstring documents, and the one ``e2e/pytest.ini`` sets a value for. A run
    aimed at an isolated ``--fresh --port-offset`` stack therefore drove the LIVE stack
    instead, silently, which is exactly the mixed-stack state ``--fresh`` exists to prevent
    (issue #431).

    Honouring the flag first makes the documented invocation work; ``E2E_FRONTEND_URL``
    remains for callers that set it (``scripts/e2e/run-e2e.sh``).
    """
    from_flag = request.config.getoption("base_url", default=None)
    if from_flag:
        return str(from_flag)
    return FRONTEND_URL


@pytest.fixture(scope="session")
def backend_url(request: pytest.FixtureRequest) -> str:
    """Backend base URL: ``--backend-url`` > ``E2E_BACKEND_URL`` > the dev default.

    Kept in step with ``base_url`` above: pointing the browser at one stack while the API
    helpers talk to another is worse than pointing both at the wrong one, because the
    mismatch is invisible until an assertion disagrees with what the UI shows.

    SESSION-scoped deliberately, matching ``base_url``. It was function-scoped, which made it
    unusable from the many module- and session-scoped fixtures that log in or create a test
    user ONCE (login rate limits; one MFA address per session) — a wider-scoped fixture
    requesting a narrower one is a hard ``ScopeMismatch`` at setup. Three independent passes
    over the e2e suite each hit this and each worked around it with a locally widened copy of
    these six lines. Nothing here needs per-test scope: it resolves a string from a CLI flag
    and the environment (issue #431).
    """
    from_flag = request.config.getoption("backend_url", default=None)
    if from_flag:
        return str(from_flag)
    return BACKEND_URL


@pytest.fixture
def login_page(page: Page, base_url: str):
    """Navigate to the login page and wait until it is ACTUALLY ready.

    Waiting for ``#email`` alone is not enough. Half of what the login page renders —
    the registration link, the SSO/PKI buttons, the "or continue with" divider — is
    gated on ``authMethods``, which arrives from an async ``GET /api/auth/methods``.
    Asserting on any of it before that response lands is a race that only shows up
    under parallel load: ``test_registration_link_exists`` failed in a 3-worker run
    while passing every time the same page was driven by hand.

    Waiting for the response itself is the deterministic signal. The ``expect_response``
    context is entered BEFORE ``goto`` so the response cannot be missed.
    """
    with page.expect_response(lambda r: "/api/auth/methods" in r.url, timeout=20000):
        page.goto(base_url)
    page.wait_for_selector("#email", timeout=10000)
    return page


@pytest.fixture
def authenticated_page(page: Page, base_url: str):
    """Return a page that's already logged in as admin."""
    page.goto(base_url)
    page.wait_for_selector("#email", timeout=10000)

    # Login as admin
    page.fill("#email", TEST_ADMIN_EMAIL)
    page.fill("#password", TEST_ADMIN_PASSWORD)
    page.click("button[type=submit]")

    # Wait for the redirect OFF the login page (a bare f"{base_url}/**"
    # pattern matches /login itself and returns before navigation happens)
    page.wait_for_url(lambda url: "/login" not in url, timeout=15000)

    # Wait for page to be fully loaded
    page.wait_for_load_state("networkidle")

    return page


@pytest.fixture(scope="session")
def shared_auth_state(browser, base_url: str):
    """Login ONCE per session and persist browser storage state.

    Repeated per-test logins trip the backend's login rate limiting in larger
    runs — prefer this with a fresh context per test (see gallery_page).
    """
    import tempfile

    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    page = context.new_page()
    page.goto(base_url)
    page.wait_for_selector("#email", timeout=15000)
    page.fill("#email", TEST_ADMIN_EMAIL)
    page.fill("#password", TEST_ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_selector(".gallery-action-buttons", timeout=30000)

    fd, state_file = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    context.storage_state(path=state_file)
    page.close()
    context.close()

    yield state_file

    if os.path.exists(state_file):
        os.unlink(state_file)


@pytest.fixture
def gallery_page(browser, shared_auth_state, base_url: str):
    """A fresh pre-authenticated page on the gallery (no per-test login)."""
    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    page = context.new_page()
    page.goto(base_url)
    page.wait_for_selector(".gallery-action-buttons", timeout=30000)
    yield page
    page.close()
    context.close()


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _generate_media(filename: str, ffmpeg_args: list[str]) -> Path:
    """Generate a small media fixture with ffmpeg (cached across runs)."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available — cannot generate media fixtures")
    FIXTURES_DIR.mkdir(exist_ok=True)
    out = FIXTURES_DIR / filename
    if not out.exists():
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", *ffmpeg_args, str(out)],
            check=True,
            timeout=60,
        )
    return out


@pytest.fixture(scope="session")
def sample_audio() -> Path:
    """2-second 440 Hz mono WAV — small, valid, passes magic-byte validation."""
    return _generate_media(
        "sample_audio.wav",
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ac", "1", "-ar", "16000"],
    )


@pytest.fixture(scope="session")
def sample_video() -> Path:
    """2-second test-pattern MP4 with a sine audio track."""
    return _generate_media(
        "sample_video.mp4",
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
        ],
    )


@pytest.fixture
def console_errors(page: Page):
    """Capture console errors during test."""
    errors = []

    def handle_console(msg):
        if msg.type == "error":
            errors.append(msg.text)

    page.on("console", handle_console)
    yield errors
    page.remove_listener("console", handle_console)


@pytest.fixture
def screenshot_on_failure(request, page: Page):
    """Take screenshot on test failure."""
    yield
    if request.node.rep_call.failed:
        screenshot_dir = "backend/tests/e2e/screenshots"
        os.makedirs(screenshot_dir, exist_ok=True)
        test_name = request.node.name.replace("/", "_").replace(":", "_")
        page.screenshot(path=f"{screenshot_dir}/{test_name}.png")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Hook to capture test result for screenshot_on_failure fixture."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


class AuthHelper:
    """Helper class for authentication operations."""

    def __init__(self, page: Page, base_url: str):
        self.page = page
        self.base_url = base_url

    def login(self, email: str, password: str) -> bool:
        """Login with credentials. Returns True if successful."""
        self.page.goto(self.base_url)
        self.page.wait_for_selector("#email", timeout=10000)
        self.page.fill("#email", email)
        self.page.fill("#password", password)
        self.page.click("button[type=submit]")

        # Wait for either success (redirect) or error message
        try:
            self.page.wait_for_url(f"{self.base_url}/**", timeout=10000)
            return True
        except Exception:
            return False

    def logout(self):
        """Logout current user."""
        # Click user menu and logout
        self.page.click("[data-testid=user-menu], .user-menu, #user-menu")
        self.page.click("[data-testid=logout], button:has-text('Logout')")
        self.page.wait_for_url(f"{self.base_url}/login**", timeout=10000)

    def register(self, username: str, email: str, password: str) -> bool:
        """Register a new user. Returns True if successful."""
        self.page.goto(f"{self.base_url}/login")
        self.page.wait_for_selector("a[href*=register]", timeout=10000)
        self.page.click("a[href*=register]")

        # Fill registration form
        self.page.wait_for_selector("#username", timeout=10000)
        self.page.fill("#username", username)
        self.page.fill("#email", email)
        self.page.fill("#password", password)
        self.page.fill("#confirmPassword", password)

        # Submit
        self.page.click("button:has-text('Create Account')")
        self.page.wait_for_timeout(2000)

        # Check for success - redirected away from register page
        try:
            return "register" not in self.page.url.lower()
        except Exception:
            return False

    def get_error_message(self) -> str | None:
        """Get any error message displayed on the page."""
        error_selectors = [
            ".error-message",
            "[role=alert]",
            ".alert-error",
            ".text-red-500",
            "[data-testid=error]",
        ]
        for selector in error_selectors:
            element = self.page.query_selector(selector)
            if element:
                return cast(str | None, element.text_content())
        return None


@pytest.fixture
def auth_helper(page: Page, base_url: str):
    """Provide authentication helper."""
    return AuthHelper(page, base_url)


class APIHelper:
    """Helper for making API calls alongside browser tests."""

    def __init__(self, backend_url: str):
        self.backend_url = backend_url
        self._token: str | None = None

    def login(self, email: str, password: str) -> dict:
        """Login via API and store token (retry through transient rate limiting)."""
        import time

        import requests

        result: dict = {}
        for attempt in range(4):
            response = requests.post(
                f"{self.backend_url}/api/auth/token",
                data={"username": email, "password": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            result = cast(dict, response.json())
            if response.status_code == 200:
                self._token = cast(str, result["access_token"])
                return result
            time.sleep(5 * (attempt + 1))
        return result

    def get(self, endpoint: str) -> dict:
        """Make authenticated GET request."""
        import requests

        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        response = requests.get(f"{self.backend_url}{endpoint}", headers=headers, timeout=30)
        return cast(dict, response.json())

    def post(self, endpoint: str, data: dict) -> dict:
        """Make authenticated POST request."""
        import requests

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        response = requests.post(
            f"{self.backend_url}{endpoint}", json=data, headers=headers, timeout=30
        )
        return cast(dict, response.json())

    def delete(self, endpoint: str) -> int:
        """Make authenticated DELETE request; returns the status code."""
        import requests

        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        response = requests.delete(f"{self.backend_url}{endpoint}", headers=headers, timeout=30)
        return response.status_code


@pytest.fixture
def api_helper(backend_url: str):
    """Provide API helper for backend calls."""
    return APIHelper(backend_url)
