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
    # 1280 was the gap that let issue #452 ship: the navbar overflowed and clipped
    # the user menu off-screen from 1025px to 1434px, and the three widths above
    # straddle that band without entering it. 1280 is also a very common laptop
    # width, and the widest point of the broken band was 155px of clipping.
    "laptop": {"width": 1280, "height": 800},
    "desktop": {"width": 1920, "height": 1080},
}

#: Widths swept by :func:`test_navbar_user_menu_never_clips`. One viewport cannot
#: cover this: the failure is a BAND, not a point, and which widths break moves
#: every time a nav item is added or a breakpoint is retuned.
NAVBAR_SWEEP_WIDTHS = (800, 900, 1024, 1100, 1200, 1280, 1300, 1366, 1440, 1500, 1600, 1920)


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


class TestNavbarFits:
    """The navbar must fit the viewport at every desktop/tablet width.

    Separate from :class:`TestGalleryResponsive` because it needs its own width
    sweep rather than the three parametrized viewports, and because it asserts a
    different thing: not "the document does not scroll sideways" but "this
    specific control is inside the window".
    """

    def test_navbar_user_menu_never_clips(self, browser, shared_auth_state, base_url: str):
        """The user menu stays inside the viewport across the whole width range.

        This is the regression guard for issue #452, where the navbar grew (it
        gained Chat, and "Gallery" split into "All files" + "Gallery") without
        its breakpoints being retuned. The user menu was clipped off the right
        edge from 1025px to 1434px — by 155px at the worst point — so the signed
        in user could not reach settings or log out on a common laptop width.

        **Two weaker assertions were measured against the live bug and BOTH pass
        while it is present**, which is why this one is written the way it is:

        * ``is_visible()`` returns True for a clipped element. Playwright's
          visibility means rendered and non-empty, not on-screen.
        * The ``scrollWidth - clientWidth`` check in
          :class:`TestGalleryResponsive` reports **0px of overflow while the
          button hangs 127px past the edge**. The navbar is ``position: fixed``,
          so its overflow never extends the document's scroll width. That test
          could not have caught this at any viewport, and adding 1280 to
          ``VIEWPORTS`` alone would not have been enough.

        So the assertion has to be geometric: compare the element's own bounding
        box against the viewport width.
        """
        context = browser.new_context(
            storage_state=shared_auth_state,
            viewport={"width": NAVBAR_SWEEP_WIDTHS[-1], "height": 800},
            ignore_https_errors=True,
        )
        page = context.new_page()
        try:
            page.goto(base_url)
            page.wait_for_selector(".user-button", timeout=30000)

            clipped: list[str] = []
            probed = 0
            for width in NAVBAR_SWEEP_WIDTHS:
                page.set_viewport_size({"width": width, "height": 800})
                # A fixed wait, not a poll: the assertion is the ABSENCE of
                # clipping, so there is no state to poll for. Re-layout after a
                # viewport change is synchronous in Chromium; this is slack for
                # the transition on .navbar-container's gap.
                page.wait_for_timeout(250)

                box = page.locator(".user-button").first.bounding_box()
                if box is None:
                    # Below the mobile breakpoint the button moves into the
                    # hamburger menu and has no box. Not a failure — but it must
                    # not silently swallow the whole sweep, hence `probed`.
                    continue
                probed += 1
                overhang = box["x"] + box["width"] - width
                if overhang > 0.5:
                    clipped.append(f"{width}px: user menu extends {overhang:.0f}px past the edge")

            assert probed >= len(NAVBAR_SWEEP_WIDTHS) - 1, (
                f"Only {probed} of {len(NAVBAR_SWEEP_WIDTHS)} widths produced a "
                f"measurable user menu. The selector or the mobile breakpoint "
                f"changed, and this test is no longer measuring the desktop navbar."
            )
            assert not clipped, "Navbar overflows the viewport:\n  " + "\n  ".join(clipped)
        finally:
            page.close()
            context.close()


class TestSearchResponsive:
    """The search page renders its input at every viewport."""

    def test_search_input_renders(self, sized_page, base_url: str):
        sized_page.goto(f"{base_url}/search")
        sized_page.wait_for_selector(".search-page", timeout=15000)
        expect(sized_page.locator(".search-input")).to_be_visible()
