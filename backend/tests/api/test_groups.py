"""Functional characterization tests for the user-groups endpoints (``groups.py``).

Covers CRUD plus membership management on ``/api/groups``:

- ``GET    /api/groups``                          (list owned + member)
- ``POST   /api/groups``                          (create; creator becomes owner)
- ``GET    /api/groups/{uuid}``                   (detail with members; member-only)
- ``PUT    /api/groups/{uuid}``                   (owner/admin)
- ``DELETE /api/groups/{uuid}``                   (owner only)
- ``POST   /api/groups/{uuid}/members``           (owner/admin add)
- ``PUT    /api/groups/{uuid}/members/{user}``    (role update)
- ``DELETE /api/groups/{uuid}/members/{user}``    (remove / leave)

The 403-other_user delete contract is already pinned in
``test_ownership_contracts.py``; these build functional coverage (happy/401/
404/422/membership matrices) around it without duplicating it. All rows are
created on the savepoint-isolated ``db_session`` and roll back at teardown.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.group import UserGroup
from app.models.group import UserGroupMember


def _make_group(db_session, owner, *, name: str | None = None) -> UserGroup:
    """Create a group owned by ``owner`` with the owner auto-enrolled as member."""
    group = UserGroup(
        owner_id=owner.id,
        name=name or f"grp-{uuid.uuid4().hex[:8]}",
        description="test group",
    )
    db_session.add(group)
    db_session.flush()
    db_session.add(UserGroupMember(group_id=group.id, user_id=owner.id, role="owner"))
    db_session.commit()
    db_session.refresh(group)
    return group


def _add_member(db_session, group, user, role: str = "member") -> UserGroupMember:
    member = UserGroupMember(group_id=group.id, user_id=user.id, role=role)
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


# ---------------------------------------------------------------------------
# GET /api/groups  (list)
# ---------------------------------------------------------------------------


def test_list_groups_unauthorized(client):
    response = client.get("/api/groups")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_list_groups_returns_owned(client, user_token_headers, normal_user, db_session):
    group = _make_group(db_session, normal_user)
    response = client.get("/api/groups", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    uuids = {g["uuid"] for g in response.json()}
    assert str(group.uuid) in uuids


def test_list_groups_excludes_non_member(client, other_user_auth_headers, normal_user, db_session):
    group = _make_group(db_session, normal_user)
    response = client.get("/api/groups", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_200_OK
    uuids = {g["uuid"] for g in response.json()}
    assert str(group.uuid) not in uuids


def test_list_groups_includes_member_group(
    client, other_user_auth_headers, normal_user, other_user, db_session
):
    """A user who is a member (not owner) still sees the group in their list."""
    group = _make_group(db_session, normal_user)
    _add_member(db_session, group, other_user, role="member")
    response = client.get("/api/groups", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_200_OK
    entry = next((g for g in response.json() if g["uuid"] == str(group.uuid)), None)
    assert entry is not None
    assert entry["my_role"] == "member"
    assert entry["member_count"] == 2


# ---------------------------------------------------------------------------
# POST /api/groups  (create)
# ---------------------------------------------------------------------------


def test_create_group_happy(client, user_token_headers):
    name = f"new-grp-{uuid.uuid4().hex[:8]}"
    response = client.post("/api/groups", headers=user_token_headers, json={"name": name})
    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["name"] == name
    assert body["my_role"] == "owner"
    assert body["member_count"] == 1


def test_create_group_unauthorized(client):
    response = client.post("/api/groups", json={"name": "x"})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_create_group_duplicate_name_400(client, user_token_headers, normal_user, db_session):
    existing = _make_group(db_session, normal_user, name="dup-grp")
    response = client.post("/api/groups", headers=user_token_headers, json={"name": existing.name})
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "already exists" in response.json()["detail"]


def test_create_group_empty_name_422(client, user_token_headers):
    """name has min_length=1."""
    response = client.post("/api/groups", headers=user_token_headers, json={"name": ""})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_create_group_missing_name_422(client, user_token_headers):
    response = client.post("/api/groups", headers=user_token_headers, json={})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# GET /api/groups/{uuid}  (detail)
# ---------------------------------------------------------------------------


def test_get_group_detail_member(client, user_token_headers, normal_user, db_session):
    group = _make_group(db_session, normal_user)
    response = client.get(f"/api/groups/{group.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["uuid"] == str(group.uuid)
    assert body["my_role"] == "owner"
    assert len(body["members"]) == 1


def test_get_group_detail_non_member_403(client, other_user_auth_headers, normal_user, db_session):
    group = _make_group(db_session, normal_user)
    response = client.get(f"/api/groups/{group.uuid}", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You are not a member of this group"


def test_get_group_nonexistent_404(client, user_token_headers):
    response = client.get(f"/api/groups/{uuid.uuid4()}", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Group not found"


# ---------------------------------------------------------------------------
# PUT /api/groups/{uuid}  (update; owner/admin)
# ---------------------------------------------------------------------------


def test_update_group_owner(client, user_token_headers, normal_user, db_session):
    group = _make_group(db_session, normal_user)
    response = client.put(
        f"/api/groups/{group.uuid}",
        headers=user_token_headers,
        json={"description": "updated desc"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["description"] == "updated desc"


def test_update_group_non_member_403(client, other_user_auth_headers, normal_user, db_session):
    """A non-member lacks owner/admin role → the group-admin gate rejects them."""
    group = _make_group(db_session, normal_user)
    response = client.put(
        f"/api/groups/{group.uuid}",
        headers=other_user_auth_headers,
        json={"name": "stolen"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Requires owner or admin role in this group"


def test_update_group_plain_member_403(
    client, other_user_auth_headers, normal_user, other_user, db_session
):
    """A plain member (role='member') cannot update the group."""
    group = _make_group(db_session, normal_user)
    _add_member(db_session, group, other_user, role="member")
    response = client.put(
        f"/api/groups/{group.uuid}",
        headers=other_user_auth_headers,
        json={"name": "stolen"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Requires owner or admin role in this group"


def test_update_group_nonexistent_404(client, user_token_headers):
    response = client.put(
        f"/api/groups/{uuid.uuid4()}", headers=user_token_headers, json={"name": "x"}
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Group not found"


# ---------------------------------------------------------------------------
# DELETE /api/groups/{uuid}  (owner only)  — 403-other_user pinned elsewhere
# ---------------------------------------------------------------------------


def test_delete_group_owner_204(client, user_token_headers, normal_user, db_session):
    group = _make_group(db_session, normal_user)
    response = client.delete(f"/api/groups/{group.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT
    follow_up = client.get(f"/api/groups/{group.uuid}", headers=user_token_headers)
    assert follow_up.status_code == status.HTTP_404_NOT_FOUND


def test_delete_group_admin_member_403(
    client, other_user_auth_headers, normal_user, other_user, db_session
):
    """A group *admin* (not the owner) still cannot delete — only owner_id can."""
    group = _make_group(db_session, normal_user)
    _add_member(db_session, group, other_user, role="admin")
    response = client.delete(f"/api/groups/{group.uuid}", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Only the group owner can delete this group"


def test_delete_group_nonexistent_404(client, user_token_headers):
    response = client.delete(f"/api/groups/{uuid.uuid4()}", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# POST /api/groups/{uuid}/members  (add)
# ---------------------------------------------------------------------------


def test_add_member_owner(client, user_token_headers, normal_user, other_user, db_session):
    group = _make_group(db_session, normal_user)
    response = client.post(
        f"/api/groups/{group.uuid}/members",
        headers=user_token_headers,
        json={"user_uuid": str(other_user.uuid), "role": "member"},
    )
    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["user_uuid"] == str(other_user.uuid)
    assert body["role"] == "member"


def test_add_member_already_member_400(
    client, user_token_headers, normal_user, other_user, db_session
):
    group = _make_group(db_session, normal_user)
    _add_member(db_session, group, other_user, role="member")
    response = client.post(
        f"/api/groups/{group.uuid}/members",
        headers=user_token_headers,
        json={"user_uuid": str(other_user.uuid), "role": "member"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "User is already a member of this group"


def test_add_member_unknown_user_404(client, user_token_headers, normal_user, db_session):
    group = _make_group(db_session, normal_user)
    response = client.post(
        f"/api/groups/{group.uuid}/members",
        headers=user_token_headers,
        json={"user_uuid": str(uuid.uuid4()), "role": "member"},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "User not found"


def test_add_member_non_admin_403(
    client, other_user_auth_headers, normal_user, other_user, db_session
):
    group = _make_group(db_session, normal_user)
    response = client.post(
        f"/api/groups/{group.uuid}/members",
        headers=other_user_auth_headers,
        json={"user_uuid": str(other_user.uuid), "role": "member"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Requires owner or admin role in this group"


def test_add_member_invalid_role_422(
    client, user_token_headers, normal_user, other_user, db_session
):
    """role is constrained to admin|member by a regex pattern."""
    group = _make_group(db_session, normal_user)
    response = client.post(
        f"/api/groups/{group.uuid}/members",
        headers=user_token_headers,
        json={"user_uuid": str(other_user.uuid), "role": "superboss"},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# PUT /api/groups/{uuid}/members/{user}  (role update)
# ---------------------------------------------------------------------------


def test_update_member_role_owner(client, user_token_headers, normal_user, other_user, db_session):
    group = _make_group(db_session, normal_user)
    _add_member(db_session, group, other_user, role="member")
    response = client.put(
        f"/api/groups/{group.uuid}/members/{other_user.uuid}",
        headers=user_token_headers,
        json={"role": "admin"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["role"] == "admin"


def test_update_member_role_not_a_member_404(
    client, user_token_headers, normal_user, other_user, db_session
):
    group = _make_group(db_session, normal_user)
    response = client.put(
        f"/api/groups/{group.uuid}/members/{other_user.uuid}",
        headers=user_token_headers,
        json={"role": "admin"},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "User is not a member of this group"


def test_update_member_role_cannot_change_owner_400(
    client, user_token_headers, normal_user, db_session
):
    """The owner's own role cannot be changed."""
    group = _make_group(db_session, normal_user)
    response = client.put(
        f"/api/groups/{group.uuid}/members/{normal_user.uuid}",
        headers=user_token_headers,
        json={"role": "admin"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Cannot change the group owner's role"


def test_update_member_role_admin_cannot_touch_admin_403(
    client, db_session, normal_user, other_user, admin_user
):
    """A group-admin caller cannot promote/demote another admin (owner-only)."""
    # normal_user owns the group; other_user is a group-admin caller; admin_user
    # is a second group-admin member. (admin_user here is a fixture user; its
    # *site* role is irrelevant — group role is what gates this endpoint.)
    group = _make_group(db_session, normal_user)
    _add_member(db_session, group, other_user, role="admin")
    _add_member(db_session, group, admin_user, role="admin")
    # Authenticate as other_user (a group admin).
    login = client.post(
        "/api/auth/token",
        data={"username": other_user.email, "password": "otherpass123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = client.put(
        f"/api/groups/{group.uuid}/members/{admin_user.uuid}",
        headers=headers,
        json={"role": "member"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Only the group owner can change admin roles"


# ---------------------------------------------------------------------------
# DELETE /api/groups/{uuid}/members/{user}  (remove / leave)
# ---------------------------------------------------------------------------


def test_remove_member_owner(client, user_token_headers, normal_user, other_user, db_session):
    group = _make_group(db_session, normal_user)
    _add_member(db_session, group, other_user, role="member")
    response = client.delete(
        f"/api/groups/{group.uuid}/members/{other_user.uuid}",
        headers=user_token_headers,
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_member_can_leave_self(
    client, other_user_auth_headers, normal_user, other_user, db_session
):
    """A plain member can remove themselves (leave) without owner/admin role."""
    group = _make_group(db_session, normal_user)
    _add_member(db_session, group, other_user, role="member")
    response = client.delete(
        f"/api/groups/{group.uuid}/members/{other_user.uuid}",
        headers=other_user_auth_headers,
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_owner_cannot_leave_400(client, user_token_headers, normal_user, db_session):
    group = _make_group(db_session, normal_user)
    response = client.delete(
        f"/api/groups/{group.uuid}/members/{normal_user.uuid}",
        headers=user_token_headers,
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Group owner cannot leave. Delete the group instead."


def test_remove_other_member_requires_admin_403(
    client, db_session, normal_user, other_user, admin_user
):
    """A plain member removing *another* member is rejected by the admin gate."""
    group = _make_group(db_session, normal_user)
    _add_member(db_session, group, other_user, role="member")
    _add_member(db_session, group, admin_user, role="member")
    login = client.post(
        "/api/auth/token",
        data={"username": other_user.email, "password": "otherpass123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = client.delete(
        f"/api/groups/{group.uuid}/members/{admin_user.uuid}",
        headers=headers,
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Requires owner or admin role in this group"


def test_remove_member_not_a_member_404(
    client, user_token_headers, normal_user, other_user, db_session
):
    group = _make_group(db_session, normal_user)
    response = client.delete(
        f"/api/groups/{group.uuid}/members/{other_user.uuid}",
        headers=user_token_headers,
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "User is not a member of this group"


# ---------------------------------------------------------------------------
# Group membership audit events (issue #443's smaller half — these three event
# types + collection/tag RESOURCE_SHARE/UNSHARE previously emitted nothing).
# ---------------------------------------------------------------------------


def test_add_member_emits_group_member_add_audit_event(
    client, user_token_headers, normal_user, other_user, db_session, monkeypatch
):
    from app.api.endpoints import groups as groups_module

    events = []
    monkeypatch.setattr(groups_module.audit_logger, "log", lambda **kw: events.append(kw))

    group = _make_group(db_session, normal_user)
    response = client.post(
        f"/api/groups/{group.uuid}/members",
        headers=user_token_headers,
        json={"user_uuid": str(other_user.uuid), "role": "member"},
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == groups_module.AuditEventType.GROUP_MEMBER_ADD
    assert event["user_id"] == normal_user.id
    assert event["target_user_id"] == other_user.id
    assert event["details"]["group_uuid"] == str(group.uuid)
    assert event["details"]["role"] == "member"


def test_add_member_400_does_not_emit_audit_event(
    client, user_token_headers, normal_user, other_user, db_session, monkeypatch
):
    """Control: a rejected add (already a member) must not fire the event --
    otherwise the assertion above would pass equally for a handler that audits
    every request regardless of outcome."""
    from app.api.endpoints import groups as groups_module

    events = []
    monkeypatch.setattr(groups_module.audit_logger, "log", lambda **kw: events.append(kw))

    group = _make_group(db_session, normal_user)
    _add_member(db_session, group, other_user, role="member")
    response = client.post(
        f"/api/groups/{group.uuid}/members",
        headers=user_token_headers,
        json={"user_uuid": str(other_user.uuid), "role": "member"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert events == []


def test_update_member_role_emits_group_member_role_change_audit_event(
    client, user_token_headers, normal_user, other_user, db_session, monkeypatch
):
    from app.api.endpoints import groups as groups_module

    events = []
    monkeypatch.setattr(groups_module.audit_logger, "log", lambda **kw: events.append(kw))

    group = _make_group(db_session, normal_user)
    _add_member(db_session, group, other_user, role="member")
    response = client.put(
        f"/api/groups/{group.uuid}/members/{other_user.uuid}",
        headers=user_token_headers,
        json={"role": "admin"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == groups_module.AuditEventType.GROUP_MEMBER_ROLE_CHANGE
    assert event["user_id"] == normal_user.id
    assert event["target_user_id"] == other_user.id
    assert event["details"]["previous_role"] == "member"
    assert event["details"]["role"] == "admin"


def test_remove_member_emits_group_member_remove_audit_event(
    client, user_token_headers, normal_user, other_user, db_session, monkeypatch
):
    from app.api.endpoints import groups as groups_module

    events = []
    monkeypatch.setattr(groups_module.audit_logger, "log", lambda **kw: events.append(kw))

    group = _make_group(db_session, normal_user)
    _add_member(db_session, group, other_user, role="member")
    response = client.delete(
        f"/api/groups/{group.uuid}/members/{other_user.uuid}",
        headers=user_token_headers,
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == groups_module.AuditEventType.GROUP_MEMBER_REMOVE
    assert event["user_id"] == normal_user.id
    assert event["target_user_id"] == other_user.id
    assert event["details"]["self_remove"] is False


def test_remove_member_self_leave_marks_self_remove_true(
    client, other_user_auth_headers, normal_user, other_user, db_session, monkeypatch
):
    from app.api.endpoints import groups as groups_module

    events = []
    monkeypatch.setattr(groups_module.audit_logger, "log", lambda **kw: events.append(kw))

    group = _make_group(db_session, normal_user)
    _add_member(db_session, group, other_user, role="member")
    response = client.delete(
        f"/api/groups/{group.uuid}/members/{other_user.uuid}",
        headers=other_user_auth_headers,
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert len(events) == 1
    assert events[0]["details"]["self_remove"] is True
    assert events[0]["user_id"] == other_user.id
    assert events[0]["target_user_id"] == other_user.id
