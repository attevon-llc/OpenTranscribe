"""End-to-end coverage for support-access grants, frontend half (issue #1122).

Needs a stack running with ``TENANCY_MODE=multi`` (``docker-compose.yml`` passes
``TENANCY_MODE=${TENANCY_MODE:-auto}``; ``auto`` only latches multi once an Organization
row exists). In single-tenant mode every support-access control must be ABSENT, which the
one unmarked control test below asserts; everything else is marked ``support_access`` and
is deselected from ``run-e2e.sh``'s Phase 1 so a single-tenant stack never skips it.

Roles: ``admin@example.com`` is the support engineer (the grantee); a throwaway
``support-e2e-<uuid4>@example.com`` user is the subject whose personal workspace is
accessed. The subject owns a small uploaded file so there is something to read.

**Dev-data safety.** Every grant is revoked, the file deleted and the user deleted in
finalizers (a delete on the happy path does not count). Names carry a uuid4 suffix and the
email prefix is registered in ``scripts/cleanup-test-users.py``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from collections.abc import Iterator

import pytest
import requests
from a11y_lib import form_login_with_retry
from a11y_lib import gated_violations
from a11y_lib import run_axe
from a11y_lib import set_theme
from playwright.sync_api import Browser
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e]

SUPPORT_SUBJECT_PREFIX = "support-e2e-"
SUBJECT_PASSWORD = "Xk9!pLm2@Wq7Rn"  # noqa: S105 - throwaway account, deleted in teardown
GRANT_HEADER = "x-support-access-grant"
REASON = "Investigating a stuck transcript for the e2e run"


def _multi_tenant(backend_url: str) -> bool:
    resp = requests.get(f"{backend_url}/api/system/capabilities", timeout=30)
    return resp.status_code == 200 and resp.json().get("tenancy_mode") == "multi"


@pytest.fixture(scope="module")
def multi_tenant_stack(backend_url: str) -> None:
    """Preflight: these flows are meaningless (and the routes 404) outside multi mode."""
    if not _multi_tenant(backend_url):
        pytest.fail(
            "support_access e2e needs TENANCY_MODE=multi on the stack; this one reports "
            "another mode. Start it with TENANCY_MODE=multi or deselect -m support_access."
        )


@pytest.fixture(scope="module")
def support_subject(backend_url: str, admin_token: str) -> Iterator[dict[str, str]]:
    """A plain user whose personal workspace the support engineer asks to enter."""
    suffix = uuid.uuid4().hex[:8]
    email = f"{SUPPORT_SUBJECT_PREFIX}{suffix}@example.com"
    full_name = f"Support Subject {suffix}"
    admin = {"Authorization": f"Bearer {admin_token}"}
    created = requests.post(
        f"{backend_url}/api/admin/users",
        headers=admin,
        json={
            "email": email,
            "password": f"Zq3!{uuid.uuid4().hex[:10]}Rv9#",
            "full_name": full_name,
            "role": "user",
        },
        timeout=30,
    )
    if created.status_code not in (200, 201):
        pytest.skip(f"could not create the subject user (HTTP {created.status_code})")
    user_uuid = str(created.json()["uuid"])
    try:
        reset = requests.post(
            f"{backend_url}/api/admin/users/{user_uuid}/reset-password",
            headers=admin,
            json={"new_password": SUBJECT_PASSWORD, "force_change": False},
            timeout=30,
        )
        assert reset.status_code == 200, reset.text[:300]
        yield {
            "email": email,
            "password": SUBJECT_PASSWORD,
            "uuid": user_uuid,
            "full_name": full_name,
        }
    finally:
        requests.delete(f"{backend_url}/api/admin/users/{user_uuid}", headers=admin, timeout=30)


@pytest.fixture(scope="module")
def subject_token(backend_url: str, support_subject: dict[str, str]) -> str:
    resp = requests.post(
        f"{backend_url}/api/auth/token",
        data={"username": support_subject["email"], "password": support_subject["password"]},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    assert resp.status_code == 200, resp.text[:300]
    return str(resp.json()["access_token"])


@pytest.fixture(scope="module")
def subject_file(
    backend_url: str, subject_token: str, owned_media_factory: Callable[..., dict]
) -> dict:
    return owned_media_factory(subject_token)


@pytest.fixture
def grants(backend_url: str, admin_token: str) -> Iterator[Callable[[str], str]]:
    """Request grants against the subject; every one is revoked however the test ends."""
    created: list[str] = []
    admin = {"Authorization": f"Bearer {admin_token}"}

    def _request(subject_uuid: str, minutes: int = 15) -> str:
        resp = requests.post(
            f"{backend_url}/api/support-access/grants",
            headers=admin,
            json={
                "organization_uuid": None,
                "subject_user_uuid": subject_uuid,
                "access_level": "read",
                "reason": REASON,
                "duration_minutes": minutes,
            },
            timeout=30,
        )
        assert resp.status_code in (200, 201), resp.text[:300]
        grant_uuid = str(resp.json()["uuid"])
        created.append(grant_uuid)
        return grant_uuid

    try:
        yield _request
    finally:
        for grant_uuid in created:
            requests.post(
                f"{backend_url}/api/support-access/grants/{grant_uuid}/revoke",
                headers=admin,
                json={},
                timeout=30,
            )


def _approve(backend_url: str, subject_token: str, grant_uuid: str, minutes: int = 15) -> None:
    resp = requests.post(
        f"{backend_url}/api/users/me/support-access/{grant_uuid}/approve",
        headers={"Authorization": f"Bearer {subject_token}"},
        json={"duration_minutes": minutes},
        timeout=30,
    )
    assert resp.status_code == 200, resp.text[:300]


STAFF_NAV = re.compile(r"^\s*Support access\s*$")
REQUESTS_NAV = re.compile(r"^\s*Support access requests\s*\d*\s*$")


def _open_settings(page: Page) -> None:
    page.locator("button:has(.user-avatar)").click()
    page.locator(".dropdown-menu .dropdown-item").first.click()
    page.wait_for_selector(".settings-modal")


def _open_settings_section(page: Page, label: re.Pattern[str]) -> None:
    if page.locator(".settings-modal").count() == 0:
        _open_settings(page)
    page.locator(".settings-sidebar .nav-item", has_text=label).first.click()


def _subject_page(browser: Browser, base_url: str, subject: dict[str, str]) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1600, "height": 1000})
    page = context.new_page()
    page.goto(base_url)
    page.wait_for_selector("#email", timeout=30000)
    page.fill("#email", subject["email"])
    page.fill("#password", subject["password"])
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=30000)
    yield page
    context.close()


@pytest.fixture
def subject_ui(browser: Browser, base_url: str, support_subject: dict[str, str]) -> Iterator[Page]:
    yield from _subject_page(browser, base_url, support_subject)


def test_single_mode_hides_support_access(gallery_page: Page, backend_url: str) -> None:
    """Control: outside multi mode the UI offers no support-access entry points at all."""
    if _multi_tenant(backend_url):
        pytest.skip("stack is multi-tenant; the rows are SUPPOSED to exist here")
    _open_settings(gallery_page)
    labels = gallery_page.locator(".settings-sidebar .nav-item-label").all_inner_texts()
    assert not [label for label in labels if "support" in label.lower()], labels
    expect(gallery_page.locator("section[aria-label='Support access session']")).to_have_count(0)


@pytest.mark.support_access
def test_request_then_subject_denies(
    gallery_page: Page,
    subject_ui: Page,
    multi_tenant_stack: None,
    support_subject: dict[str, str],
    grants: Callable[[str], str],
    base_url: str,
) -> None:
    grants(support_subject["uuid"])

    subject_ui.goto(base_url)
    _open_settings_section(subject_ui, REQUESTS_NAV)
    row = subject_ui.locator(".grant-table tbody tr").first
    expect(row).to_contain_text("admin@example.com", timeout=15000)
    row.get_by_role("button", name=re.compile(r"^Deny")).click()
    subject_ui.locator(".modal-footer").get_by_role("button", name="Deny", exact=True).click()

    _open_settings_section(gallery_page, STAFF_NAV)
    mine = gallery_page.locator(".grant-table tbody tr", has_text=support_subject["full_name"])
    expect(mine.first).to_contain_text("Denied", timeout=45000)


@pytest.mark.support_access
def test_approved_session_scopes_the_ui_and_the_header(
    gallery_page: Page,
    subject_ui: Page,
    multi_tenant_stack: None,
    backend_url: str,
    base_url: str,
    support_subject: dict[str, str],
    subject_token: str,
    subject_file: dict,
    grants: Callable[[str], str],
) -> None:
    grant_uuid = grants(support_subject["uuid"])
    _approve(backend_url, subject_token, grant_uuid)

    sent: list[tuple[str, str | None]] = []
    gallery_page.on("request", lambda req: sent.append((req.url, req.headers.get(GRANT_HEADER))))

    _open_settings_section(gallery_page, STAFF_NAV)
    row = gallery_page.locator(".grant-table tbody tr", has_text=support_subject["full_name"])
    row.first.get_by_role("button", name=re.compile(r"^Start session")).click()

    banner = gallery_page.locator("section[aria-label='Support access session']")
    expect(banner).to_be_visible(timeout=15000)
    expect(banner).to_contain_text(support_subject["full_name"])

    gallery_page.goto(f"{base_url}/files/{subject_file['uuid']}")
    gallery_page.wait_for_load_state("networkidle")
    expect(banner).to_be_visible()
    # Control for the absences below: the owner, on the same file, DOES get the controls.
    subject_ui.goto(f"{base_url}/files/{subject_file['uuid']}")
    expect(subject_ui.get_by_role("button", name=re.compile(r"^Export"))).to_have_count(
        1, timeout=20000
    )
    expect(gallery_page.get_by_role("button", name=re.compile(r"Export|Download"))).to_have_count(0)
    expect(gallery_page.get_by_role("link", name=re.compile(r"Chat", re.I))).to_have_count(0)

    file_calls = [h for u, h in sent if f"/api/files/{subject_file['uuid']}" in u]
    assert file_calls, "the file detail request was never observed"
    assert all(h == grant_uuid for h in file_calls), file_calls
    for url, header in sent:
        if any(p in url for p in ("/api/auth/", "/api/support-access/", "/api/system/")):
            assert header is None, (url, header)
        if not url.startswith(base_url):
            assert header is None, (url, header)

    _open_settings_section(subject_ui, REQUESTS_NAV)
    subject_ui.get_by_role("button", name=re.compile(r"^View access log")).first.click()
    expect(subject_ui.locator("code", has_text="/api/files/{file_uuid}").first).to_be_visible(
        timeout=15000
    )
    subject_ui.keyboard.press("Escape")

    banner.get_by_role("button", name=re.compile(r"^End session")).click()
    expect(banner).to_have_count(0, timeout=15000)


@pytest.mark.support_access
def test_subject_revoking_ends_the_live_session(
    gallery_page: Page,
    subject_ui: Page,
    multi_tenant_stack: None,
    backend_url: str,
    base_url: str,
    support_subject: dict[str, str],
    subject_token: str,
    grants: Callable[[str], str],
) -> None:
    grant_uuid = grants(support_subject["uuid"])
    _approve(backend_url, subject_token, grant_uuid)

    _open_settings_section(gallery_page, STAFF_NAV)
    row = gallery_page.locator(".grant-table tbody tr", has_text=support_subject["full_name"])
    row.first.get_by_role("button", name=re.compile(r"^Start session")).click()
    banner = gallery_page.locator("section[aria-label='Support access session']")
    expect(banner).to_be_visible(timeout=15000)

    revoked = requests.post(
        f"{backend_url}/api/users/me/support-access/{grant_uuid}/revoke",
        headers={"Authorization": f"Bearer {subject_token}"},
        json={},
        timeout=30,
    )
    assert revoked.status_code == 200, revoked.text[:300]
    # The WebSocket event ends it at once; the 30 s poll / next 4xx is only the fallback.
    expect(banner).to_have_count(0, timeout=45000)


@pytest.mark.support_access
def test_break_glass_needs_ticket_and_typed_confirmation(
    gallery_page: Page,
    multi_tenant_stack: None,
    backend_url: str,
    admin_token: str,
    support_subject: dict[str, str],
) -> None:
    _open_settings_section(gallery_page, STAFF_NAV)
    gallery_page.get_by_role("button", name=re.compile(r"^Break glass")).click()
    gallery_page.get_by_role("radio", name=re.compile(r"personal workspace", re.I)).check()
    gallery_page.locator(".searchable-input").fill(support_subject["email"])
    gallery_page.locator(".searchable-option").first.click()
    gallery_page.fill("#bg-reason", "Production outage for this customer in the e2e run")
    next_button = gallery_page.locator("button[form='break-glass-form']")
    next_button.click()
    # No ticket: the form must not advance to the confirmation step.
    expect(gallery_page.locator("#bg-typed")).to_have_count(0)

    gallery_page.fill("#bg-ticket", f"INC-{uuid.uuid4().hex[:6]}")
    next_button.click()
    confirm = gallery_page.locator("button[form='break-glass-confirm']")
    expect(gallery_page.locator("#bg-typed")).to_be_visible()
    gallery_page.fill("#bg-typed", support_subject["full_name"].lower())
    expect(confirm).to_be_disabled()
    gallery_page.fill("#bg-typed", support_subject["full_name"])
    expect(confirm).to_be_enabled()
    confirm.click()
    expect(gallery_page.locator(".modal-footer .btn-primary")).to_be_visible(timeout=15000)

    # Break-glass grants are active immediately, so the finalizer path must revoke them.
    admin = {"Authorization": f"Bearer {admin_token}"}
    listing = requests.get(
        f"{backend_url}/api/support-access/grants",
        headers=admin,
        params={"scope": "mine", "limit": "50", "offset": "0", "status": "active"},
        timeout=30,
    )
    for grant in listing.json().get("items", []):
        if (grant.get("subject_user") or {}).get("uuid") == support_subject["uuid"]:
            requests.post(
                f"{backend_url}/api/support-access/grants/{grant['uuid']}/revoke",
                headers=admin,
                json={},
                timeout=30,
            )


@pytest.mark.support_access
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_support_access_panels_have_no_serious_axe_violations(
    gallery_page: Page,
    multi_tenant_stack: None,
    base_url: str,
    theme: str,
) -> None:
    form_login_with_retry(gallery_page, base_url)
    set_theme(gallery_page, theme)
    for section in (STAFF_NAV, REQUESTS_NAV):
        _open_settings_section(gallery_page, section)
        gallery_page.wait_for_load_state("networkidle")
        found = gated_violations(run_axe(gallery_page))
        assert found == [], [(v["id"], v["impact"]) for v in found]
        gallery_page.keyboard.press("Escape")
