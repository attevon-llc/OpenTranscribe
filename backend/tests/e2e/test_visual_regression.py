"""E2E visual-regression screenshot baselines.

Pixel-level regression guard for the four primary surfaces touched by the
frontend component refactor (branch: refactor/frontend-overhaul), captured in
BOTH light and dark themes:

- gallery / home (``/``)
- file-detail / transcript (``/files/{uuid}``)
- speakers (``/speakers``)
- the Settings modal

pytest-playwright (Python) has no built-in ``to_have_screenshot`` (that's the
JS runner), so this implements a pragmatic approach: take a full-page
``page.screenshot`` at a FIXED viewport and compare against a committed baseline
PNG under ``__screenshots__/`` using a small numpy pixel diff (numpy + Pillow are
already project deps — no new dependency). A change fails only when the fraction
of differing pixels exceeds a small tolerance (anti-aliasing slack).

First run (or after an intentional UI change), write/refresh the baselines::

    UPDATE_SCREENSHOTS=1 pytest backend/tests/e2e/test_visual_regression.py -v

Then re-run WITHOUT the env var to compare against the committed baselines::

    pytest backend/tests/e2e/test_visual_regression.py -v

Requirements:
- Dev environment running: ./opentr.sh start dev
- At least one completed, transcribed file in the dev dataset
- Frontend at localhost:5173, Backend at localhost:5174
  (admin@example.com / password)
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import requests

# Absolute import — the e2e dir is not a package, so a relative import breaks
# collection when invoked as `pytest backend/tests/e2e/` from the repo root.
from conftest import BACKEND_URL as DEFAULT_BACKEND_URL
from PIL import Image
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.visual  # run-e2e.sh runs visual tests serially (quiet stack)

# This module used to define its own ``FRONTEND_URL``/``BACKEND_URL`` constants here.
# A module constant is evaluated at import time, so it could not see ``--base-url`` /
# ``--backend-url`` and this file always drove whatever was on the default ports — even
# when the run was aimed at an isolated stack (issue #431). Everything below takes
# conftest's ``base_url`` / ``backend_url`` fixtures instead.
TEST_ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@example.com")
TEST_ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "password")  # noqa: S105

# Fixed viewport so baselines are deterministic across machines.
VIEWPORT = {"width": 1280, "height": 800}

# Allowed fraction of differing pixels before a comparison is treated as a real
# visual change (covers sub-pixel anti-aliasing / font-hinting jitter).
DIFF_TOLERANCE = 0.005  # 0.5%

# Per-channel intensity delta below which a pixel is considered "same" (ignores
# imperceptible 1-2/255 rendering noise).
CHANNEL_NOISE_THRESHOLD = 12

SCREENSHOT_DIR = Path(__file__).parent / "__screenshots__"

# Set to "1" to (re)write baselines instead of comparing.
UPDATE_SCREENSHOTS = os.environ.get("UPDATE_SCREENSHOTS") == "1"


def _png_to_array(data: bytes) -> np.ndarray:
    """Decode PNG bytes into an RGB uint8 numpy array."""
    with Image.open(io.BytesIO(data)) as img:
        return np.asarray(img.convert("RGB"), dtype=np.uint8)


def _diff_fraction(a: np.ndarray, b: np.ndarray) -> float:
    """Return the fraction of pixels that differ beyond the noise threshold.

    Args:
        a: First image as an RGB uint8 array.
        b: Second image as an RGB uint8 array (must match ``a``'s shape).

    Returns:
        Differing-pixel count divided by total pixel count, in [0, 1].
    """
    delta = np.abs(a.astype(np.int16) - b.astype(np.int16))
    # A pixel differs if ANY channel exceeds the per-channel noise threshold.
    differing = np.any(delta > CHANNEL_NOISE_THRESHOLD, axis=-1)
    return float(differing.sum()) / float(differing.size)


def _compare_or_write(name: str, png_bytes: bytes) -> None:
    """Compare a screenshot against its baseline, or write it in update mode.

    Args:
        name: Baseline file stem (theme + surface), e.g. ``gallery-dark``.
        png_bytes: Full-page PNG screenshot bytes.
    """
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    baseline_path = SCREENSHOT_DIR / f"{name}.png"

    if UPDATE_SCREENSHOTS or not baseline_path.exists():
        baseline_path.write_bytes(png_bytes)
        if not UPDATE_SCREENSHOTS:
            pytest.skip(
                f"Wrote missing baseline {baseline_path.name}; re-run to compare "
                f"(or use UPDATE_SCREENSHOTS=1 to refresh deliberately)."
            )
        return

    current = _png_to_array(png_bytes)
    baseline = _png_to_array(baseline_path.read_bytes())

    if current.shape != baseline.shape:
        # Full-page height can drift with content; crop both to the shared box so
        # a comparison is still meaningful rather than auto-failing on shape.
        h = min(current.shape[0], baseline.shape[0])
        w = min(current.shape[1], baseline.shape[1])
        current = current[:h, :w]
        baseline = baseline[:h, :w]

    fraction = _diff_fraction(current, baseline)
    if fraction > DIFF_TOLERANCE:
        # Persist the failing capture next to the baseline for inspection.
        actual_path = SCREENSHOT_DIR / f"{name}.actual.png"
        actual_path.write_bytes(png_bytes)
        pytest.fail(
            f"Visual regression on '{name}': {fraction:.2%} of pixels changed "
            f"(tolerance {DIFF_TOLERANCE:.2%}). Wrote {actual_path.name} for "
            f"inspection. If intentional, refresh with "
            f"UPDATE_SCREENSHOTS=1 pytest backend/tests/e2e/test_visual_regression.py"
        )


# ---------------------------------------------------------------------------
# Discover a transcribed file for the file-detail surface.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def backend_url(request: pytest.FixtureRequest) -> str:
    """Module-scoped view of conftest's ``backend_url`` fixture (issue #431).

    The discovery fixtures below are module-scoped on purpose — one login and one library
    scan per module — and a module-scoped fixture cannot request the function-scoped
    fixture conftest defines. This applies exactly conftest's precedence
    (``--backend-url`` first, then its ``E2E_BACKEND_URL``/dev default), so the flag is
    honoured here too. Delete once the conftest fixture is session-scoped.
    """
    return str(request.config.getoption("backend_url", default=None) or DEFAULT_BACKEND_URL)


@pytest.fixture(scope="module")
def api_token(backend_url: str) -> str:
    """Authenticate once per module via the backend API."""
    resp = requests.post(
        f"{backend_url}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        pytest.skip(f"Cannot authenticate against dev stack (HTTP {resp.status_code})")
    return str(resp.json()["access_token"])


@pytest.fixture(scope="module")
def transcribed_file_uuid(api_token: str, backend_url: str) -> str:
    """Discover a completed file that has transcript segments (or skip)."""
    listing = requests.get(
        f"{backend_url}/api/files",
        headers={"Authorization": f"Bearer {api_token}"},
        params={"page": "1", "page_size": "100", "sort_by": "upload_time", "sort_order": "desc"},
        timeout=30,
    )
    items: list[dict[str, Any]] = listing.json().get("items", listing.json().get("files", []))
    for f in items:
        if f.get("status") != "completed":
            continue
        detail = requests.get(
            f"{backend_url}/api/files/{f['uuid']}",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30,
        ).json()
        if detail.get("transcript_segments"):
            return str(f["uuid"])
    pytest.skip("No completed transcribed file in dev dataset — required for file-detail capture")
    return ""  # unreachable, satisfies typing


# ---------------------------------------------------------------------------
# Per-theme authenticated context. Theme is forced via localStorage in an init
# script BEFORE first paint (matches static/theme.js, which reads it on load).
# ---------------------------------------------------------------------------
def _login(page: Page, base_url: str) -> None:
    """Log in via the form, tolerating an already-authenticated context."""
    page.goto(base_url)
    if page.locator(".user-button").count():
        page.wait_for_selector(".user-button", timeout=10000)
        return
    page.wait_for_selector("#email", timeout=15000)
    page.fill("#email", TEST_ADMIN_EMAIL)
    page.fill("#password", TEST_ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_selector(".user-button", timeout=30000)


def _make_context(browser: Any, theme: str) -> Any:
    """Build a browser context pinned to a viewport and a forced theme."""
    context = browser.new_context(
        viewport=VIEWPORT,
        ignore_https_errors=True,
        # Freeze animations/transitions and CSS caret so screenshots are stable.
        reduced_motion="reduce",
    )
    context.add_init_script(f"window.localStorage.setItem('theme', '{theme}');")
    return context


def _stabilize(page: Page) -> None:
    """Quiet the page before capture: network idle, no animations, no caret."""
    page.wait_for_load_state("networkidle")
    # Disable animations/transitions and pause media to remove non-determinism.
    page.add_style_tag(
        content=(
            "*,*::before,*::after{animation:none!important;"
            "transition:none!important;caret-color:transparent!important}"
        )
    )
    page.evaluate(
        "document.querySelectorAll('video,audio').forEach(m=>{try{m.pause();"
        "m.currentTime=0;}catch(e){}})"
    )
    page.wait_for_timeout(600)


# Surfaces parametrized over both themes. Each entry: (surface, theme).
SURFACES = ["gallery", "file_detail", "speakers", "settings"]
THEMES = ["light", "dark"]


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("surface", SURFACES)
def test_visual_regression(
    browser: Any,
    theme: str,
    surface: str,
    transcribed_file_uuid: str,
    base_url: str,
) -> None:
    """Capture and compare a full-page screenshot for each surface and theme."""
    context = _make_context(browser, theme)
    page = context.new_page()
    try:
        _login(page, base_url)
        # Confirm the forced theme actually took effect.
        applied = page.evaluate("document.documentElement.getAttribute('data-theme')")
        assert applied == theme, f"Expected data-theme={theme}, got {applied}"

        if surface == "gallery":
            page.goto(base_url)
            page.wait_for_selector(".gallery-action-buttons", timeout=30000)
            page.wait_for_selector(".file-card, .file-list-row", timeout=30000)
            _stabilize(page)
        elif surface == "file_detail":
            page.goto(f"{base_url}/files/{transcribed_file_uuid}")
            page.wait_for_selector(".transcript-segment", timeout=30000)
            _stabilize(page)
        elif surface == "speakers":
            page.goto(f"{base_url}/speakers")
            page.wait_for_selector(".speakers-page", timeout=30000)
            _stabilize(page)
        elif surface == "settings":
            page.goto(base_url)
            page.wait_for_selector(".user-button", timeout=30000)
            page.locator(".user-button").click()
            settings_item = page.locator(".dropdown-menu .dropdown-item", has_text="Settings")
            expect(settings_item.first).to_be_visible(timeout=5000)
            settings_item.first.click()
            expect(page.locator(".settings-modal")).to_be_visible(timeout=10000)
            _stabilize(page)
        else:  # pragma: no cover - defensive
            pytest.fail(f"Unknown surface: {surface}")

        # The settings modal is an overlay; capture the viewport (not full page)
        # so a long scrolled background doesn't add nondeterministic height.
        full_page = surface != "settings"
        png_bytes = page.screenshot(full_page=full_page, animations="disabled")
        _compare_or_write(f"{surface}-{theme}", png_bytes)
    finally:
        page.close()
        context.close()
