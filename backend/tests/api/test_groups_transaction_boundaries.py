"""The groups endpoints commit exactly as often as they did before queries moved to a service."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event

from app.models.group import UserGroup
from app.models.group import UserGroupMember


@contextmanager
def _count_commits(db_session) -> Iterator[list[int]]:
    seen = [0]

    def on_commit(_session) -> None:
        seen[0] += 1

    event.listen(db_session, "after_commit", on_commit)
    try:
        yield seen
    finally:
        event.remove(db_session, "after_commit", on_commit)


def _owned_group(db_session, owner) -> UserGroup:
    group = UserGroup(owner_id=owner.id, name=f"grp-{uuid.uuid4().hex[:8]}", description="d")
    db_session.add(group)
    db_session.flush()
    db_session.add(UserGroupMember(group_id=group.id, user_id=owner.id, role="owner"))
    db_session.commit()
    db_session.refresh(group)
    return group


def _request_overhead(client, headers, db_session) -> int:
    """Commits the auth layer makes for any authenticated request, measured on a read."""
    with _count_commits(db_session) as commits:
        assert client.get("/api/groups", headers=headers).status_code == 200
    return commits[0]


def test_create_group_commits_once(client, user_token_headers, db_session):
    overhead = _request_overhead(client, user_token_headers, db_session)
    with _count_commits(db_session) as commits:
        response = client.post(
            "/api/groups", json={"name": f"g-{uuid.uuid4().hex[:8]}"}, headers=user_token_headers
        )
    assert response.status_code == 201
    assert commits[0] - overhead == 1


def test_update_group_commits_once(client, user_token_headers, normal_user, db_session):
    group = _owned_group(db_session, normal_user)
    overhead = _request_overhead(client, user_token_headers, db_session)
    with _count_commits(db_session) as commits:
        response = client.put(
            f"/api/groups/{group.uuid}", json={"description": "x"}, headers=user_token_headers
        )
    assert response.status_code == 200
    assert commits[0] - overhead == 1


def test_rejected_duplicate_name_commits_nothing(
    client, user_token_headers, normal_user, db_session
):
    group = _owned_group(db_session, normal_user)
    overhead = _request_overhead(client, user_token_headers, db_session)
    with _count_commits(db_session) as commits:
        response = client.post("/api/groups", json={"name": group.name}, headers=user_token_headers)
    assert response.status_code == 400
    assert commits[0] - overhead == 0
