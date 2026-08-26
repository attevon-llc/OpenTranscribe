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
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import cast

import pytest
import requests
from playwright.sync_api import Page

#: Repo root, derived from this file: backend/tests/e2e/conftest.py
_REPO_ROOT = Path(__file__).resolve().parents[3]

# ``tests/e2e/pytest.ini`` makes ``tests/e2e`` its own rootdir, and pytest's default
# confcutdir stops conftest collection AT that rootdir — so ``tests/conftest.py`` (which puts
# ``backend/`` on sys.path for the main suite) never runs here. Without this, `from tests.X
# import ...` raises "No module named 'tests'" even though `tests/` resolves fine as a PEP 420
# namespace package once `backend/` is actually on sys.path (verified directly with plain
# Python — the failure is pytest's collection boundary, not an import-system limitation). Doing
# it here, once, lets e2e modules use the same dotted imports as the main suite (e.g.
# `tests.env_gate`) instead of a second, e2e-only copy of shared logic.
_backend_dir = str(_REPO_ROOT / "backend")
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

# ``search_corpus_stack.py`` is the first e2e fixture to import ``app.*`` in-process (real
# ``SessionLocal``/``settings``/OpenSearch client, to inject the corpus via the production
# corpus-injection tool) rather than only talking to the backend over HTTP like every other
# e2e fixture. That import crashes immediately from a bare host process: ``Settings.__init__``
# ``mkdir()``s ``DATA_DIR``/``MODELS_DIR``/``TEMP_DIR``, which default to ``/app/...`` (only
# valid inside the Docker image), and ``POSTGRES_HOST`` defaults to the "postgres" Docker
# service name, unresolvable from the host. ``tests/conftest.py`` already solves exactly this
# for the main suite (throwaway temp dirs + localhost DB/MinIO/OpenSearch port autodetection,
# same env vars this dev stack exposes) — imported here for its **module-level side effects
# only** (setting `os.environ` before any `app.*` import happens), not registered as a plugin,
# so its own fixtures / its own `pytest_plugins` entries don't leak into this rootdir.
import tests.conftest  # noqa: F401,E402 — side effects only, see above

# ``tests/conftest.py`` registers ``search_corpus``/``search_corpus_token``/``neural_available``
# etc. (the self-seeding 6-file search-quality corpus, ``tests/fixtures/search_corpus_stack.py``)
# via its own ``pytest_plugins = ["fixtures.search_corpus_stack", ...]`` — but that conftest is
# cut off from this directory by the same confcutdir boundary explained above, so those fixtures
# are otherwise invisible here. Registered again, explicitly, for this rootdir; the dotted form
# (vs. root conftest's bare ``fixtures.search_corpus_stack``) is required because root conftest's
# own sys.path entry point is ``backend/tests``, while this file put ``backend/`` on sys.path
# instead (see above), so the module lives at ``tests.fixtures.search_corpus_stack`` from here.
pytest_plugins = ["tests.fixtures.search_corpus_stack"]


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


# ---------------------------------------------------------------------------
# Stack preflight
#
# ``scripts/e2e/run-e2e.sh`` checks only that ports 5173/5174 are OPEN. A
# published container port stays open while the process behind it is restarting,
# so that check cannot tell a healthy stack from a broken one — and every failure
# below was first seen as a pile of unrelated per-test timeouts rather than as
# "the stack is not ready".
# ---------------------------------------------------------------------------

#: Vite's dev server appends an HMR cache-buster (``?t=<ms>``) to every module URL
#: it rewrites. Captures ``(store_name, stamp)``.
_STORE_IMPORT_RE = re.compile(r"/src/stores/([A-Za-z0-9_.-]+?)\.ts\?t=(\d+)")


def split_store_modules(sources: dict[str, str]) -> dict[str, dict[str, list[str]]]:
    """Find shared stores the dev server is serving under MORE THAN ONE URL.

    ES modules are keyed by URL, so two importers that resolve ``$stores/auth`` to
    two different ``?t=`` stamps each get their own copy of the module — and
    therefore their own copy of every store it creates. The layout writes the
    signed-in user into one instance; a component holding the other sees ``$user``
    as ``null`` forever.

    This is not hypothetical. ``TagManagerModal.svelte`` sat on a 28-hour-old stamp
    while ``+layout``/``Navbar`` carried the current one, so its
    ``$: isAdmin = $user?.role === 'admin' || $user?.role === 'super_admin'`` was
    permanently false and the tag manager's "Share with everyone" button never
    rendered. That surfaced as ONE mystifying E2E failure
    (``test_promote_publishes_to_the_shared_vocabulary``) against correct app code
    and a correct test; the sibling test asserting the button is ABSENT passed
    throughout, because absence is what a broken store also produces.

    Pure by design: the fetching lives in :func:`_fetch_store_importers`, so the
    detector can be exercised on synthetic text (``test_preflight_guard.py``). A
    scanner that silently matches nothing is indistinguishable from a clean stack.

    Args:
        sources: Compiled module text, keyed by the source path it was fetched from.

    Returns:
        ``{store: {stamp: [importer, ...]}}`` for every store seen under two or more
        stamps; empty when the module graph is consistent.
    """
    seen: dict[str, dict[str, list[str]]] = {}
    for path, text in sources.items():
        for store, stamp in _STORE_IMPORT_RE.findall(text):
            seen.setdefault(store, {}).setdefault(stamp, []).append(path)
    return {store: by_stamp for store, by_stamp in seen.items() if len(by_stamp) > 1}


def _fetch_store_importers(base_url: str) -> dict[str, str]:
    """Fetch the compiled text of every source module that imports a ``$stores/`` module.

    Returns an empty mapping when the frontend is not a Vite dev server (the prod /
    nginx overlays serve a bundle, where this whole failure mode cannot occur), so
    the caller's check degrades to a no-op rather than a false alarm.
    """
    import requests

    frontend = _REPO_ROOT / "frontend"
    if not frontend.is_dir():
        return {}

    candidates = [
        path
        for pattern in ("src/**/*.svelte", "src/**/*.ts")
        for path in frontend.glob(pattern)
        if not path.name.endswith((".test.ts", ".spec.ts"))
        and "$stores/" in path.read_text(encoding="utf-8", errors="ignore")
    ]

    sources: dict[str, str] = {}
    with requests.Session() as session:
        for path in candidates:
            rel = path.relative_to(frontend).as_posix()
            try:
                response = session.get(f"{base_url}/{rel}", timeout=10)
            except requests.RequestException:
                continue
            # nginx answers the SPA fallback (index.html) for these paths; only a
            # Vite dev server returns transformed JS carrying ?t= stamps.
            if response.status_code == 200:
                sources[rel] = response.text
    return sources


def _await_stable_backend(backend_url: str, *, required: int = 3, budget: float = 120.0) -> str:
    """Wait for ``/health`` to answer 200 ``required`` times in a row.

    Consecutive successes, not one — a backend cycling through uvicorn reloads
    answers intermittently, and a single lucky probe reports it as ready. Observed
    at 9 healthy samples out of 40 while another process rewrote ``backend/app``;
    in that state ``initAuth``'s ``/auth/session`` call stalls, and because
    ``+layout.svelte`` renders the app behind ``{#if $authReady}``, ``#email``
    never appears and EVERY login-page fixture times out at once.

    Returns:
        An empty string when the backend is stable, else a description of the failure.
    """
    import requests

    deadline = time.monotonic() + budget
    streak = 0
    last = "no probe completed"
    while time.monotonic() < deadline:
        try:
            status = requests.get(f"{backend_url}/health", timeout=5).status_code
            if status == 200:
                streak += 1
                if streak >= required:
                    return ""
                # Kept (issue #431): polling requests.get("/health") directly for the next
                # consecutive-success sample — no Playwright page exists in this fixture, so no
                # locator can be waited on.
                time.sleep(1.0)
                continue
            last = f"/health returned {status}"
        except requests.RequestException as exc:
            last = f"/health unreachable ({type(exc).__name__})"
        streak = 0
        # Kept (issue #431): backoff before the next requests.get("/health") retry after a
        # failed/unhealthy probe — no Playwright page exists in this fixture, so no locator can
        # be waited on.
        time.sleep(2.0)
    return last


@pytest.fixture(scope="session", autouse=True)
def e2e_stack_preflight(base_url: str, backend_url: str) -> None:
    """Refuse to run the suite against a stack that cannot support it.

    Both checks exist because their absence has already cost debugging time on
    failures that looked like test defects and were not. Failing here — once, with
    the remedy — beats letting the condition surface as a different arbitrary
    subset of timeouts on every run.
    """
    problem = _await_stable_backend(backend_url)
    if problem:
        pytest.exit(
            f"E2E preflight: backend at {backend_url} is not serving steadily ({problem}).\n"
            "Ports being open is not enough — a restarting container keeps its port open.\n"
            "Start or settle the stack first:  ./opentr.sh start dev",
            returncode=3,
        )

    splits = split_store_modules(_fetch_store_importers(base_url))
    if splits:
        detail = "\n".join(
            f"  ${{stores/{store}}} served under {len(by_stamp)} URLs; e.g. "
            + " | ".join(
                f"?t={stamp} <- {sorted(importers)[0]}" for stamp, importers in by_stamp.items()
            )
            for store, by_stamp in sorted(splits.items())
        )
        pytest.exit(
            "E2E preflight: the Vite dev server is serving DUPLICATE store modules, so "
            "components hold different copies of the same Svelte store and props derived "
            f"from it (roles, session) are silently wrong:\n{detail}\n"
            "Remedy:  ./opentr.sh restart-frontend",
            returncode=3,
        )


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


# ---------------------------------------------------------------------------
# Owned media: the alternative to acting on whatever the dev library holds
# ---------------------------------------------------------------------------
#: The committed 10 s / mono / 16 kHz clip — see ``tests/fixtures/media/README.md``.
#: Real speech, unlike the ffmpeg sine tones above, so transcript assertions stay
#: meaningful; short, so a real reprocess costs seconds and fits any GPU.
COMMITTED_SAMPLE_MEDIA = (
    Path(__file__).resolve().parents[1] / "fixtures" / "media" / "sample_short.wav"
)

#: Prefix for every file these suites upload. Also listed in
#: ``scripts/cleanup-test-users.py``'s ORPHAN_PATTERNS, the backstop for a run that
#: dies before its teardown.
OWNED_MEDIA_PREFIX = "e2e-owned-"

#: Statuses a file can never leave on its own. Reaching one decides the answer, so
#: continuing to poll for "completed" only burns the rest of the window.
TERMINAL_FAILURE_STATUSES = frozenset({"error", "cancelled"})


def wait_for_stable_completion(
    backend_url: str, token: str, file_uuid: str, timeout_secs: int = 300
) -> str:
    """Poll until *file_uuid* is STABLY completed.

    One ``completed`` poll is not enough: chained async stages (analytics, search
    indexing) can flip a file back to PROCESSING moments later, racing the next
    request into an INVALID_STATUS rejection.

    Args:
        backend_url: Base URL of the API under test.
        token: Bearer token.
        file_uuid: File to poll.
        timeout_secs: Upper bound on the wait.

    Returns:
        The last observed status.
    """
    headers = {"Authorization": f"Bearer {token}"}
    consecutive = 0
    deadline = time.time() + timeout_secs
    status = "unknown"
    while time.time() < deadline:
        resp = requests.get(f"{backend_url}/api/files/{file_uuid}", headers=headers, timeout=30)
        status = str(resp.json().get("status", "unknown")) if resp.status_code == 200 else "unknown"
        if status in TERMINAL_FAILURE_STATUSES:
            return status
        consecutive = consecutive + 1 if status == "completed" else 0
        if consecutive >= 2:
            return status
        # Pure API polling: this helper takes no Playwright page, so there is no
        # locator to wait on and the sleep IS the poll interval (issue #431).
        time.sleep(3)
    return status


def wait_until_safe_to_delete(
    backend_url: str, token: str, file_uuid: str, timeout_secs: int = 90
) -> bool:
    """Poll until *file_uuid* has no live ``active_task_id``, not merely ``completed``.

    ``is_file_safe_to_delete`` (``app/utils/task_utils.py``) is deliberately NOT gated
    on ``status == PROCESSING``: a selective follow-on stage on an already-completed
    file (search_indexing/analytics/speaker_llm/summarization/topic_extraction/
    speaker_clustering) sets ``active_task_id`` via ``create_task_record`` without ever
    flipping ``status`` away from ``completed``. So two consecutive ``completed`` polls
    from ``wait_for_stable_completion`` do not guarantee the file is actually safe to
    delete — a real run hit exactly this: DELETE 409'd twice right after completion,
    each time against a *different* ``active_task_id``, and needed ``/force`` to clean
    up (found running the full pipeline verification against a fresh dev stack).
    Waiting on this directly lets the owning test's teardown delete on the first real
    attempt instead of quietly falling back to ``/force`` every time.

    Args:
        backend_url: Base URL of the API under test.
        token: Bearer token.
        file_uuid: File to poll.
        timeout_secs: Upper bound on the wait — short, because this only covers the
            tail of already-``completed`` follow-on stages, not the main pipeline.

    Returns:
        True once ``active_task_id`` is observed clear; False if the deadline passed
        first (the caller still has ``delete_media_file``'s own 409-retry + force
        fallback as a backstop).
    """
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        resp = requests.get(f"{backend_url}/api/files/{file_uuid}", headers=headers, timeout=30)
        if resp.status_code == 200 and resp.json().get("active_task_id") is None:
            return True
        time.sleep(3)
    return False


def delete_media_file(backend_url: str, token: str, file_uuid: str) -> None:
    """Remove a test-created file, retrying past the window where it is still busy.

    A file mid-pipeline answers 409, so a single DELETE is not enough. Waits out any
    live follow-on task first (see ``wait_until_safe_to_delete``), then falls back to
    ``/force`` so a failed test can never leave its upload in the dev library.
    """
    headers = {"Authorization": f"Bearer {token}"}
    wait_until_safe_to_delete(backend_url, token, file_uuid)
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            resp = requests.delete(
                f"{backend_url}/api/files/{file_uuid}", headers=headers, timeout=30
            )
            if resp.status_code in (200, 204, 404):
                return
        except requests.RequestException:
            pass
        time.sleep(5)
    try:
        requests.delete(f"{backend_url}/api/files/{file_uuid}/force", headers=headers, timeout=30)
    except requests.RequestException:
        pass


@pytest.fixture
def owned_media_factory(backend_url: str):
    """Upload media the calling suite OWNS, and delete it however the test ends.

    Exists because several suites used to run **real, mutating** pipeline actions —
    reprocess, summarize, speaker identification — against whatever recording the dev
    library happened to contain. That is issue #541, and it did measurable damage: a
    reprocess rewrote an ambient file's transcript, created new speaker rows, and
    through the auto-accept path mutated an ambient speaker profile's centroid and
    counters, permanently changing the speakers page.

    Yields:
        ``upload(token) -> dict``: uploads the committed clip, waits for a stable
        completion and returns the file payload. Every file it creates is deleted on
        teardown, in a ``finally``-equivalent — a cleanup that only runs on the happy
        path is exactly the one that does not run when a test fails.
    """
    created: list[tuple[str, str]] = []

    def _upload(token: str, *, timeout_secs: int = 300) -> dict:
        if not COMMITTED_SAMPLE_MEDIA.exists():  # pragma: no cover - the fixture is tracked
            pytest.skip(f"missing media fixture {COMMITTED_SAMPLE_MEDIA}")

        headers = {"Authorization": f"Bearer {token}"}
        # uuid-suffixed: never a fixed identity for a persisted object, and it makes
        # the upload identifiable in a listing while a run is being debugged.
        name = f"{OWNED_MEDIA_PREFIX}{uuid.uuid4().hex[:8]}{COMMITTED_SAMPLE_MEDIA.suffix}"
        with COMMITTED_SAMPLE_MEDIA.open("rb") as fh:
            resp = requests.post(
                f"{backend_url}/api/files",
                headers=headers,
                files={"file": (name, fh, "audio/wav")},
                timeout=300,
            )
        assert resp.status_code == 200, f"Upload failed: {resp.status_code} {resp.text[:300]}"
        file_uuid = str(resp.json()["uuid"])
        created.append((token, file_uuid))

        status = wait_for_stable_completion(backend_url, token, file_uuid, timeout_secs)
        assert status == "completed", f"uploaded fixture never completed (status={status})"

        detail = requests.get(f"{backend_url}/api/files/{file_uuid}", headers=headers, timeout=30)
        assert detail.status_code == 200, f"Failed to re-read upload: {detail.text[:300]}"
        return dict(detail.json())

    try:
        yield _upload
    finally:
        for token, file_uuid in created:
            delete_media_file(backend_url, token, file_uuid)


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

        try:
            self.page.wait_for_url(lambda url: "register" not in url.lower(), timeout=10000)
            return True
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
            # Kept (issue #431): raw requests.post retry against rate limiting — no Playwright
            # page in this helper, no locator to wait on.
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
