"""Functional characterization tests for the summary-prompt endpoints.

Covers the CRUD / listing / active-prompt surface of ``prompts.py``
(``/api/prompts``) that is NOT already exercised by
``tests/api/endpoints/test_sharing.py`` (which owns share-toggle, clone,
shared-library, tags, attribution, usage-count) or
``tests/api/test_ownership_contracts.py`` (which pins the 403-other_user authz
snapshots). Here we add: create + per-user limit, list filtering envelope +
pagination, by-content-type grouping, active prompt get/set, and the
system/shared/private visibility matrix for the single-prompt GET.

All rows are created on the savepoint-isolated ``db_session`` and roll back.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app import models
from app.api.endpoints.prompts import MAX_USER_PROMPTS


def _make_prompt(
    db_session,
    owner,
    *,
    is_shared: bool = False,
    is_system_default: bool = False,
    is_active: bool = True,
    content_type: str = "general",
    name: str | None = None,
) -> models.SummaryPrompt:
    p = models.SummaryPrompt(
        user_id=None if is_system_default else owner.id,
        name=name or f"prompt-{uuid.uuid4().hex[:8]}",
        prompt_text="Summarize this.",
        content_type=content_type,
        is_system_default=is_system_default,
        is_active=is_active,
        is_shared=is_shared,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


# ---------------------------------------------------------------------------
# GET /api/prompts  (list + filter + pagination envelope)
# ---------------------------------------------------------------------------


def test_list_prompts_unauthorized(client):
    response = client.get("/api/prompts")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_list_prompts_envelope(client, user_token_headers, normal_user, db_session):
    _make_prompt(db_session, normal_user)
    response = client.get("/api/prompts", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    for key in ("prompts", "total", "page", "size", "has_next", "has_prev"):
        assert key in body, f"missing key {key!r}"
    assert body["page"] == 1


def test_list_prompts_include_user_only(client, user_token_headers, normal_user, db_session):
    """include_system=false, include_shared=false → only the user's own prompts."""
    mine = _make_prompt(db_session, normal_user)
    response = client.get(
        "/api/prompts",
        headers=user_token_headers,
        params={"include_system": "false", "include_shared": "false"},
    )
    assert response.status_code == status.HTTP_200_OK
    uuids = {p["uuid"] for p in response.json()["prompts"]}
    assert str(mine.uuid) in uuids


def test_list_prompts_excludes_other_users_private(
    client, other_user_auth_headers, normal_user, db_session
):
    private = _make_prompt(db_session, normal_user, is_shared=False)
    response = client.get("/api/prompts", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_200_OK
    uuids = {p["uuid"] for p in response.json()["prompts"]}
    assert str(private.uuid) not in uuids


def test_list_prompts_limit_over_max_422(client, user_token_headers):
    response = client.get("/api/prompts", headers=user_token_headers, params={"limit": 5000})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_list_prompts_negative_skip_422(client, user_token_headers):
    response = client.get("/api/prompts", headers=user_token_headers, params={"skip": -1})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# GET /api/prompts/by-content-type/{content_type}
# ---------------------------------------------------------------------------


def test_by_content_type_groups(client, user_token_headers, normal_user, db_session):
    mine = _make_prompt(db_session, normal_user, content_type="meeting")
    response = client.get("/api/prompts/by-content-type/meeting", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["content_type"] == "meeting"
    user_uuids = {p["uuid"] for p in body["user_prompts"]}
    assert str(mine.uuid) in user_uuids


def test_by_content_type_invalid_400(client, user_token_headers):
    response = client.get("/api/prompts/by-content-type/not-a-type", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid content type" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /api/prompts  (create) + per-user cap
# ---------------------------------------------------------------------------


def test_create_prompt_happy(client, user_token_headers):
    name = f"created-{uuid.uuid4().hex[:8]}"
    response = client.post(
        "/api/prompts",
        headers=user_token_headers,
        json={
            "name": name,
            "prompt_text": "Summarize.",
            "content_type": "general",
        },
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["name"] == name
    assert body["is_system_default"] is False


def test_create_prompt_at_limit_400(client, user_token_headers, normal_user, db_session):
    """At MAX_USER_PROMPTS active user prompts, creating another is 400."""
    for _ in range(MAX_USER_PROMPTS):
        _make_prompt(db_session, normal_user)
    response = client.post(
        "/api/prompts",
        headers=user_token_headers,
        json={"name": "over-limit", "prompt_text": "x", "content_type": "general"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert (
        f"Maximum number of custom prompts reached ({MAX_USER_PROMPTS})"
        in (response.json()["detail"])
    )


def test_create_prompt_missing_text_422(client, user_token_headers):
    response = client.post("/api/prompts", headers=user_token_headers, json={"name": "no-text"})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# GET /api/prompts/{uuid}  (visibility matrix; 403 pinned in ownership contracts)
# ---------------------------------------------------------------------------


def test_get_own_prompt(client, user_token_headers, normal_user, db_session):
    p = _make_prompt(db_session, normal_user)
    response = client.get(f"/api/prompts/{p.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["uuid"] == str(p.uuid)


def test_get_system_prompt_visible_to_any(client, other_user_auth_headers, db_session):
    """System prompts are public — visible to a user who doesn't own them.

    Reads an EXISTING seeded system-default prompt (read-only) rather than
    creating one, which would collide with the ``unique_system_default_per_
    content_type`` partial index the dev DB already populates.
    """
    sys_prompt = (
        db_session.query(models.SummaryPrompt)
        .filter(models.SummaryPrompt.is_system_default.is_(True))
        .first()
    )
    if sys_prompt is None:
        import pytest

        pytest.skip("no seeded system-default prompt in this DB")
    response = client.get(f"/api/prompts/{sys_prompt.uuid}", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_200_OK


def test_get_prompt_nonexistent_404(client, user_token_headers):
    response = client.get(f"/api/prompts/{uuid.uuid4()}", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Active prompt get / set
# ---------------------------------------------------------------------------


def test_get_active_prompt_returns_envelope(client, user_token_headers):
    """With no explicit selection the endpoint falls back to a system default."""
    response = client.get("/api/prompts/active/current", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert "active_prompt_id" in response.json()


def test_set_active_prompt_own(client, user_token_headers, normal_user, db_session):
    p = _make_prompt(db_session, normal_user)
    response = client.post(
        "/api/prompts/active/set",
        headers=user_token_headers,
        json={"prompt_id": str(p.uuid)},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["active_prompt_id"] == str(p.uuid)


def test_set_active_prompt_inactive_400(client, user_token_headers, normal_user, db_session):
    p = _make_prompt(db_session, normal_user, is_active=False)
    response = client.post(
        "/api/prompts/active/set",
        headers=user_token_headers,
        json={"prompt_id": str(p.uuid)},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Cannot use inactive prompt"


def test_set_active_prompt_nonexistent_404(client, user_token_headers):
    response = client.post(
        "/api/prompts/active/set",
        headers=user_token_headers,
        json={"prompt_id": str(uuid.uuid4())},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Prompt not found"


# ---------------------------------------------------------------------------
# PUT / DELETE happy paths (403 pinned in ownership contracts)
# ---------------------------------------------------------------------------


def test_update_own_prompt(client, user_token_headers, normal_user, db_session):
    p = _make_prompt(db_session, normal_user)
    response = client.put(
        f"/api/prompts/{p.uuid}",
        headers=user_token_headers,
        json={"name": "renamed-prompt"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["name"] == "renamed-prompt"


def test_delete_own_prompt(client, user_token_headers, normal_user, db_session):
    p = _make_prompt(db_session, normal_user)
    response = client.delete(f"/api/prompts/{p.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["detail"] == "Prompt deleted successfully"
