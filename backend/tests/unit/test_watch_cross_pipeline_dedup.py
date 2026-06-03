"""Tests for check_duplicate_by_imohash — the cross-pipeline dedup layer.

Runs against the live test DB (savepoint-rolled-back) via the db_session fixture.
"""

from __future__ import annotations

import uuid as uuid_pkg

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.utils.file_hash import check_duplicate_by_imohash


def _make_media(db, user, imohash, status=FileStatus.COMPLETED, storage_path="user_1/file_x/a.mp4"):
    mf = MediaFile(
        uuid=uuid_pkg.uuid4(),
        filename="a.mp4",
        user_id=user.id,
        storage_path=storage_path,
        file_size=1234,
        content_type="video/mp4",
        status=status,
        imohash=imohash,
    )
    db.add(mf)
    db.flush()
    return mf


def test_finds_matching_imohash(db_session, normal_user):
    mf = _make_media(db_session, normal_user, "deadbeef00")
    found = check_duplicate_by_imohash(db_session, "deadbeef00")
    assert found is not None
    assert found.id == mf.id


def test_no_match_returns_none(db_session, normal_user):
    _make_media(db_session, normal_user, "aaaa1111")
    assert check_duplicate_by_imohash(db_session, "no-such-hash") is None


def test_empty_imohash_returns_none(db_session, normal_user):
    assert check_duplicate_by_imohash(db_session, "") is None


def test_error_status_is_ignored(db_session, normal_user):
    _make_media(db_session, normal_user, "errhash22", status=FileStatus.ERROR)
    assert check_duplicate_by_imohash(db_session, "errhash22") is None


def test_exclude_file_id(db_session, normal_user):
    mf = _make_media(db_session, normal_user, "excl333")
    # Excluding the only match yields nothing.
    assert check_duplicate_by_imohash(db_session, "excl333", exclude_file_id=mf.id) is None
