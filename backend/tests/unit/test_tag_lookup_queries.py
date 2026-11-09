"""Pins for the tag lookups the tag routers used to run inline.

``visible_tag_id`` (tag_scope), ``get_tag`` and ``get_share`` (tag_sharing).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import NoResultFound

from app.models.sharing import TagShare
from app.models.tag import Tag
from app.models.user import User
from app.services.tag_scope import visible_tag_id
from app.services.tag_sharing import get_share
from app.services.tag_sharing import get_tag


def _user(db) -> User:
    user = User(
        email=f"lookup-{uuid.uuid4().hex[:8]}@example.com",
        full_name="lookup user",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.flush()
    return user


def _tag(db, owner: User | None) -> Tag:
    tag = Tag(name=f"lookup-{uuid.uuid4().hex[:8]}", user_id=owner.id if owner else None)
    db.add(tag)
    db.flush()
    return tag


def test_visible_tag_id_returns_the_id_of_an_owned_tag(db_session):
    owner = _user(db_session)
    tag = _tag(db_session, owner)
    assert visible_tag_id(db_session, tag.uuid, owner.id, None) == tag.id


def test_visible_tag_id_hides_another_users_unshared_tag(db_session):
    owner, stranger = _user(db_session), _user(db_session)
    tag = _tag(db_session, owner)
    assert visible_tag_id(db_session, tag.uuid, stranger.id, None) is None


def test_visible_tag_id_is_none_for_an_unknown_uuid(db_session):
    caller = _user(db_session)
    assert visible_tag_id(db_session, uuid.uuid4(), caller.id, None) is None


def test_get_tag_returns_the_row_and_raises_when_gone(db_session):
    tag = _tag(db_session, _user(db_session))
    assert get_tag(db_session, tag.id).id == tag.id
    with pytest.raises(NoResultFound):
        get_tag(db_session, -1)


def test_get_share_matches_on_tag_and_share_uuid_together(db_session):
    owner, other = _user(db_session), _user(db_session)
    tag, other_tag = _tag(db_session, owner), _tag(db_session, owner)
    share = TagShare(
        tag_id=tag.id, target_type="user", target_user_id=other.id, shared_by_id=owner.id
    )
    db_session.add(share)
    db_session.flush()
    found = get_share(db_session, tag.id, share.uuid)
    assert found is not None
    assert found.id == share.id
    assert get_share(db_session, other_tag.id, share.uuid) is None
    assert get_share(db_session, tag.id, uuid.uuid4()) is None


def test_lookups_never_commit_so_the_routers_transaction_boundary_is_unchanged(
    db_session, monkeypatch
):
    commits: list[int] = []
    monkeypatch.setattr(db_session, "commit", lambda: commits.append(1))
    owner = _user(db_session)
    tag = _tag(db_session, owner)
    visible_tag_id(db_session, tag.uuid, owner.id, None)
    get_tag(db_session, tag.id)
    get_share(db_session, tag.id, uuid.uuid4())
    assert commits == []
