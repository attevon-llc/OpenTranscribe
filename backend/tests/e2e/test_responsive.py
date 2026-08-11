"""
E2E responsive/viewport tests (issue #123 Phase 7).

Verifies the core surfaces stay usable across mobile / tablet / desktop
viewports: the login form renders and the gallery + navbar adapt (mobile
shows the hamburger toggle, desktop shows the full nav).

All read-only — no dev data is touched.

Run:
    pytest backend/tests/e2e/test_responsive.py -v
    DISPLAY=:11 pytest backend/tests/e2e/test_responsive.py -v --headed
"""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.responsive

# This module used to define its own ``FRONTEND_URL`` constant here. A module constant is
# evaluated at import time, so it could not see ``--base-url`` and this file always drove
# whatever was on the default port — even when the run was aimed at an isolated stack
# (issue #431). Every test below takes conftest's ``base_url`` fixture instead.

VIEWPORTS = {
    "mobile": {"width": 375, "height": 667},
    "tablet": {"width": 768, "height": 1024},
    "desktop": {"width": 1920, "height": 1080},
}


@pytest.fixture(params=list(VIEWPORTS.keys()))
def sized_page(request, browser, shared_auth_state):
    """A pre-authenticated page at the parametrized viewport."""
    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport=VIEWPORTS[request.param],
        ignore_https_errors=True,
    )
    page = context.new_page()
    page._viewport_name = request.param  # type: ignore[attr-defined]
    yield page
    page.close()
    context.close()


@pytest.fixture(params=list(VIEWPORTS.keys()))
def anon_page(request, browser):
    """An unauthenticated page at the parametrized viewport."""
    context = browser.new_context(
        viewport=VIEWPORTS[request.param],
        ignore_https_errors=True,
    )
    page = context.new_page()
    page._viewport_name = request.param  # type: ignore[attr-defined]
    yield page
    page.close()
    context.close()


class TestLoginResponsive:
    """The login form is usable at every viewport."""

    def test_login_form_renders(self, anon_page, base_url: str):
        anon_page.goto(base_url)
        anon_page.wait_for_selector("#email", timeout=15000)
        expect(anon_page.locator("#email")).to_be_visible()
        expect(anon_page.locator("#password")).to_be_visible()
        expect(anon_page.locator("button[type=submit]")).to_be_visible()
        # The form must fit the viewport width (no horizontal overflow)
        overflow = anon_page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1, f"Horizontal overflow of {overflow}px at {anon_page._viewport_name}"


class TestGalleryResponsive:
    """The gallery and navbar adapt to each viewport."""

    def test_gallery_renders(self, sized_page, base_url: str):
        sized_page.goto(base_url)
        sized_page.wait_for_selector(".gallery-action-buttons", timeout=30000)
        expect(sized_page.locator(".gallery-action-buttons")).to_be_visible()

    def test_navbar_adapts(self, sized_page, base_url: str):
        sized_page.goto(base_url)
        sized_page.wait_for_selector(".gallery-action-buttons", timeout=30000)
        toggle = sized_page.locator(".mobile-toggle")
        name = sized_page._viewport_name  # type: ignore[attr-defined]
        if name == "mobile":
            # Mobile must offer the hamburger menu
            expect(toggle).to_be_visible(timeout=5000)
        elif name == "desktop":
            # Desktop shows the full nav; the hamburger stays hidden
            expect(toggle).to_be_hidden()

    def test_no_horizontal_overflow(self, sized_page, base_url: str):
        sized_page.goto(base_url)
        sized_page.wait_for_selector(".gallery-action-buttons", timeout=30000)
        # Kept deliberately: the assertion is the ABSENCE of horizontal overflow, measured
        # by an evaluate() that does not poll. Measuring before thumbnails/lazy content
        # have laid out would pass for the wrong reason (issue #431).
        sized_page.wait_for_timeout(1000)
        overflow = sized_page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1, (
            f"Horizontal overflow of {overflow}px at {sized_page._viewport_name}"  # type: ignore[attr-defined]
        )


class TestSearchResponsive:
    """The search page renders its input at every viewport."""

    def test_search_input_renders(self, sized_page, base_url: str):
        sized_page.goto(f"{base_url}/search")
        sized_page.wait_for_selector(".search-page", timeout=15000)
        expect(sized_page.locator(".search-input")).to_be_visible()
