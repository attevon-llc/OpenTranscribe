# mypy: disable-error-code="misc"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures that declare Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object. Declared once here rather than as a cast at every call site — casts
# bury the assertion, and widening a production signature to suit a test is worse.
"""
E2E Tests for Speaker Profile Sharing Feature

Lightweight API-level tests that run in-browser after a single login.
These validate the sharing fields and mutation guards without relying
on heavy endpoints.

Run with:
    pytest backend/tests/e2e/test_speaker_sharing.py -v
    DISPLAY=:11 pytest backend/tests/e2e/test_speaker_sharing.py -v --headed
"""

import os

import pytest
from conftest import FRONTEND_URL
from conftest import TEST_ADMIN_EMAIL
from conftest import TEST_ADMIN_PASSWORD
from playwright.sync_api import Browser
from playwright.sync_api import Page

SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "screenshots", "speaker_sharing")


@pytest.fixture(scope="module")
def logged_in_page(browser: Browser) -> Page:
    """Login once, navigate to speakers. Shared across all tests."""
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()

    # Login via /login page explicitly
    page.goto(f"{FRONTEND_URL}/login", timeout=15000)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_load_state("networkidle")

    # Only login if on the login page (not already authenticated)
    if "/login" in page.url and page.locator("#email").is_visible():
        page.fill("#email", TEST_ADMIN_EMAIL)
        page.fill("#password", TEST_ADMIN_PASSWORD)
        page.click("button[type=submit]")
        # `networkidle` alone resolves as soon as the login POST settles, before the
        # client-side redirect off /login lands — the assertions below then observe a
        # 401'd, still-on-/login page. Wait for the actual navigation first, matching
        # the `wait_for_url` idiom AuthHelper.login/register use in conftest.py.
        page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        page.wait_for_load_state("networkidle")

    page.screenshot(path=os.path.join(SCREENSHOT_DIR, "01-after-login.png"))

    # Go to speakers
    page.goto(f"{FRONTEND_URL}/speakers", timeout=30000)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_load_state("networkidle")
    page.screenshot(path=os.path.join(SCREENSHOT_DIR, "02-speakers-page.png"))

    yield page
    ctx.close()


def test_speakers_page_loads(logged_in_page: Page):
    """Speakers page is accessible."""
    assert "/speakers" in logged_in_page.url


def test_profiles_api_returns_200(logged_in_page: Page):
    """Profiles endpoint returns 200."""
    result = logged_in_page.evaluate("""async () => {
        const r = await fetch('/api/speaker-profiles/profiles', {credentials:'include'});
        return r.status;
    }""")
    assert result == 200


def test_profiles_have_sharing_fields(logged_in_page: Page):
    """Profile objects contain is_shared and owner_name."""
    result = logged_in_page.evaluate("""async () => {
        const r = await fetch('/api/speaker-profiles/profiles', {credentials:'include'});
        const profiles = await r.json();
        if (!Array.isArray(profiles) || profiles.length === 0) return {empty: true};
        const p = profiles[0];
        return {
            has_is_shared: 'is_shared' in p,
            has_owner_name: 'owner_name' in p,
            is_shared: p.is_shared,
            keys: Object.keys(p)
        };
    }""")
    if result.get("empty"):
        pytest.skip("No profiles exist")
    assert result["has_is_shared"], f"Missing is_shared. Keys: {result['keys']}"
    assert result["has_owner_name"], f"Missing owner_name. Keys: {result['keys']}"


def test_own_profiles_not_shared(logged_in_page: Page):
    """Admin's own profiles are is_shared=false."""
    result = logged_in_page.evaluate("""async () => {
        const r = await fetch('/api/speaker-profiles/profiles', {credentials:'include'});
        const profiles = await r.json();
        if (!Array.isArray(profiles) || profiles.length === 0) return {empty: true};
        return {
            total: profiles.length,
            own: profiles.filter(p => p.is_shared === false).length
        };
    }""")
    if result.get("empty"):
        pytest.skip("No profiles exist")
    assert result["own"] > 0, "Admin should have at least one own profile"
    logged_in_page.screenshot(path=os.path.join(SCREENSHOT_DIR, "03-profiles.png"))


def test_delete_fake_profile_not_500(logged_in_page: Page):
    """DELETE nonexistent profile returns 404, not 500."""
    status = logged_in_page.evaluate("""async () => {
        const r = await fetch(
            '/api/speaker-profiles/profiles/00000000-0000-0000-0000-000000000000',
            {method:'DELETE', credentials:'include'}
        );
        return r.status;
    }""")
    assert status in (404, 403)


def test_update_fake_profile_not_500(logged_in_page: Page):
    """PUT nonexistent profile returns 404, not 500."""
    status = logged_in_page.evaluate("""async () => {
        const r = await fetch(
            '/api/speaker-profiles/profiles/00000000-0000-0000-0000-000000000000',
            {method:'PUT', credentials:'include',
             headers:{'Content-Type':'application/json'},
             body: JSON.stringify({name:'test'})}
        );
        return r.status;
    }""")
    assert status in (404, 403, 422)


def test_assign_fake_speaker_not_500(logged_in_page: Page):
    """POST assign-profile on fake speaker returns non-500."""
    status = logged_in_page.evaluate("""async () => {
        const r = await fetch(
            '/api/speaker-profiles/speakers/00000000-0000-0000-0000-000000000000/assign-profile',
            {method:'POST', credentials:'include',
             headers:{'Content-Type':'application/json'},
             body: JSON.stringify({profile_uuid:'00000000-0000-0000-0000-000000000000'})}
        );
        return r.status;
    }""")
    assert status != 500


def test_no_raw_i18n_keys(logged_in_page: Page):
    """No untranslated i18n keys visible on page."""
    body = logged_in_page.text_content("body") or ""
    for key in [
        "speakers.profiles.shared",
        "speakers.profiles.sharedBy",
        "speakers.profiles.sharedProfile",
    ]:
        assert key not in body, f"Raw i18n key leaked: {key}"


def test_occurrences_endpoint(logged_in_page: Page):
    """Occurrences endpoint responds for first profile."""
    result = logged_in_page.evaluate("""async () => {
        const r = await fetch('/api/speaker-profiles/profiles', {credentials:'include'});
        const profiles = await r.json();
        if (!Array.isArray(profiles) || profiles.length === 0) return {skip: true};
        const occ = await fetch(
            `/api/speaker-profiles/profiles/${profiles[0].uuid}/occurrences`,
            {credentials:'include'}
        );
        return {status: occ.status};
    }""")
    if result.get("skip"):
        pytest.skip("No profiles exist")
    assert result["status"] in (200, 404)
