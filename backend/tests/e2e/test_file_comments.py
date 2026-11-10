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
comments it adds through the UI (one timed, one general) and then checks through the API that
nothing is left.
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


def _post_through_the_form(page: Page, file_uuid: str, text: str) -> dict:
    """Fill the composer, press Add, and return the comment the server saved."""
    form = page.locator(".comment-form")
    form.locator("textarea").fill(text)
    with page.expect_response(
        lambda r: r.request.method == "POST" and f"/comments/files/{file_uuid}/comments" in r.url
    ) as created:
        form.locator("button.submit-button").click()
    assert created.value.status == 200, created.value.text()[:300]
    return cast(dict, created.value.json())


def _delete_through_the_list(page: Page, text: str) -> None:
    item = page.locator(".comment-item", has_text=text)
    item.locator("button.delete-button").click()
    with page.expect_response(
        lambda r: r.request.method == "DELETE" and "/comments/" in r.url
    ) as deleted:
        page.locator('[data-testid="confirm-dialog-confirm"]').click()
    assert deleted.value.status == 204, deleted.value.text()[:300]
    expect(page.locator(".comment-item", has_text=text)).to_have_count(0, timeout=10000)


def _add_then_delete_through_the_form(page: Page, base_url: str, file_uuid: str) -> list[str]:
    """Add one timed and one general comment with the form, check both, delete both.

    Returns the two texts so the caller can confirm through the API that nothing is left.
    """
    suffix = uuid.uuid4().hex[:8]
    timed_text = f"e2e timed comment {suffix}"
    general_text = f"e2e general comment {suffix}"
    page.goto(f"{base_url}/files/{file_uuid}")
    page.wait_for_load_state("networkidle")

    form = page.locator(".comment-form")
    expect(form).to_be_visible(timeout=15000)
    submit = form.locator("button.submit-button")
    toggle = form.locator(".timestamp-toggle input[type=checkbox]")

    # Add is never a dead button: with no text it stays enabled and says what is missing.
    expect(submit).to_be_enabled()
    submit.click()
    expect(form.locator(".form-error")).to_be_visible()

    # Timed by default, at the player's time.
    expect(toggle).to_be_checked()
    timed = _post_through_the_form(page, file_uuid, timed_text)
    assert isinstance(timed["timestamp"], int | float), timed
    timed_item = page.locator(".comment-item", has_text=timed_text)
    expect(timed_item).to_have_count(1, timeout=10000)
    expect(timed_item.locator("button.timestamp-link")).to_be_visible()
    expect(timed_item.locator(".general-label")).to_have_count(0)

    # Unticking Timestamp makes a general comment about the whole file.
    toggle.uncheck()
    expect(form.locator(".kind-summary.general")).to_be_visible()
    general = _post_through_the_form(page, file_uuid, general_text)
    assert general["timestamp"] is None, general
    general_item = page.locator(".comment-item", has_text=general_text)
    expect(general_item).to_have_count(1, timeout=10000)
    expect(general_item.locator(".general-label")).to_be_visible()
    expect(general_item.locator("button.timestamp-link")).to_have_count(0)
    # General comments are listed first.
    expect(page.locator(".comment-item").first).to_contain_text(general_text)

    _delete_through_the_list(page, timed_text)
    _delete_through_the_list(page, general_text)
    return [timed_text, general_text]


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

        texts = _add_then_delete_through_the_form(second_user_page, base_url, file_uuid)

        assert all(c["text"] not in texts for c in _comments(backend_url, token, file_uuid))

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

        texts = _add_then_delete_through_the_form(second_user_page, base_url, file_uuid)

        # Read back as the owner: the viewer's comments are gone for everyone, not just hidden.
        assert all(c["text"] not in texts for c in _comments(backend_url, admin_token, file_uuid))
