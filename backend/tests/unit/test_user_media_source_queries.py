"""Media-source queries in ``services.user_media_source_queries``."""

from __future__ import annotations

from app.models.user_media_source import UserMediaSource
from app.services import user_media_source_queries as q


def _source(db_session, user, hostname, **fields):
    row = UserMediaSource(user_id=user.id, hostname=hostname, provider_type="mediacms", **fields)
    db_session.add(row)
    db_session.flush()
    return row


def test_own_listing_count_and_uuid_lookup_are_scoped_to_the_owner(
    normal_user, admin_user, db_session
):
    mine = _source(db_session, normal_user, "a.example")
    theirs = _source(db_session, admin_user, "b.example")
    assert [s.id for s in q.list_own_media_sources(db_session, normal_user.id)] == [mine.id]
    assert q.count_media_sources(db_session, normal_user.id) == 1
    assert q.find_own_media_source(db_session, str(mine.uuid), normal_user.id) is not None
    assert q.find_own_media_source(db_session, str(theirs.uuid), normal_user.id) is None


def test_shared_listing_needs_shared_active_source_of_an_active_other_user(
    normal_user, admin_user, db_session
):
    visible = _source(db_session, admin_user, "shared.example", is_shared=True, is_active=True)
    _source(db_session, admin_user, "private.example", is_shared=False, is_active=True)
    _source(db_session, admin_user, "off.example", is_shared=True, is_active=False)
    _source(db_session, normal_user, "own.example", is_shared=True, is_active=True)
    ids = [s.id for s in q.list_shared_media_sources(db_session, normal_user.id, None)]
    assert visible.id in ids
    assert all(
        s.user_id != normal_user.id and s.is_shared and s.is_active
        for s in q.list_shared_media_sources(db_session, normal_user.id, None)
    )
