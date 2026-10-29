"""E2E: Arabic (right-to-left) smoke for the privacy surfaces (#1335).

Not a translation test (``npm run check:i18n`` is parity only, and these strings are only
as translated as the locale file says). What a browser can prove and a unit test cannot:
the document really flips to ``dir=rtl`` and neither Settings > Voice Attributes nor the
purge dialog spills out of its box horizontally.

The overflow probe is itself checked against a deliberately overflowing element, so a probe
that always answers "fits" cannot make these pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect
from privacy_ui import USER_VOICEPRINT_PURGE
from privacy_ui import USER_VOICEPRINT_SWITCH
from privacy_ui import purge_modal

pytestmark = pytest.mark.settings

_LOCALES = Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "i18n" / "locales"

#: Elements of `root` whose box leaves `root`'s horizontal extent, plus root's own scroll
#: overflow. Subpixel slack of 1 px for borders.
OVERFLOW_JS = """
(root) => {
  const box = root.getBoundingClientRect();
  const out = [];
  if (root.scrollWidth > root.clientWidth + 1) {
    out.push('root scrolls: ' + root.scrollWidth + ' > ' + root.clientWidth);
  }
  for (const el of root.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.left < box.left - 1 || r.right > box.right + 1) {
      out.push(el.tagName.toLowerCase() + '.' + (el.className || '').toString().slice(0, 40));
    }
  }
  return out;
}
"""


def _ar(key: str) -> str:
    data = json.loads((_LOCALES / "ar.json").read_text(encoding="utf-8"))
    return str(data[key])


@pytest.fixture
def ar_page(browser: Any, second_user_auth_state: str, base_url: str) -> Any:
    context = browser.new_context(
        storage_state=second_user_auth_state,
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
    )
    context.add_init_script("localStorage.setItem('locale', 'ar')")
    page = context.new_page()
    page.goto(base_url)
    page.wait_for_selector(".user-button", timeout=30000)
    yield page
    page.close()
    context.close()


def _open_voice_privacy_ar(page: Page) -> None:
    page.locator(".user-button").click()
    page.locator(".dropdown-menu .dropdown-item", has_text=_ar("nav.settings")).first.click()
    expect(page.locator(".settings-modal")).to_be_visible(timeout=10000)
    page.locator(
        ".settings-sidebar .nav-item", has_text=_ar("settings.speakerIdentification.title")
    ).first.click()
    page.locator("#tab-spk-attributes").click()
    expect(page.locator(USER_VOICEPRINT_SWITCH)).to_be_visible(timeout=15000)


def test_the_overflow_probe_can_fail(ar_page: Page) -> None:
    """Control: a child wider than its parent is reported."""
    result = ar_page.evaluate(
        """(js) => {
          const root = document.createElement('div');
          root.style.cssText = 'width:100px;overflow:visible;position:fixed;top:0;left:0';
          const wide = document.createElement('div');
          wide.style.cssText = 'width:400px;height:10px';
          root.appendChild(wide);
          document.body.appendChild(root);
          const found = eval(js)(root);
          root.remove();
          return found;
        }""",
        OVERFLOW_JS,
    )
    assert len(result) >= 1, "the probe reported no overflow for a 400px child in a 100px box"


def test_privacy_settings_render_rtl_without_overflow(ar_page: Page) -> None:
    _open_voice_privacy_ar(ar_page)
    assert ar_page.evaluate("document.documentElement.dir") == "rtl"
    card = ar_page.locator('[data-testid="user-privacy-card"]')
    assert (
        ar_page.evaluate("(el) => getComputedStyle(el).direction", card.element_handle()) == "rtl"
    )
    # The switch knob sits on the logical end: right-to-left puts it left of the text.
    switch_box = ar_page.locator(
        '[data-testid="privacy-card-user-voiceprints"] .toggle'
    ).bounding_box()
    title_box = ar_page.locator("#user-voiceprints-title").bounding_box()
    assert switch_box is not None and title_box is not None
    assert switch_box["x"] < title_box["x"], (switch_box, title_box)

    overflow = ar_page.evaluate(f"(el) => ({OVERFLOW_JS})(el)", card.element_handle())
    assert overflow == [], overflow
    modal_overflow = ar_page.evaluate(
        "(el) => el.scrollWidth - el.clientWidth",
        ar_page.locator(".settings-content").element_handle(),
    )
    assert modal_overflow <= 1


def test_purge_dialog_renders_rtl_without_overflow(ar_page: Page, voiceprints_off: None) -> None:
    _open_voice_privacy_ar(ar_page)
    ar_page.locator(USER_VOICEPRINT_PURGE).click()
    dialog = purge_modal(ar_page)
    expect(dialog).to_be_visible(timeout=10000)

    overflow = ar_page.evaluate(f"(el) => ({OVERFLOW_JS})(el)", dialog.element_handle())
    assert overflow == [], overflow
    # The word the person must type is Latin and stays left-to-right inside the RTL dialog.
    word_input = dialog.locator("input[type=text]")
    assert word_input.get_attribute("dir") == "ltr"
    assert (
        ar_page.evaluate("(el) => getComputedStyle(el).direction", dialog.element_handle()) == "rtl"
    )
