"""Pins for ``app/services/comment_queries.py``, the lookups behind the comment routes."""

from __future__ import annotations

import uuid
from typing import Any

from app.models.media import MediaFile
from app.models.user import User
from app.services.comment_queries import add_comment
from app.services.comment_queries import file_org_and_owner
from app.services.comment_queries import file_organization_id
from app.services.comment_queries import file_owner_id
from app.services.comment_queries import get_comment_file
from app.services.comment_queries import list_file_comments
from app.services.comment_queries import load_comment


def _id(row: Any) -> int:
    """The id of a row a lookup must have found (fails the test when it returned None)."""
    assert row is not None
    return int(row.id)


def _user(db) -> User:
    user = User(
        email=f"comment-{uuid.uuid4().hex[:8]}@example.com",
        full_name="comment user",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.flush()
    return user


def _file(db, owner: User) -> MediaFile:
    file_uuid = uuid.uuid4()
    media = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="c.wav",
        storage_path=f"c/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1,
        status="completed",
    )
    db.add(media)
    db.flush()
    return media


def test_add_comment_flushes_an_id_without_committing(db_session, monkeypatch):
    commits: list[int] = []
    monkeypatch.setattr(db_session, "commit", lambda: commits.append(1))
    owner = _user(db_session)
    media = _file(db_session, owner)
    comment = add_comment(
        db_session, media_file_id=media.id, user_id=owner.id, text="hi", timestamp=1.5
    )
    assert comment.id is not None
    assert (comment.text, comment.timestamp, comment.media_file_id) == ("hi", 1.5, media.id)
    assert commits == []


def test_list_file_comments_scopes_to_the_file_and_orders_on_request(db_session):
    owner = _user(db_session)
    media, other = _file(db_session, owner), _file(db_session, owner)
    late = add_comment(db_session, media_file_id=media.id, user_id=owner.id, text="b", timestamp=9)
    early = add_comment(db_session, media_file_id=media.id, user_id=owner.id, text="a", timestamp=1)
    add_comment(db_session, media_file_id=other.id, user_id=owner.id, text="x", timestamp=0)
    ordered = list_file_comments(db_session, media.id, order_by_timestamp=True)
    assert [c.id for c in ordered] == [early.id, late.id]
    assert {c.id for c in list_file_comments(db_session, media.id, order_by_timestamp=False)} == {
        early.id,
        late.id,
    }


def test_load_comment_returns_the_row_or_none(db_session):
    owner = _user(db_session)
    comment = add_comment(
        db_session,
        media_file_id=_file(db_session, owner).id,
        user_id=owner.id,
        text="t",
        timestamp=0,
    )
    assert _id(load_comment(db_session, comment.id)) == comment.id
    assert load_comment(db_session, -1) is None


def test_file_lookups_return_owner_org_and_row_or_nothing(db_session):
    owner = _user(db_session)
    media = _file(db_session, owner)
    assert _id(get_comment_file(db_session, media.id)) == media.id
    assert get_comment_file(db_session, -1) is None
    assert file_organization_id(db_session, media.id) is None
    assert file_org_and_owner(db_session, media.id) == (None, owner.id)
    assert file_org_and_owner(db_session, -1) is None
    assert file_owner_id(db_session, media.id) == owner.id
    assert file_owner_id(db_session, -1) is None
