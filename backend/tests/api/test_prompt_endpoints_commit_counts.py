"""The prompt endpoints commit exactly where they did before their queries moved to a service."""

from __future__ import annotations

import uuid

import pytest

from app import models
from tests.commit_counter import endpoint_commits


def _prompt(db_session, owner) -> models.SummaryPrompt:
    p = models.SummaryPrompt(
        user_id=owner.id,
        name=f"p-{uuid.uuid4().hex[:8]}",
        prompt_text="Summarize.",
        content_type="general",
        is_system_default=False,
        is_active=True,
        is_shared=False,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.mark.parametrize(
    "path",
    [
        "/api/prompts",
        "/api/prompts/by-content-type/general",
        "/api/prompts/active/current",
        "/api/prompts/shared/library",
    ],
)
def test_read_endpoints_do_not_commit(client, user_token_headers, db_session, path):
    assert endpoint_commits(client, db_session, "GET", path, headers=user_token_headers) == 0


def test_single_prompt_read_does_not_commit(client, user_token_headers, normal_user, db_session):
    mine = _prompt(db_session, normal_user)
    url = f"/api/prompts/{mine.uuid}"
    assert endpoint_commits(client, db_session, "GET", url, headers=user_token_headers) == 0


def test_create_and_delete_commit_once_each(client, user_token_headers, db_session):
    body = {"name": "n", "prompt_text": "t", "content_type": "general"}
    assert (
        endpoint_commits(
            client, db_session, "POST", "/api/prompts", json=body, headers=user_token_headers
        )
        == 1
    )
    listing = client.get("/api/prompts", headers=user_token_headers).json()["prompts"]
    created = next(p for p in listing if p["name"] == "n")
    url = f"/api/prompts/{created['uuid']}"
    assert endpoint_commits(client, db_session, "DELETE", url, headers=user_token_headers) == 1


def test_clone_set_active_and_share_commit_once_each(
    client, user_token_headers, normal_user, db_session
):
    mine = _prompt(db_session, normal_user)
    headers = user_token_headers
    clone_url = f"/api/prompts/{mine.uuid}/clone"
    assert endpoint_commits(client, db_session, "POST", clone_url, headers=headers) == 1
    select = {"prompt_id": str(mine.uuid)}
    assert (
        endpoint_commits(
            client, db_session, "POST", "/api/prompts/active/set", json=select, headers=headers
        )
        == 1
    )
    share_url = f"/api/prompts/shared/{mine.uuid}/toggle"
    assert (
        endpoint_commits(
            client, db_session, "POST", share_url, json={"is_shared": False}, headers=headers
        )
        == 1
    )
