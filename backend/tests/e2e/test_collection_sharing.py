"""End-to-end coverage for collection sharing from the RECIPIENT's browser (issue #583).

Every other collection-management E2E test (``test_collection_management.py``) drives the
OWNER's browser only. `PermissionService.get_file_permission`
(``backend/app/services/permission_service.py:106-146``) resolves a non-owner's permission
**only** through "file is in a collection shared with the user" — there is no per-file share
endpoint — so a second, real, non-admin account is the one setup path that reaches the
recipient-side rendering at all. That account and the sharing sequence live in
``conftest.py``'s ``second_user`` / ``second_user_page`` / ``shared_collection_factory``
fixtures.

**#583 itself**: a viewer must never see the share/edit/delete controls that only the owner
may use — even though the backend already 403s those calls
(``_require_collection_owner``), a control that renders and then fails is a worse UX bug than
one that fails outright, and it is also a discoverable admin-surface: a viewer who can find the
delete button is a viewer who will eventually click it.

**Dev-data safety.** Every collection here carries the ``e2e-shared-`` prefix
(``shared_collection_factory``) and every account carries the ``share-e2e-`` prefix
(``second_user``); both fixtures delete what they create in a ``finally``, independent of test
outcome.
"""

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.collections]


def _open_manager(page: Page) -> None:
    """Open the collections manager from the gallery, with nothing selected."""
    page.keyboard.press("Escape")
    page.click(".collections-btn")
    page.wait_for_selector(".collections-panel", timeout=30000)


class TestRecipientSeesSharedCollection:
    """A collection shared with a user renders under "Shared with Me", not "My Collections"."""

    def test_recipient_sees_shared_collection_with_viewer_badge(
        self, second_user_page: Page, shared_collection_factory
    ) -> None:
        shared = shared_collection_factory(permission="viewer")

        _open_manager(second_user_page)

        shared_section = second_user_page.locator(".section-label.shared-label")
        expect(shared_section).to_be_visible(timeout=10000)
        expect(shared_section).to_have_text("Shared with Me")

        card = second_user_page.locator(".collection-card.shared-card").filter(
            has_text=shared["name"]
        )
        expect(card).to_be_visible(timeout=10000)
        expect(card.locator(".badge.shared-permission")).to_have_text("Viewer")


class TestViewerHasNoOwnerControls:
    """The #583 assertion: a viewer never sees share/edit/delete on a shared collection."""

    def test_viewer_sees_no_edit_delete_or_share_controls_on_a_shared_collection(
        self, second_user_page: Page, gallery_page: Page, shared_collection_factory
    ) -> None:
        shared = shared_collection_factory(permission="viewer")

        # Recipient side: the shared card renders with zero owner-only controls.
        _open_manager(second_user_page)
        card = second_user_page.locator(".collection-card.shared-card").filter(
            has_text=shared["name"]
        )
        expect(card).to_be_visible(timeout=10000)
        expect(card.locator(".share-button")).to_have_count(0)
        expect(card.locator(".edit-button")).to_have_count(0)
        expect(card.locator(".delete-config-button")).to_have_count(0)

        # Falsifiability control, in the SAME test: the identical collection, viewed by its
        # OWNER under "My Collections", DOES render all three controls. Without this half an
        # absence-only assertion cannot distinguish "correctly hidden" from "broken rendering".
        _open_manager(gallery_page)
        owner_card = gallery_page.locator(".collection-card").filter(has_text=shared["name"])
        expect(owner_card).to_be_visible(timeout=10000)
        expect(owner_card.locator(".share-button")).to_have_count(1)
        expect(owner_card.locator(".edit-button")).to_have_count(1)
        expect(owner_card.locator(".delete-config-button")).to_have_count(1)


class TestOwnerCanManageShares:
    """The owner's share modal lists the recipient and can revoke/change their access."""

    def test_owner_can_manage_shares_but_recipient_sees_a_static_permission_label(
        self, gallery_page: Page, second_user: dict[str, str], shared_collection_factory
    ) -> None:
        shared = shared_collection_factory(permission="viewer")

        _open_manager(gallery_page)
        owner_card = gallery_page.locator(".collection-card").filter(has_text=shared["name"])
        expect(owner_card).to_be_visible(timeout=10000)
        owner_card.locator(".share-button").click()

        dialog = gallery_page.get_by_role("dialog", name="Share Collection")
        expect(dialog).to_be_visible(timeout=10000)

        share_row = dialog.locator(".share-row").filter(has_text=second_user["email"])
        expect(share_row).to_be_visible(timeout=10000)
        expect(share_row.locator(".revoke-btn")).to_be_visible()
        expect(share_row.locator("select")).to_be_visible()
