"""``migration_file_queries`` and ``search_reindex_queries`` against the real database."""

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime

import pytest

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.user import User
from app.services import migration_file_queries as mfq
from app.services import search_reindex_queries as srq


@pytest.fixture
def owner(db_session):
    user = User(
        email=f"mig_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name="Owner",
        hashed_password="x",
        is_active=True,
        role="user",
    )
    db_session.add(user)
    db_session.flush()
    return user


def _file(db, user, status, name="f.mp4"):
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=name,
        content_type="video/mp4",
        file_size=1,
        storage_path=f"test/{uuid_pkg.uuid4().hex}.mp4",
        status=status,
    )
    db.add(media)
    db.flush()
    return media


def _speaker(db, media, predicted, name="SPEAKER_00"):
    db.add(
        Speaker(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media.id,
            user_id=media.user_id,
            name=name,
            attributes_predicted_at=datetime.now(UTC) if predicted else None,
        )
    )
    db.flush()


def test_completed_file_queries_ignore_other_statuses(db_session, owner):
    done = _file(db_session, owner, FileStatus.COMPLETED)
    _file(db_session, owner, FileStatus.PENDING)
    assert done.id in mfq.completed_file_ids(db_session)
    rows = {r.id: r.uuid for r in mfq.completed_file_id_uuid_rows(db_session)}
    assert rows[done.id] == done.uuid
    before = mfq.completed_file_count(db_session)
    _file(db_session, owner, FileStatus.COMPLETED)
    assert mfq.completed_file_count(db_session) == before + 1


def test_attribute_migration_counts_pending_is_files_with_unpredicted_speakers(db_session, owner):
    base = mfq.attribute_migration_counts(db_session)
    pending = _file(db_session, owner, FileStatus.COMPLETED)
    _speaker(db_session, pending, predicted=False)
    _speaker(db_session, pending, predicted=True, name="SPEAKER_01")
    done = _file(db_session, owner, FileStatus.COMPLETED)
    _speaker(db_session, done, predicted=True)
    _file(db_session, owner, FileStatus.COMPLETED)  # no speakers: counted nowhere
    after = mfq.attribute_migration_counts(db_session)
    assert after.total == base.total + 2
    assert after.pending == base.pending + 1


def test_titles_for_uuids_applies_owner_filter_only_when_tenant_scoped(db_session, owner):
    mine = _file(db_session, owner, FileStatus.COMPLETED, "mine.mp4")
    stranger = User(
        email=f"s_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name="S",
        hashed_password="x",
        is_active=True,
        role="user",
    )
    db_session.add(stranger)
    db_session.flush()
    theirs = _file(db_session, stranger, FileStatus.COMPLETED, "theirs.mp4")
    uuids = [mine.uuid, theirs.uuid]
    everything = srq.titles_for_uuids(
        db_session, uuids, organization_id=None, owner_user_id=None, tenant_scoped=False
    )
    assert everything == {str(mine.uuid): "mine.mp4", str(theirs.uuid): "theirs.mp4"}
    own = srq.titles_for_uuids(
        db_session, uuids, organization_id=None, owner_user_id=owner.id, tenant_scoped=True
    )
    assert own == {str(mine.uuid): "mine.mp4"}


def test_count_indexable_files_requires_segments_and_completed(db_session, owner):
    indexable = _file(db_session, owner, FileStatus.COMPLETED)
    db_session.add(
        TranscriptSegment(media_file_id=indexable.id, start_time=0.0, end_time=1.0, text="hi")
    )
    _file(db_session, owner, FileStatus.COMPLETED)  # no segments
    unfinished = _file(db_session, owner, FileStatus.PROCESSING)
    db_session.add(
        TranscriptSegment(media_file_id=unfinished.id, start_time=0.0, end_time=1.0, text="hi")
    )
    db_session.flush()
    assert srq.count_indexable_files(db_session, user_id=owner.id, organization_id=None) == 1
