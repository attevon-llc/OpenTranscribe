"""E2E: the About dialog and its stale-build banner (#1273, #1335).

The banner compares the bundle running in the tab with the server answering ``/api/version``.
A real mismatch cannot be produced on a healthy stack, so the version endpoint is stubbed
with ``page.route``; the unstubbed dialog and a stub that MATCHES the running bundle are the
negative controls that prove the banner is driven by the comparison and not rendered always.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect
from privacy_ui import open_about
from timeouts import APP_SHELL_READY_MS

pytestmark = pytest.mark.settings

BANNER = ".about-content .version-mismatch"
BANNER_TEXT = "doesn't match the server"


@pytest.fixture
def home_page(browser: Any, shared_auth_state: str, base_url: str) -> Any:
    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    page = context.new_page()
    page.goto(base_url)
    page.wait_for_selector(".gallery-action-buttons", timeout=APP_SHELL_READY_MS)
    yield page
    page.close()
    context.close()


def _running_build(page: Page) -> tuple[str, str | None]:
    """(version, commit-or-None) of the bundle in this tab, read from the dialog header."""
    label = page.locator(".header-version-number").first.inner_text()
    match = re.search(r"([^\s()]+)\s*(?:\(([0-9a-f]{7,40})\))?\s*$", label.strip(), re.I)
    assert match, label
    return match.group(1), match.group(2)


def _stub_version(page: Page, version: str, git_sha: str) -> None:
    page.route(
        "**/api/version",
        lambda route: route.fulfill(json={"version": version, "git_sha": git_sha}),
    )


def test_about_dialog_opens_names_both_builds_and_closes(home_page: Page) -> None:
    open_about(home_page)
    header = home_page.locator(".header-version")
    expect(header).to_contain_text("App build")
    expect(header).to_contain_text("Server", timeout=15000)
    expect(home_page.locator(BANNER)).to_have_count(0)  # the real stack matches itself

    home_page.keyboard.press("Escape")
    expect(home_page.locator("h2.modal-title", has_text="About OpenTranscribe")).to_have_count(0)


def test_stale_build_banner_appears_for_a_mismatched_server(home_page: Page) -> None:
    _stub_version(home_page, "v99.0.0", "deadbeef0000")
    open_about(home_page)

    banner = home_page.locator(BANNER)
    expect(banner).to_be_visible(timeout=15000)
    expect(banner).to_contain_text(BANNER_TEXT)
    expect(banner).to_have_attribute("role", "status")
    expect(home_page.locator(".header-version")).to_contain_text("v99.0.0")


def test_no_banner_when_the_stubbed_server_is_the_same_build(home_page: Page) -> None:
    """Control: same stub mechanism, matching identity, no banner."""
    open_about(home_page)
    version, commit = _running_build(home_page)
    # The dialog reads the server's identity once per page load, so the stub has to be in
    # place before the first open of a fresh load.
    home_page.reload()
    home_page.wait_for_selector(".gallery-action-buttons", timeout=APP_SHELL_READY_MS)
    _stub_version(home_page, version, commit or "unknown")
    open_about(home_page)
    expect(home_page.locator(".header-version")).to_contain_text("Server", timeout=15000)
    expect(home_page.locator(BANNER)).to_have_count(0)
