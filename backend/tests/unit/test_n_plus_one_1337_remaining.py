"""Statement counts stay flat for the five N+1 sites #1337 left open (T4, P6, P7, K2, K3).

Each test builds the same shape at n=2 and n=12 rows, each row on its OWN file or collection
(a shared parent would hit the session identity map and hide the lazy load), expires the
session so nothing is cached, and asserts the two counts are EQUAL, with a control that the
rows really came back. The fix in each case is ``selectinload``, SQLAlchemy 2.0's documented
loader for a collection or many-to-one fan-out
(https://docs.sqlalchemy.org/en/20/orm/queryguide/relationships.html#selectin-eager-loading).
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg

import pytest

from app.models.collection import Collection
from app.models.content_hold import ContentHold
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.services.speaker_matching_service import SpeakerMatchingService
from tests.query_budget import count_statements

pytestmark = pytest.mark.unit


def _file(db, user, *, status=FileStatus.COMPLETED, **extra):
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"{uuid_pkg.uuid4().hex}.mp4",
        title="t",
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status=status,
        upload_time=extra.pop("upload_time", dt.datetime.now(dt.UTC)),
        **extra,
    )
    db.add(media)
    db.flush()
    return media


def _flat(db, build, run):
    """Rows seen and statement logs for n=2, then n=12; the test asserts on the result."""
    build(2)
    db.commit()
    db.expire_all()
    with count_statements(db.connection()) as small:
        seen_small = run()
    build(10)
    db.commit()
    db.expire_all()
    with count_statements(db.connection()) as large:
        seen_large = run()
    return (len(seen_small), len(seen_large)), small, large


def test_collection_holds_page_loads_collections_in_one_statement(db_session, normal_user):
    from app.services.content_hold_queries import list_platform_holds

    def build(n):
        for _ in range(n):
            coll = Collection(name=f"c{uuid_pkg.uuid4().hex}", user_id=normal_user.id)
            db_session.add(coll)
            db_session.flush()
            db_session.add(
                ContentHold(
                    authority="platform",
                    scope="collection",
                    collection_id=coll.id,
                    reason="Preservation demand 2026-77",
                )
            )

    def run():
        rows, _ = list_platform_holds(db_session, limit=500)
        mine = [h for h in rows if h.collection and h.collection.user_id == normal_user.id]
        return mine

    sizes, small, large = _flat(db_session, build, run)
    assert sizes == (2, 12)  # control: every row came back
    assert large.count == small.count, large.describe()


def test_profile_occurrences_statement_count_is_flat(db_session, normal_user):
    from app.services.speaker_matching.profiles import find_speaker_occurrences

    profile = SpeakerProfile(user_id=normal_user.id, name="P")
    db_session.add(profile)
    db_session.flush()

    def build(n):
        for _ in range(n):
            media = _file(db_session, normal_user)
            db_session.add(
                Speaker(
                    uuid=uuid_pkg.uuid4(),
                    user_id=normal_user.id,
                    media_file_id=media.id,
                    name="SPEAKER_00",
                    profile_id=profile.id,
                    identity_source="voiceprint_link",
                    verified=False,
                )
            )

    def run():
        svc = SpeakerMatchingService(db_session, None)
        return find_speaker_occurrences(svc, profile.id, normal_user.id)

    sizes, small, large = _flat(db_session, build, run)
    assert sizes == (2, 12)  # control: every row came back
    assert large.count == small.count, large.describe()


def test_speakers_linked_to_profile_loads_files_in_one_statement(db_session, normal_user):
    from app.services.speaker_list_queries import speakers_linked_to_profile

    profile = SpeakerProfile(user_id=normal_user.id, name="P2")
    db_session.add(profile)
    db_session.flush()

    def build(n):
        for _ in range(n):
            media = _file(db_session, normal_user)
            db_session.add(
                Speaker(
                    uuid=uuid_pkg.uuid4(),
                    user_id=normal_user.id,
                    media_file_id=media.id,
                    name="SPEAKER_00",
                    profile_id=profile.id,
                    verified=False,
                )
            )

    def run():
        rows = speakers_linked_to_profile(
            db_session,
            profile.id,
            owner_user_id=normal_user.id,
            tenant_pred=None,
            include_quarantined=None,
        )
        return [s.media_file.filename for s in rows]

    sizes, small, large = _flat(db_session, build, run)
    assert sizes == (2, 12)  # control: every row came back
    assert large.count == small.count, large.describe()


def test_stuck_tasks_load_their_files_in_one_statement(db_session, normal_user):
    from app.services.task_detection_service import task_detection_service

    long_ago = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)

    def build(n):
        for _ in range(n):
            media = _file(db_session, normal_user, status=FileStatus.PROCESSING)
            db_session.add(
                Task(
                    id=str(uuid_pkg.uuid4()),
                    user_id=normal_user.id,
                    media_file_id=media.id,
                    task_type="summarization",
                    status="in_progress",
                    progress=0.5,
                    created_at=long_ago,
                    updated_at=long_ago,
                )
            )

    def run():
        tasks = task_detection_service.identify_stuck_tasks(db_session)
        mine = [t for t in tasks if t.user_id == normal_user.id]
        # the endpoint reads each task's file; returning the uuids makes the access real
        return [t.media_file.uuid for t in mine if t.media_file is not None]

    sizes, small, large = _flat(db_session, build, run)
    assert sizes == (2, 12)  # control: every row came back
    assert large.count == small.count, large.describe()


def test_inconsistent_files_load_their_owners_in_one_statement(db_session, normal_user):
    from app.models.user import User
    from app.services.task_detection_service import task_detection_service

    long_ago = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)

    def build(n):
        for i in range(n):
            owner = User(
                email=f"own{uuid_pkg.uuid4().hex}@example.com",
                hashed_password="x",
                full_name=f"o{i}",
            )
            db_session.add(owner)
            db_session.flush()
            _file(db_session, owner, status=FileStatus.PENDING, upload_time=long_ago)

    def run():
        files = task_detection_service.identify_inconsistent_media_files(db_session)
        mine = [f for f in files if f.upload_time == long_ago]
        # the endpoint reads each file's owner
        return [f.user.uuid for f in mine]

    sizes, small, large = _flat(db_session, build, run)
    assert sizes == (2, 12)  # control: every row came back
    assert large.count == small.count, large.describe()
