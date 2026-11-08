"""Two tabs whose access tokens expire together both stay signed in.

The refresh token is an httpOnly cookie every tab shares, and the server spends it once:
a refresh token that was already rotated and comes back counts as reuse and ends the
session (RFC 9700 §4.14.2, ``auth.token.reuse``). Tabs share the access cookie too, so
they expire at the same moment and each tries to refresh. The SPA serialises those
refreshes across tabs (``frontend/src/lib/auth/sessionRefresh.ts``, Web Locks); without
it the second tab presents the spent cookie and signs both tabs out.

Run (needs the dev stack, ``./opentr.sh start dev``):
    ./scripts/e2e/run-e2e.sh backend/tests/e2e/test_multi_tab_session_refresh.py
    DISPLAY=:11 pytest backend/tests/e2e/test_multi_tab_session_refresh.py -v --headed
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
import requests
from playwright.sync_api import Page

pytestmark = pytest.mark.auth

REUSE_EVENT = "auth.token.reuse"
#: Lets the audit index make the event searchable before it is queried.
AUDIT_INDEX_SETTLE_MS = 2000


def _signed_in(page: Page, base_url: str) -> bool:
    body = page.request.get(f"{base_url}/api/auth/session").json()
    return bool(body.get("authenticated"))


def test_two_tabs_restore_an_expired_session_without_refresh_reuse(
    authenticated_page: Page, base_url: str, backend_url: str, admin_token: str
):
    context = authenticated_page.context
    first = authenticated_page
    second = context.new_page()
    try:
        second.goto(base_url)
        second.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        second.wait_for_load_state("networkidle")
        since = datetime.now(UTC) - timedelta(seconds=5)

        # Both tabs' access token "expires" at once: drop the shared access cookie and
        # keep the refresh cookie, so each tab's boot finds a refreshable session.
        context.clear_cookies(name="access_token")
        assert any(cookie["name"] == "refresh_token" for cookie in context.cookies())
        assert not _signed_in(first, base_url)

        # Reload both tabs at the same instant, scheduled in-page against one clock.
        fire_at = first.evaluate("() => Date.now() + 500")
        schedule = "t => setTimeout(() => location.reload(), Math.max(0, t - Date.now()))"
        with first.expect_navigation(), second.expect_navigation():
            first.evaluate(schedule, fire_at)
            second.evaluate(schedule, fire_at)

        for page in (first, second):
            page.wait_for_load_state("networkidle")
            assert "/login" not in page.url, f"a tab was signed out: {page.url}"
        assert _signed_in(first, base_url)
        assert _signed_in(second, base_url)

        first.wait_for_timeout(AUDIT_INDEX_SETTLE_MS)
        response = requests.get(
            f"{backend_url}/api/admin/audit-logs",
            params={"event_type": REUSE_EVENT, "start_date": since.isoformat()},
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=30,
        )
        assert response.status_code == 200, response.text
        audit = response.json()
        assert "error" not in audit, audit
        assert audit["total"] == 0, audit["logs"]
    finally:
        second.close()
