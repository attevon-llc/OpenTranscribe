"""
E2E: Settings > Processing pipeline (issue #1227, WP5 of docs/design/v060_pipeline_composition_plan.md).

The page is read-only, so nothing here changes a setting; there is no cancel path to take.

Gate (written reason, never silent): the page needs ``GET /api/user-settings/pipeline`` and the
SPA build that renders it. A stack started from ``master`` or a stale image has neither, and
the failure would read as a product bug. Set ``E2E_PIPELINE_VIEW=1`` when the stack under test
serves this branch (``./opentr.sh start dev`` from a checkout containing #1224/#1227).

Run:
    E2E_PIPELINE_VIEW=1 pytest backend/tests/e2e/test_settings_pipeline.py -v
    DISPLAY=:11 E2E_PIPELINE_VIEW=1 pytest backend/tests/e2e/test_settings_pipeline.py -v --headed
"""

from __future__ import annotations

import os
import re

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect
from timeouts import LOGIN_FORM_READY_MS

pytestmark = [
    pytest.mark.settings,
    pytest.mark.skipif(
        os.environ.get("E2E_PIPELINE_VIEW") != "1",
        reason=(
            "needs a stack serving the processing-pipeline view (#1224 backend + #1227 SPA); "
            "set E2E_PIPELINE_VIEW=1 when the stack runs this branch"
        ),
    ),
]

TEST_ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@example.com")
TEST_ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "password")  # noqa: S105

STEPS = ["transcription", "diarization", "speaker_id", "llm"]


def _login(page: Page, base_url: str) -> None:
    page.goto(base_url)
    if page.locator(".user-button").count():
        return
    page.wait_for_selector("#email", timeout=LOGIN_FORM_READY_MS)
    page.fill("#email", TEST_ADMIN_EMAIL)
    page.fill("#password", TEST_ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_selector(".user-button", timeout=20000)


def _open_pipeline(page: Page) -> None:
    page.locator(".user-button").click()
    # The menu entry is translated too: the ar run has no English "Settings" to match.
    page.locator(
        ".dropdown-menu .dropdown-item", has_text=re.compile("Settings|الإعدادات")
    ).first.click()
    expect(page.locator(".settings-modal")).to_be_visible(timeout=10000)
    page.locator(".settings-sidebar .nav-item").first.wait_for(timeout=10000)
    # The row is the first of the Transcription group; match it by its position, not its
    # (translated) label, so the same test serves every locale.
    group = page.locator(".settings-sidebar .sidebar-section").nth(1)
    group.locator(".nav-item").first.click()
    expect(page.locator('[data-testid="pipeline-overview"]')).to_be_visible(timeout=10000)


@pytest.fixture(scope="module")
def pipeline_available(backend_url: str) -> None:
    """Skip, with the reason, when the backend under test has no pipeline route."""
    token = requests.post(
        f"{backend_url}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    ).json()["access_token"]
    resp = requests.get(
        f"{backend_url}/api/user-settings/pipeline",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if resp.status_code == 404:
        pytest.skip("backend under test has no /api/user-settings/pipeline (older than #1224)")
    assert resp.status_code == 200, resp.text


@pytest.fixture
def page_factory(browser, base_url: str):  # type: ignore[no-untyped-def]
    contexts = []

    def make(theme: str = "light", locale: str = "en") -> Page:
        context = browser.new_context(
            viewport={"width": 1600, "height": 1200}, ignore_https_errors=True
        )
        context.add_init_script(
            f"localStorage.setItem('theme', '{theme}'); localStorage.setItem('locale', '{locale}');"
        )
        contexts.append(context)
        page: Page = context.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page._console_errors = errors  # type: ignore[attr-defined]
        _login(page, base_url)
        return page

    yield make
    for context in contexts:
        context.close()


@pytest.mark.parametrize(("theme", "locale"), [("light", "en"), ("dark", "en"), ("light", "ar")])
def test_pipeline_view_renders_four_steps(
    pipeline_available: None,
    page_factory,
    theme: str,
    locale: str,  # type: ignore[no-untyped-def]
) -> None:
    """Four step cards in processing order, each with where it runs; ar is right-to-left."""
    page = page_factory(theme, locale)
    _open_pipeline(page)

    cards = page.locator('[data-testid="pipeline-step"]')
    expect(cards).to_have_count(4)
    assert [cards.nth(i).get_attribute("data-step") for i in range(4)] == STEPS
    for i in range(4):
        expect(cards.nth(i).locator('[data-testid="pipeline-where"]')).not_to_be_empty()

    if locale == "ar":
        assert page.evaluate("document.documentElement.dir") == "rtl"
    assert not [e for e in page._console_errors if "pipeline" in e.lower()]  # type: ignore[attr-defined]


def test_change_link_opens_the_tab_that_edits_the_step(
    pipeline_available: None,
    page_factory,  # type: ignore[no-untyped-def]
) -> None:
    """Every step links to the one place that edits it; following one leaves the page."""
    page = page_factory()
    _open_pipeline(page)

    page.locator('[data-step="diarization"] [data-testid="pipeline-change"]').click()

    expect(page.locator("#diarization-source")).to_be_visible(timeout=10000)
    expect(page.locator('[data-testid="pipeline-overview"]')).to_have_count(0)
