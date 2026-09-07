"""End-to-end coverage for what a non-owner VIEWER sees on a shared file's detail page
(issues #585, #588).

Setup mirrors ``test_collection_sharing.py``: an owned, ephemeral file
(``owned_media_factory``) is added to a collection shared ``viewer`` with a real second
account (``second_user`` / ``shared_collection_factory``, both in ``conftest.py``) — the one
path ``PermissionService.get_file_permission`` resolves a non-owner's permission through.

Each assertion below carries a FALSIFIABILITY CONTROL in the same test: the file's owner,
looking at the identical file, must see the affordance the viewer does not. An
absence-only assertion cannot distinguish "correctly hidden" from "broken rendering" — the
repo's own E2E conftest docs call this out explicitly (``split_store_modules``'s docstring).

**Dev-data safety.** The file is created and destroyed by ``owned_media_factory`` (issue
#541 — never touch an ambient dev-library recording); the collection and the second account
are torn down by their own fixtures regardless of outcome.
"""

from typing import cast

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e]


@pytest.fixture
def shared_owned_file(admin_token: str, owned_media_factory, shared_collection_factory) -> dict:
    """An owned, ephemeral, completed file — in a collection shared ``viewer`` with second_user."""
    media = cast(dict, owned_media_factory(admin_token))
    shared_collection_factory(member_file_uuids=(media["uuid"],), permission="viewer")
    return media


class TestRedactionControlsHiddenFromViewer:
    """#585: a viewer must not see the redaction show-original/rescan footer."""

    def test_viewer_does_not_see_the_redaction_controls_on_a_shared_file(
        self, second_user_page: Page, gallery_page: Page, base_url: str, shared_owned_file: dict
    ) -> None:
        file_uuid = shared_owned_file["uuid"]

        second_user_page.goto(f"{base_url}/files/{file_uuid}")
        second_user_page.wait_for_load_state("networkidle")

        # Proves the page actually loaded the file as a non-owner (not a 403 empty shell) —
        # without this, the redaction-footer absence below would be indistinguishable from
        # the page having failed to load at all.
        expect(second_user_page.locator(".shared-chip")).to_contain_text("Shared", timeout=15000)
        expect(second_user_page.locator(".redaction-footer")).to_have_count(0)

        # Falsifiability control: the OWNER, on the identical file, does see the footer.
        gallery_page.goto(f"{base_url}/files/{file_uuid}")
        gallery_page.wait_for_load_state("networkidle")
        expect(gallery_page.locator(".redaction-footer")).to_have_count(1, timeout=15000)


class TestReprocessAndSummaryHiddenFromViewer:
    """#588 (frontend half): a viewer sees no reprocess/generate-summary affordance."""

    def test_viewer_sees_no_reprocess_or_generate_summary_affordance(
        self, second_user_page: Page, gallery_page: Page, base_url: str, shared_owned_file: dict
    ) -> None:
        file_uuid = shared_owned_file["uuid"]

        second_user_page.goto(f"{base_url}/files/{file_uuid}")
        second_user_page.wait_for_load_state("networkidle")

        # Non-vacuity: the viewer sees SOME action button, so the absences below are a real
        # permission gate rather than every action button failing to render.
        expect(second_user_page.locator(".view-transcript-btn")).to_be_visible(timeout=15000)
        expect(second_user_page.locator(".reprocess-button-header")).to_have_count(0)
        expect(second_user_page.locator(".generate-summary-btn")).to_have_count(0)

        # Falsifiability control: the OWNER sees the reprocess affordance on the same file
        # (a completed file with canEdit=True renders it — see FileActionButtons.svelte).
        gallery_page.goto(f"{base_url}/files/{file_uuid}")
        gallery_page.wait_for_load_state("networkidle")
        expect(gallery_page.locator(".reprocess-button-header")).to_be_visible(timeout=15000)
