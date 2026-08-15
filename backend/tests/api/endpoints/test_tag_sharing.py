"""Tests for tag sharing (v386): ``/api/tags/{tag_uuid}/shares``.

Mirrors collection sharing (see ``test_media_collections.py``'s share tests) but
a tag share grants vocabulary, not a permission level — there is no
``permission`` field, and revoking never touches the tag itself or any
``FileTag`` association (``app/services/tag_sharing.py``'s module docstring).

Also covers the audit events wired up for issue #443's smaller half:
RESOURCE_SHARE / RESOURCE_UNSHARE previously did not exist on
``AuditEventType`` and nothing was emitted for a tag grant or its revocation.
Before this file, no test exercised ``app/api/endpoints/tags/sharing.py`` or
``app/services/tag_sharing.py`` at all.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.media import Tag
from app.models.sharing import TagShare


def _make_tag(db_session, owner, *, name: str | None = None) -> Tag:
    name = name or f"tag-{uuid.uuid4().hex[:8]}"
    tag = Tag(name=name, user_id=owner.id, source="manual", normalized_name=name.lower())
    db_session.add(tag)
    db_session.commit()
    db_session.refresh(tag)
    return tag


def _make_group(db_session, owner) -> UserGroup:
    group = UserGroup(owner_id=owner.id, name=f"grp-{uuid.uuid4().hex[:8]}")
    db_session.add(group)
    db_session.flush()
    db_session.add(UserGroupMember(group_id=group.id, user_id=owner.id, role="owner"))
    db_session.commit()
    db_session.refresh(group)
    return group


def _make_tag_share(db_session, tag, sharer, *, target_user=None, target_group=None) -> TagShare:
    share = TagShare(
        tag_id=tag.id,
        shared_by_id=sharer.id,
        target_type="user" if target_user else "group",
        target_user_id=target_user.id if target_user else None,
        target_group_id=target_group.id if target_group else None,
    )
    db_session.add(share)
    db_session.commit()
    db_session.refresh(share)
    return share


# ---------------------------------------------------------------------------
# GET /api/tags/{tag_uuid}/shares
# ---------------------------------------------------------------------------


def test_list_shares_owner(client, user_token_headers, normal_user, other_user, db_session):
    tag = _make_tag(db_session, normal_user)
    _make_tag_share(db_session, tag, normal_user, target_user=other_user)
    response = client.get(f"/api/tags/{tag.uuid}/shares", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert len(body) == 1
    assert body[0]["target_type"] == "user"


def test_list_shares_non_owner_404(client, other_user_auth_headers, normal_user, db_session):
    """The writable gate answers 404, not 403, for a tag the caller cannot
    write to -- so probing cannot be used to enumerate another owner's tags
    (see backend/app/api/CLAUDE.md's tag-plane scoping note)."""
    tag = _make_tag(db_session, normal_user)
    response = client.get(f"/api/tags/{tag.uuid}/shares", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# POST /api/tags/{tag_uuid}/shares
# ---------------------------------------------------------------------------


def test_create_share_user_target(client, user_token_headers, normal_user, other_user, db_session):
    tag = _make_tag(db_session, normal_user)
    response = client.post(
        f"/api/tags/{tag.uuid}/shares",
        headers=user_token_headers,
        json={"target_user_uuid": str(other_user.uuid)},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["target_type"] == "user"


def test_create_share_group_target(client, user_token_headers, normal_user, db_session):
    tag = _make_tag(db_session, normal_user)
    group = _make_group(db_session, normal_user)
    response = client.post(
        f"/api/tags/{tag.uuid}/shares",
        headers=user_token_headers,
        json={"target_group_uuid": str(group.uuid)},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["target_type"] == "group"


def test_create_share_with_owner_400(client, user_token_headers, normal_user, db_session):
    """Sharing a tag back to its own owner is a no-op TagShareError, not a grant."""
    tag = _make_tag(db_session, normal_user)
    response = client.post(
        f"/api/tags/{tag.uuid}/shares",
        headers=user_token_headers,
        json={"target_user_uuid": str(normal_user.uuid)},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_create_share_not_owner_404(
    client, other_user_auth_headers, normal_user, other_user, db_session
):
    """A non-owner cannot share someone else's tag -- the writable gate 404s it."""
    tag = _make_tag(db_session, normal_user)
    response = client.post(
        f"/api/tags/{tag.uuid}/shares",
        headers=other_user_auth_headers,
        json={"target_user_uuid": str(other_user.uuid)},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# DELETE /api/tags/{tag_uuid}/shares/{share_uuid}
# ---------------------------------------------------------------------------


def test_revoke_share(client, user_token_headers, normal_user, other_user, db_session):
    tag = _make_tag(db_session, normal_user)
    share = _make_tag_share(db_session, tag, normal_user, target_user=other_user)
    response = client.delete(
        f"/api/tags/{tag.uuid}/shares/{share.uuid}", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_revoke_share_not_found_404(client, user_token_headers, normal_user, db_session):
    tag = _make_tag(db_session, normal_user)
    response = client.delete(
        f"/api/tags/{tag.uuid}/shares/{uuid.uuid4()}", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Audit events (issue #443's smaller half)
# ---------------------------------------------------------------------------


def test_create_share_emits_resource_share_audit_event(
    client, user_token_headers, normal_user, other_user, db_session, monkeypatch
):
    from app.api.endpoints.tags import sharing as sharing_module

    events = []
    monkeypatch.setattr(sharing_module.audit_logger, "log", lambda **kw: events.append(kw))

    tag = _make_tag(db_session, normal_user)
    response = client.post(
        f"/api/tags/{tag.uuid}/shares",
        headers=user_token_headers,
        json={"target_user_uuid": str(other_user.uuid)},
    )
    assert response.status_code == status.HTTP_200_OK
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == sharing_module.AuditEventType.RESOURCE_SHARE
    assert event["user_id"] == normal_user.id
    assert event["target_user_id"] == other_user.id
    assert event["details"]["resource_type"] == "tag"
    assert event["details"]["resource_uuid"] == str(tag.uuid)


def test_create_share_with_owner_400_does_not_emit_audit_event(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    """Control: a rejected share (TagShareError) must not fire the event."""
    from app.api.endpoints.tags import sharing as sharing_module

    events = []
    monkeypatch.setattr(sharing_module.audit_logger, "log", lambda **kw: events.append(kw))

    tag = _make_tag(db_session, normal_user)
    response = client.post(
        f"/api/tags/{tag.uuid}/shares",
        headers=user_token_headers,
        json={"target_user_uuid": str(normal_user.uuid)},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert events == []


def test_revoke_share_emits_resource_unshare_audit_event(
    client, user_token_headers, normal_user, other_user, db_session, monkeypatch
):
    from app.api.endpoints.tags import sharing as sharing_module

    events = []
    monkeypatch.setattr(sharing_module.audit_logger, "log", lambda **kw: events.append(kw))

    tag = _make_tag(db_session, normal_user)
    share = _make_tag_share(db_session, tag, normal_user, target_user=other_user)
    response = client.delete(
        f"/api/tags/{tag.uuid}/shares/{share.uuid}", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == sharing_module.AuditEventType.RESOURCE_UNSHARE
    assert event["user_id"] == normal_user.id
    assert event["target_user_id"] == other_user.id
    assert event["details"]["resource_type"] == "tag"
    assert event["details"]["resource_uuid"] == str(tag.uuid)


def test_revoke_share_not_found_does_not_emit_audit_event(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    from app.api.endpoints.tags import sharing as sharing_module

    events = []
    monkeypatch.setattr(sharing_module.audit_logger, "log", lambda **kw: events.append(kw))

    tag = _make_tag(db_session, normal_user)
    response = client.delete(
        f"/api/tags/{tag.uuid}/shares/{uuid.uuid4()}", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert events == []
