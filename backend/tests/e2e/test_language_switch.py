"""Switching the UI language must render the language you picked — not the last one.

REGRESSION (reported 2026-09-13, driving the v0.5.0 UI by hand): pick a language and
it applies; pick a second and the UI only partly changes; pick a third and the UI
shows the SECOND. The rendered language trailed the selection by exactly one change.

Cause, in `frontend/src/stores/locale.ts`:

    set: (newLocale) => {
      set(newLocale);                  // store updates SYNCHRONOUSLY
      void applyLanguage(newLocale);   // i18next.changeLanguage is ASYNC
    }
    i18next.on('languageChanged', (lng) => update(() => lng));

`t` is `derived(locale, ...)`, so every `$t(...)` recomputed the instant the store
changed — while i18next was still serving the OLD strings, because `applyLanguage`
had only been kicked off. When it resolved, the `languageChanged` handler wrote a
value the store ALREADY HELD, and svelte's `writable` uses `safe_not_equal`, which
does not notify for an unchanged primitive. No subscriber ran again, so the UI kept
rendering whatever i18next had at the previous recompute.

Fixed by an `i18nGeneration` counter bumped on `languageChanged`, which `t` also
derives from — so the re-render does not depend on the locale VALUE having changed.

⚠️ Why this test asserts on RENDERED TEXT rather than on `document.documentElement.lang`
or the `<select>` value: both of those are written synchronously by `locale.set()` and
were CORRECT throughout the bug. A test that checked them would have passed against the
broken build while the UI was visibly stale. The only honest signal is a translated
string the user can read.

Unit-level coverage of the same defect lives in `frontend/src/stores/locale.test.ts`;
this file is the end-to-end half, because the failure is a re-render that only a real
browser performs.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.settings

#: Three languages, switched in order, none of them the `en` we start from.
#: Deliberately not `ar`: it is RTL and its own concern (see test_a11y).
LANGUAGES_TO_CYCLE = ["fr", "de", "es"]

#: The Profile section holds `LanguageSettings.svelte`; the selector is its `<select>`.
LANGUAGE_SELECT = "#ui-language"
LANGUAGE_LABEL = "label[for=ui-language]"

#: The key whose rendered value this file asserts on.
LABEL_KEY = "settings.language.selectLabel"

#: The navbar dropdown entry that opens Settings. Needed per-language: once the UI has
#: switched, "Settings" is "Einstellungen", and a helper that matched only the English
#: text could not reach the modal again — which is a property worth asserting anyway.
NAV_SETTINGS_KEY = "nav.settings"

_LOCALES_DIR = Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "i18n" / "locales"


def translation(code: str, key: str) -> str:
    """The exact string the shipped locale file says `code` renders for `key`."""
    data = json.loads((_LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"))
    # The locale files are FLAT: dot-notation is the literal key, not a nested path.
    return str(data[key])


def expected_label(code: str) -> str:
    """The translation the UI MUST render for `code`, read from the shipped locale file.

    Read rather than hardcoded, for two reasons. A copy pasted in here would rot the
    first time someone rewords the string, and — more importantly — asserting against
    the app's own source of truth is what turns "the text changed" into "the text is
    THIS language". The bug this file exists for produced text that changed on every
    switch while being the wrong language every time, so distinctness alone would not
    have caught it.
    """
    return translation(code, LABEL_KEY)


def _open_settings_modal(page: Page, lang: str = "en") -> None:
    """Open the Settings modal via the Navbar user dropdown -> Settings.

    Same path a user takes, and the same one `test_settings_modal.py` uses — kept
    consistent rather than reaching into the store, so a broken dropdown fails here too.
    """
    user_button = page.locator(".user-button")
    expect(user_button).to_be_visible(timeout=15000)
    user_button.click()

    settings_item = page.locator(
        ".dropdown-menu .dropdown-item", has_text=translation(lang, NAV_SETTINGS_KEY)
    )
    expect(settings_item.first).to_be_visible(timeout=5000)
    settings_item.first.click()

    expect(page.locator(".settings-modal")).to_be_visible(timeout=10000)


def _open_language_settings(page: Page, lang: str = "en") -> None:
    """Navigate to the section that renders the language selector, in `lang`."""
    _open_settings_modal(page, lang)
    nav_item = page.locator(
        ".settings-sidebar .nav-item", has_text=translation(lang, "settings.profile.title")
    )
    expect(nav_item.first).to_be_visible(timeout=8000)
    nav_item.first.click()
    expect(page.locator(LANGUAGE_SELECT)).to_be_visible(timeout=10000)


@pytest.fixture
def english_ui(gallery_page: Page) -> Iterator[Page]:
    """Start every run from a known language, and restore it afterwards.

    E2E must not persist changes to dev state. The locale lives in `localStorage`,
    which is per-context, but the fixture resets it explicitly so a failure midway
    cannot leave the next test looking at a German UI.
    """
    gallery_page.evaluate("localStorage.setItem('locale', 'en')")
    gallery_page.reload(wait_until="domcontentloaded")
    gallery_page.wait_for_selector(".user-button", timeout=30000)
    yield gallery_page
    gallery_page.evaluate("localStorage.setItem('locale', 'en')")


class TestLanguageSwitching:
    def test_the_ui_renders_the_language_that_was_selected(self, english_ui: Page) -> None:
        """THE regression, and the actual requirement: selected language == rendered language.

        Three switches in a row. After each one the label must be that language's real
        translation — not merely different from the last, which is all the off-by-one
        bug violated. Asserting the exact expected string is what makes this a test of
        correctness rather than of change.
        """
        page = english_ui
        _open_language_settings(page)

        # Baseline: we start in English, and the label proves it.
        expect(page.locator(LANGUAGE_LABEL)).to_have_text(expected_label("en"), timeout=15000)

        for code in LANGUAGES_TO_CYCLE:
            page.select_option(LANGUAGE_SELECT, code)

            # The locale chunk is fetched before i18next switches, so the translated
            # text arrives asynchronously — poll for the expected value rather than
            # sleeping a guessed interval.
            expect(page.locator(LANGUAGE_LABEL)).to_have_text(expected_label(code), timeout=15000)
            expect(page.locator(LANGUAGE_SELECT)).to_have_value(code, timeout=10000)

    def test_the_selection_is_persisted_and_survives_a_reload(self, english_ui: Page) -> None:
        """A switch must outlive the page, not just repaint it.

        Control for the test above: proves the switch really reached the store and
        localStorage, so a passing render test cannot be explained by a transient repaint.
        """
        page = english_ui
        _open_language_settings(page)

        page.select_option(LANGUAGE_SELECT, "de")
        expect(page.locator(LANGUAGE_LABEL)).to_have_text(expected_label("de"), timeout=15000)

        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector(".user-button", timeout=30000)
        assert page.evaluate("localStorage.getItem('locale')") == "de"

        _open_language_settings(page, "de")
        expect(page.locator(LANGUAGE_SELECT)).to_have_value("de", timeout=10000)
        expect(page.locator(LANGUAGE_LABEL)).to_have_text(expected_label("de"), timeout=15000)

    def test_the_expected_labels_are_actually_distinct(self) -> None:
        """GUARD THE GUARD: the fixture data must be able to tell the languages apart.

        If `settings.language.selectLabel` were identical across these locales, every
        assertion above would hold no matter which language rendered. No browser needed
        — this is about the fixture, and it fails fast if a future translation pass
        makes two of them collide.
        """
        labels = {code: expected_label(code) for code in ["en", *LANGUAGES_TO_CYCLE]}
        assert len(set(labels.values())) == len(labels), (
            f"two locales translate {LABEL_KEY} identically, so this suite could not "
            f"distinguish them: {labels}"
        )
