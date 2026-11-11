"""Browser-side helpers shared by the visual-regression suite and the styling A/B tool.

``test_visual_regression.py`` and ``styling_ab.py`` must photograph the app the same way
(viewport, reduced motion, font gate, settle time) or a difference between their images
means nothing. One implementation, two importers: the same reason ``_visual_diff.py``
exists. Not named ``test_*``, so pytest does not collect it.

Playwright types are imported lazily-by-annotation only (``Any``), so the unit tests can
import this module on a host without a browser installed.
"""

from __future__ import annotations

from typing import Any

from timeouts import LOGIN_FORM_READY_MS

# Fixed viewport so captures are deterministic across machines.
VIEWPORT = {"width": 1280, "height": 800}


def login(page: Any, base_url: str, email: str, password: str) -> None:
    """Log in via the form, tolerating an already-authenticated context."""
    page.goto(base_url)
    if page.locator(".user-button").count():
        page.wait_for_selector(".user-button", timeout=10000)
        return
    page.wait_for_selector("#email", timeout=LOGIN_FORM_READY_MS)
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("button[type=submit]")
    page.wait_for_selector(".user-button", timeout=30000)


def make_context(browser: Any, theme: str, locale: str | None = None) -> Any:
    """Build a browser context pinned to a viewport, a forced theme and optionally a locale.

    Theme (and locale) are forced via localStorage in an init script BEFORE first paint
    (``static/theme.js`` and ``stores/locale.ts`` read them on load). ``locale=None`` leaves
    the app's own default, which is what the visual-regression baselines were captured with.
    """
    context = browser.new_context(
        viewport=VIEWPORT,
        ignore_https_errors=True,
        # Freeze animations/transitions and CSS caret so screenshots are stable.
        reduced_motion="reduce",
    )
    script = f"window.localStorage.setItem('theme', '{theme}');"
    if locale is not None:
        script += f"window.localStorage.setItem('locale', '{locale}');"
    context.add_init_script(script)
    return context


def stabilize(page: Any, *, network_idle: bool = True) -> None:
    """Quiet the page before capture: network idle, no animations, no caret.

    ``network_idle=False`` is for a screen that deliberately holds a request open (an upload
    frozen on "preparing"): that request is in flight forever, so ``networkidle`` never fires.
    """
    if network_idle:
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
    # Web fonts load lazily, and `networkidle` can fire before the first text uses a face. A
    # capture taken before Poppins lands photographs the fallback sans-serif, and that image
    # then fails against every correct render (v0.6.0: all ten baselines were recaptured
    # in the fallback face once). Wait for pending faces, then REFUSE to capture without
    # the real one, so a missing font fails loudly instead of being baked into a baseline.
    page.evaluate("document.fonts.ready.then(() => true)")
    assert page.evaluate("document.fonts.check('600 14px Poppins')"), (
        "Poppins is not loaded; a visual capture now would bake the fallback font into the "
        "baseline (is frontend/static/fonts populated by the prebuild step?)"
    )
    # Kept deliberately: a paint/layout settle before a screenshot. The comparison is a
    # pixel diff, not a locator, so there is nothing to auto-wait on (issue #431).
    page.wait_for_timeout(600)
