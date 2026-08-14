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

# Flat import, NOT `from tests.e2e._visual_diff import ...`.
#
# This module only ever runs under `tests/e2e/pytest.ini`, which makes
# `tests/e2e` the rootdir; `tests/` is not a package and never reaches sys.path
# there, so the dotted form raises `ModuleNotFoundError: No module named 'tests'`
# and the ENTIRE visual suite fails to collect. It shipped that way briefly and
# was invisible because the eight baselines were already failing for unrelated
# reasons, so nobody ran the module. `tests/unit/test_visual_diff_fraction.py`
# keeps the dotted form, which is correct under the repo-root rootdir it runs in.
from _visual_diff import CHANNEL_NOISE_THRESHOLD  # noqa: F401
from _visual_diff import DIFF_TOLERANCE
from _visual_diff import diff_fraction as _diff_fraction
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

SCREENSHOT_DIR = Path(__file__).parent / "__screenshots__"

# Set to "1" to (re)write baselines instead of comparing.
UPDATE_SCREENSHOTS = os.environ.get("UPDATE_SCREENSHOTS") == "1"


def _png_to_array(data: bytes) -> np.ndarray:
    """Decode PNG bytes into an RGB uint8 numpy array."""
    with Image.open(io.BytesIO(data)) as img:
        return np.asarray(img.convert("RGB"), dtype=np.uint8)


def _compare_or_write(name: str, png_bytes: bytes) -> None:
    """Compare a screenshot against its baseline, or write it in update mode.

    Args:
        name: Baseline file stem (theme + surface), e.g. ``gallery-dark``.
        png_bytes: Full-page PNG screenshot bytes.
    """
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    baseline_path = SCREENSHOT_DIR / f"{name}.png"

    if UPDATE_SCREENSHOTS:
        baseline_path.write_bytes(png_bytes)
        return

    if not baseline_path.exists():
        # A missing baseline is a FAILURE, never an auto-write.
        #
        # This used to write the file and `pytest.skip`, which made the suite
        # self-approving in two runs with no human ever looking at the image:
        # run once to write, run again to "pass" against what the possibly
        # broken build just produced. `scripts/e2e/run-e2e.sh` treats skips as
        # success, so the first run was green too.
        pytest.fail(
            f"No baseline for '{name}' at {baseline_path}. A screenshot is only "
            f"a baseline once a human has looked at it. Generate it deliberately "
            f"with UPDATE_SCREENSHOTS=1 and review the image in the diff before "
            f"committing it."
        )

    current = _png_to_array(png_bytes)
    baseline = _png_to_array(baseline_path.read_bytes())

    # Shapes may differ; _diff_fraction charges the non-overlapping area as
    # differing rather than cropping it away unseen.
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
    # Kept deliberately: a paint/layout settle before a screenshot. The comparison is a
    # pixel diff, not a locator, so there is nothing to auto-wait on (issue #431).
    page.wait_for_timeout(600)


# Surfaces parametrized over both themes. Each entry: (surface, theme).
SURFACES = ["gallery", "file_detail", "speakers", "settings"]
THEMES = ["light", "dark"]


#: Regions whose pixels change without the UI changing, per surface (issue #451).
#:
#: These are masked out of the capture rather than tolerated by the diff budget.
#: Two back-to-back runs on identical code and data differ by 0.08–0.09% purely
#: because of these elements — live CPU/disk/GPU-VRAM gauges and a relative
#: "Last run: 21m ago" chip. That is inside the 0.5% tolerance today, so the suite
#: passes, but the headroom is only ~5.5x and it shrinks as digit widths change
#: over longer intervals. A tolerance absorbing known noise is a tolerance that
#: cannot also catch a small real regression.
#:
#: The counters are masked for a second and more important reason: they render
#: live dev-database totals (users, files, segments, clusters, profiles). A
#: baseline containing them is invalidated by the next upload, which is exactly
#: why the current 8 baselines cannot be honestly refreshed.
#:
#: ⚠️ Only selectors VERIFIED to match on the live app are listed. A first draft
#: also carried `settings`: `.stat-value`, `.stat-detail`, `.progress-fill`,
#: `.model-value` — all four matched **zero** elements, so that entry masked
#: nothing while reading as though it did. Settings is deliberately absent until
#: its selectors are confirmed against a modal that stays open long enough to
#: query (it closed on its own in a scripted session, which is its own question).
#: Do not add a selector here without checking `page.locator(sel).count()`.
_VOLATILE_SELECTORS: dict[str, tuple[str, ...]] = {
    # "Last run: N minutes ago" (1 element) + per-cluster membership counts (20).
    "speakers": (".last-clustered-chip", ".member-count"),
}


def _volatile_regions(page: Page, surface: str) -> list[Any]:
    """Locators to paint over before comparing, for *surface*.

    Only selectors that actually match are returned. Playwright masks every
    element a locator resolves to, and a selector matching nothing is silently a
    no-op — so a renamed class would quietly stop masking and reintroduce the
    noise it was added to remove. That is not hypothetical: the first draft of
    `_VOLATILE_SELECTORS` listed four settings selectors that matched **zero**
    elements, and nothing about the run said so.

    The masked surfaces therefore assert their own coverage below rather than
    trusting the table.
    """
    return [
        page.locator(selector)
        for selector in _VOLATILE_SELECTORS.get(surface, ())
        if page.locator(selector).count()
    ]


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
            # A masked surface must actually mask something. Playwright treats a
            # selector matching nothing as a silent no-op, so without this the
            # relative-time chip and the live cluster counts would drift back
            # into the baseline the moment a class is renamed — and the run would
            # look identical. Asserted here rather than in a separate test
            # because it is only knowable against the rendered page.
            assert _volatile_regions(page, "speakers"), (
                "None of _VOLATILE_SELECTORS['speakers'] matched anything on the "
                "speakers page, so this capture masks nothing and its baseline "
                "will absorb the 'Last run: N ago' chip and live cluster counts."
            )
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
        png_bytes = page.screenshot(
            full_page=full_page,
            animations="disabled",
            mask=_volatile_regions(page, surface),
            mask_color="#ff00ff",
        )
        _compare_or_write(f"{surface}-{theme}", png_bytes)
    finally:
        page.close()
        context.close()
