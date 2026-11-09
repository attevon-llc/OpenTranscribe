"""The collection endpoints commit exactly as often as they did before queries moved out."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event

from app.models.collection import Collection


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


def _overhead(client, headers, db_session) -> int:
    """Commits the auth layer makes for any authenticated request, measured on a read."""
    with _count_commits(db_session) as commits:
        assert client.get("/api/collections", headers=headers).status_code == 200
    return commits[0]


def _owned(db_session, owner) -> Collection:
    collection = Collection(name=f"c-{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db_session.add(collection)
    db_session.commit()
    db_session.refresh(collection)
    return collection


def test_create_collection_commits_once(client, user_token_headers, db_session):
    overhead = _overhead(client, user_token_headers, db_session)
    with _count_commits(db_session) as commits:
        response = client.post(
            "/api/collections",
            json={"name": f"n-{uuid.uuid4().hex[:8]}"},
            headers=user_token_headers,
        )
    assert response.status_code == 200
    assert commits[0] - overhead == 1


def test_rejected_duplicate_name_commits_nothing(
    client, user_token_headers, normal_user, db_session
):
    existing = _owned(db_session, normal_user)
    overhead = _overhead(client, user_token_headers, db_session)
    with _count_commits(db_session) as commits:
        response = client.post(
            "/api/collections", json={"name": existing.name}, headers=user_token_headers
        )
    assert response.status_code == 400
    assert commits[0] - overhead == 0


def test_rename_collection_commits_once(client, user_token_headers, normal_user, db_session):
    collection = _owned(db_session, normal_user)
    overhead = _overhead(client, user_token_headers, db_session)
    with _count_commits(db_session) as commits:
        response = client.put(
            f"/api/collections/{collection.uuid}",
            json={"name": f"r-{uuid.uuid4().hex[:8]}"},
            headers=user_token_headers,
        )
    assert response.status_code == 200
    assert commits[0] - overhead == 1


def test_listing_commits_nothing(client, user_token_headers, normal_user, db_session):
    _owned(db_session, normal_user)
    overhead = _overhead(client, user_token_headers, db_session)
    with _count_commits(db_session) as commits:
        assert client.get("/api/collections", headers=user_token_headers).status_code == 200
    assert commits[0] - overhead == 0
