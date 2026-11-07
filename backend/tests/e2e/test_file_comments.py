"""E2E: a non-admin member adds and deletes a comment on the file detail page.

A member reported they could not add a comment to a media file. The API answers every
reachable cell of the permission matrix (``tests/api/test_comment_permission_matrix.py``);
this suite drives the real form in a browser, as a plain ``role="user"`` account, on:

* a file the member uploaded themselves, and
* a file another account shared with them as a ``viewer`` (commenting is collaborative, so
  a viewer may comment).

Organizations cannot be created over HTTP on a community stack (see
``test_org_privacy_panel.py``), so the organization half of the matrix is covered by the API
tests, which set the organization context in process.

**Dev-data safety.** Every file is created and deleted by ``owned_media_factory``; the shared
collection and the second account are torn down by their own fixtures. Each test deletes the
comment it adds through the UI and then checks through the API that nothing is left.
"""

from __future__ import annotations

import uuid
from typing import cast

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect
from privacy_ui import token_for

pytestmark = [pytest.mark.e2e]


def _comments(backend_url: str, token: str, file_uuid: str) -> list[dict]:
    response = requests.get(
        f"{backend_url}/api/comments/files/{file_uuid}/comments",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    assert response.status_code == 200, response.text[:300]
    return cast(list[dict], response.json())


def _add_then_delete_through_the_form(page: Page, base_url: str, file_uuid: str) -> str:
    """Add a comment with the form, see it listed, delete it; return its text."""
    text = f"e2e comment {uuid.uuid4().hex[:8]}"
    page.goto(f"{base_url}/files/{file_uuid}")
    page.wait_for_load_state("networkidle")

    form = page.locator(".comment-form")
    expect(form).to_be_visible(timeout=15000)
    form.locator("textarea").fill(text)
    submit = form.locator("button.submit-button")
    # Add stays disabled until a time is marked, and the page says so.
    expect(submit).to_be_disabled()
    expect(form.locator(".form-hint")).to_be_visible()
    form.locator("button.timestamp-button").click()
    expect(form.locator(".form-hint")).to_have_count(0)
    expect(submit).to_be_enabled()

    with page.expect_response(
        lambda r: r.request.method == "POST" and f"/comments/files/{file_uuid}/comments" in r.url
    ) as created:
        submit.click()
    assert created.value.status == 200, created.value.text()[:300]

    item = page.locator(".comment-item", has_text=text)
    expect(item).to_have_count(1, timeout=10000)

    item.locator("button.delete-button").click()
    with page.expect_response(
        lambda r: r.request.method == "DELETE" and "/comments/" in r.url
    ) as deleted:
        page.locator("button.modal-delete-button").click()
    assert deleted.value.status == 204, deleted.value.text()[:300]
    expect(page.locator(".comment-item", has_text=text)).to_have_count(0, timeout=10000)
    return text


class TestMemberCommentsOnTheFileDetailPage:
    def test_member_adds_and_deletes_a_comment_on_their_own_file(
        self,
        second_user_page: Page,
        second_user: dict[str, str],
        owned_media_factory,
        base_url: str,
        backend_url: str,
    ) -> None:
        token = token_for(backend_url, second_user["email"], second_user["password"])
        media = cast(dict, owned_media_factory(token))
        file_uuid = str(media["uuid"])

        text = _add_then_delete_through_the_form(second_user_page, base_url, file_uuid)

        assert all(c["text"] != text for c in _comments(backend_url, token, file_uuid))

    def test_viewer_adds_and_deletes_a_comment_on_a_shared_file(
        self,
        second_user_page: Page,
        second_user: dict[str, str],
        admin_token: str,
        owned_media_factory,
        shared_collection_factory,
        base_url: str,
        backend_url: str,
    ) -> None:
        media = cast(dict, owned_media_factory(admin_token))
        file_uuid = str(media["uuid"])
        shared_collection_factory(member_file_uuids=(file_uuid,), permission="viewer")

        text = _add_then_delete_through_the_form(second_user_page, base_url, file_uuid)

        # Read back as the owner: the viewer's comment is gone for everyone, not just hidden.
        assert all(c["text"] != text for c in _comments(backend_url, admin_token, file_uuid))
