"""E2E: an instance administrator places and releases a legal hold through the UI.

Legal-hold plan (``docs/design/legal_hold_ux_freeze_plan.md`` §10, Lane D). In the community
edition the admin is the operator, so every hold here is an operator hold (``/api/admin/holds``):

1. Upload a file the test owns (``owned_media_factory``).
2. Select it in the gallery and place a hold from Organize > "Place legal hold"; the card shows
   the "Legal hold" badge and the computed "Legal hold" chip lists it.
3. On the file page: the held-file banner, the title edit disabled with its reason, a forced
   edit refused with ``409 FILE_UNDER_LEGAL_HOLD``, and a new comment still accepted.
4. Release the hold from Settings > Legal holds; the badge and banner are gone.

A second test covers the administrator screens (owner feedback 2026-11-08): the file page's
title area carries no Copy file ID or Place legal hold control (the ID sits in File details), a
hold is placed from Settings > Legal holds and a takedown from Settings > Quarantine by
searching for the file by name and selecting it, never by typing its ID.

**Dev-data safety.** The file is created and deleted by ``owned_media_factory`` (its comment goes
with it). The hold is released in a ``finally`` through the API if the UI release did not run,
because a held file cannot be deleted. The released hold record itself stays, by design: a hold
and its release are the audit trail, and nothing in the product deletes them. Its reason and
matter reference carry a uuid suffix so a run can be told apart.
"""

from __future__ import annotations

import re
import uuid
from typing import cast

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect
from privacy_ui import open_settings_section
from timeouts import APP_SHELL_READY_MS

pytestmark = [pytest.mark.e2e]


def _hold(backend_url: str, token: str, hold_uuid: str) -> dict:
    response = requests.get(
        f"{backend_url}/api/admin/holds/{hold_uuid}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    assert response.status_code == 200, response.text[:300]
    return cast(dict, response.json())


def _release_if_active(backend_url: str, token: str, hold_uuid: str | None) -> None:
    """Teardown: a held file cannot be deleted, so the hold must end before the file does."""
    if not hold_uuid or not _hold(backend_url, token, hold_uuid).get("active"):
        return
    response = requests.post(
        f"{backend_url}/api/admin/holds/{hold_uuid}/release",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "e2e teardown: release the test's own hold"},
        timeout=30,
    )
    assert response.status_code == 200, response.text[:300]


def _place_from_gallery(page: Page, filename: str, reason: str, matter: str) -> str:
    page.reload()
    page.wait_for_selector(".gallery-action-buttons", timeout=APP_SHELL_READY_MS)
    page.click(".select-btn")
    card = page.locator(".file-card", has_text=filename)
    expect(card).to_be_visible(timeout=15000)
    card.locator(".file-selector").click()

    page.locator(".organize-btn").click()
    page.locator('[data-testid="gallery-place-hold"]').click()
    dialog = page.locator('[data-testid="place-hold-dialog"]')
    expect(dialog).to_be_visible(timeout=10000)
    expect(page.locator('[data-testid="place-hold-target"]')).to_contain_text(filename)
    page.locator('[data-testid="place-hold-reason"]').fill(reason)
    dialog.get_by_label("Matter reference (optional)").fill(matter)
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/api/admin/holds")
    ) as placed:
        page.locator('[data-testid="place-hold-submit"]').click()
    assert placed.value.status == 201, placed.value.text()[:300]
    return str(placed.value.json()["uuid"])


def test_admin_places_and_releases_a_hold_from_the_ui(
    gallery_page: Page,
    base_url: str,
    backend_url: str,
    admin_token: str,
    owned_media_factory,
) -> None:
    page = gallery_page
    media = owned_media_factory(admin_token)
    file_uuid, filename = str(media["uuid"]), str(media["filename"])
    suffix = uuid.uuid4().hex[:8]
    reason = f"e2e legal hold {suffix}: preservation notice"
    matter = f"e2e-matter-{suffix}"
    hold_uuid: str | None = None
    headers = {"Authorization": f"Bearer {admin_token}"}

    try:
        hold_uuid = _place_from_gallery(page, filename, reason, matter)

        # Gallery: the badge on the card, and the computed "Legal hold" chip lists the file.
        card = page.locator(".file-card", has_text=filename)
        expect(card.get_by_text("Legal hold")).to_be_visible(timeout=10000)
        # The badge shares the card's one-line meta row; it once ran past the card edge and
        # was cut to "Legal ho". The whole pill must sit inside the card.
        fit = card.evaluate(
            """el => {
              const badge = el.querySelector('.meta-line .badge');
              if (!badge) return {missing: true};
              const c = el.getBoundingClientRect(), b = badge.getBoundingClientRect();
              return {missing: false, inside: b.left >= c.left && b.right <= c.right,
                      card: [c.left, c.right], badge: [b.left, b.right]};
            }"""
        )
        assert fit == {**fit, "missing": False, "inside": True}, fit
        page.reload()
        page.wait_for_selector(".gallery-action-buttons", timeout=APP_SHELL_READY_MS)
        chip = page.locator('[data-testid="held-tag-filter"] button')
        expect(chip).to_be_visible(timeout=15000)
        with page.expect_response(lambda r: "/api/files?" in r.url and "held=true" in r.url):
            chip.click()
        expect(page.locator(".file-card", has_text=filename)).to_be_visible(timeout=15000)
        chip.click()  # leave the gallery filter as the test found it

        # File page: banner, frozen title edit with its visible reason, comments still open.
        page.goto(f"{base_url}/files/{file_uuid}")
        banner = page.locator('[data-testid="held-file-banner"]')
        expect(banner).to_be_visible(timeout=15000)
        expect(banner).to_contain_text("The service operator placed it.")
        expect(banner).not_to_contain_text(reason)
        expect(page.get_by_role("button", name="Edit display name")).to_be_disabled()
        # The frozen action notes widen the buttons beside the media heading; the heading
        # once gave way and broke mid-word ("Vide" / "o"). It must stay on one line.
        heading_lines = page.locator(".video-header h4").evaluate(
            """h => { const r = document.createRange(); r.selectNodeContents(h);
                      return new Set([...r.getClientRects()].map(x => Math.round(x.top))).size; }"""
        )
        assert heading_lines == 1, heading_lines
        banner.get_by_role("button", name="What does a legal hold do?").click()
        expect(banner).to_contain_text("Still works:")

        forced = requests.put(
            f"{backend_url}/api/files/{file_uuid}",
            headers=headers,
            json={"title": f"forced edit {suffix}"},
            timeout=30,
        )
        assert forced.status_code == 409, forced.text[:300]
        assert forced.json()["detail"]["error"] == "FILE_UNDER_LEGAL_HOLD"

        expect(page.locator('[data-testid="hold-comment-hint"]')).to_be_visible()
        form = page.locator(".comment-form")
        form.locator("textarea").fill(f"e2e comment on a held file {suffix}")
        with page.expect_response(
            lambda r: (
                r.request.method == "POST" and f"/comments/files/{file_uuid}/comments" in r.url
            )
        ) as created:
            form.locator("button.submit-button").click()
        assert created.value.status == 200, created.value.text()[:300]
        item = page.locator(".comment-item", has_text=suffix)
        expect(item.locator("button.delete-button")).to_be_disabled()

        # Release from Settings > Legal holds, the only place release lives.
        open_settings_section(page, "Legal holds")
        row = page.locator(".holds-table tr", has_text=matter)
        expect(row).to_be_visible(timeout=15000)
        expect(row).to_contain_text(reason)  # the reason is shown here and nowhere else
        row.get_by_role("button", name="Release").click()
        page.locator("[role=dialog] textarea").last.fill(f"e2e matter closed {suffix}")
        with page.expect_response(
            lambda r: (
                r.request.method == "POST" and r.url.endswith(f"/admin/holds/{hold_uuid}/release")
            )
        ) as released:
            page.get_by_role("button", name="Release hold").click()
        assert released.value.status == 200, released.value.text()[:300]
        assert _hold(backend_url, admin_token, hold_uuid)["active"] is False

        page.goto(f"{base_url}/files/{file_uuid}")
        expect(page.get_by_role("button", name="Edit display name")).to_be_enabled(timeout=15000)
        expect(page.locator('[data-testid="held-file-banner"]')).to_have_count(0)
    finally:
        _release_if_active(backend_url, admin_token, hold_uuid)


def _pick_file(page: Page, field_name: str, filename: str) -> None:
    """Search the shared file picker by name and select the file (the list is portaled to body)."""
    page.get_by_role("combobox", name=field_name).fill(filename)
    option = page.get_by_role("option", name=re.compile(re.escape(filename)))
    expect(option).to_be_visible(timeout=15000)
    option.click()
    expect(page.locator('[data-testid="file-search-picker-chips"]')).to_contain_text(filename)


def test_admin_screens_hold_and_quarantine_files_found_by_name(
    gallery_page: Page,
    base_url: str,
    backend_url: str,
    admin_token: str,
    owned_media_factory,
) -> None:
    page = gallery_page
    media = owned_media_factory(admin_token)
    file_uuid, filename = str(media["uuid"]), str(media["filename"])
    suffix = uuid.uuid4().hex[:8]
    headers = {"Authorization": f"Bearer {admin_token}"}
    hold_uuid: str | None = None
    quarantined = False

    try:
        # File page: the title area shows the file only; the ID is in File details.
        page.goto(f"{base_url}/files/{file_uuid}")
        header = page.locator(".file-header")
        expect(header.locator("h1")).to_be_visible(timeout=15000)
        expect(header.get_by_role("button", name="Copy file ID")).to_have_count(0)
        expect(header.get_by_role("button", name=re.compile("legal hold", re.I))).to_have_count(0)
        expect(header).not_to_contain_text(file_uuid)
        page.get_by_role("button", name="File Details").click()
        expect(page.get_by_text(file_uuid, exact=True)).to_be_visible(timeout=10000)

        # Settings > Legal holds: search by name, select, place. No ID field anywhere.
        open_settings_section(page, "Legal holds")
        page.get_by_role("button", name="Place a hold").click()
        dialog = page.locator('[data-testid="place-hold-dialog"]')
        expect(dialog).to_be_visible(timeout=10000)
        expect(dialog.get_by_role("radio", name="Specific files")).to_be_checked()
        expect(dialog).not_to_contain_text("file ID")
        _pick_file(page, "Files to hold", filename)
        page.locator('[data-testid="place-hold-reason"]').fill(
            f"e2e hold by name {suffix}: preservation notice"
        )
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/admin/holds")
        ) as placed:
            page.locator('[data-testid="place-hold-submit"]').click()
        assert placed.value.status == 201, placed.value.text()[:300]
        hold_uuid = str(placed.value.json()["uuid"])
        held = [f["uuid"] for f in _hold(backend_url, admin_token, hold_uuid)["files"]]
        assert held == [file_uuid], held
        _release_if_active(backend_url, admin_token, hold_uuid)

        # Settings > Quarantine: the gallery's takedown form, with the same picker.
        # Still in Settings: switch sections from the sidebar.
        page.locator(".settings-sidebar .nav-item", has_text="Quarantine").first.click()
        page.locator('[data-testid="quarantine-choose-files"]').click()
        modal = page.get_by_role("dialog", name="Quarantine files")
        expect(modal).to_be_visible(timeout=10000)
        _pick_file(page, "Files to quarantine", filename)
        modal.get_by_placeholder(re.compile("DMCA notice reference")).fill(f"e2e takedown {suffix}")
        modal.locator('input[type="radio"][value="operator"]').check()
        modal.locator("#quarantine-operator-reason").fill(f"e2e operator action {suffix}")
        # No hold here: the hold path is covered above and a held file needs its own release.
        modal.get_by_role("checkbox", name=re.compile("Place a legal hold")).uncheck()
        with page.expect_response(
            lambda r: (
                r.request.method == "POST" and r.url.endswith(f"/files/{file_uuid}/quarantine")
            )
        ) as taken_down:
            modal.get_by_role("button", name="Quarantine 1 file").click()
        assert taken_down.value.status == 200, taken_down.value.text()[:300]
        quarantined = True
        expect(page.locator(".quarantine-table")).to_contain_text(filename, timeout=15000)
    finally:
        if quarantined:
            released = requests.post(
                f"{backend_url}/api/admin/files/{file_uuid}/release",
                headers=headers,
                json={"clear_legal_hold": True},
                timeout=30,
            )
            assert released.status_code == 200, released.text[:300]
        _release_if_active(backend_url, admin_token, hold_uuid)
