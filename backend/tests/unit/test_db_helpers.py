"""``app/utils/db_helpers.py`` — the tenant-scope choke point and query builders.

These are exercised against the real Postgres savepoint (``db_session``) rather
than a mocked ``Session``/``Query``, so a wrong filter clause actually fails the
assertion instead of a mock silently accepting whatever was called on it.
"""

from __future__ import annotations

import uuid

from app.core.enums import FileStatus
from app.core.tenancy import UNSCOPED
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import Tag
from app.models.media import TranscriptSegment
from app.models.organization import Organization
from app.utils import db_helpers


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _make_org(db_session) -> Organization:
    org = Organization(name=f"org-{_suffix()}")
    db_session.add(org)
    db_session.flush()
    return org


def _make_file(db_session, owner, *, status=FileStatus.COMPLETED, **kwargs) -> MediaFile:
    file_uuid = uuid.uuid4()
    defaults = dict(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"dbhelpers-{file_uuid.hex[:8]}.wav",
        storage_path=f"dbhelpers-test/{file_uuid.hex}.wav",
        content_type="audio/wav",
        file_size=1024,
        status=status,
    )
    defaults.update(kwargs)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.flush()
    return media_file


# ---------------------------------------------------------------------------
# apply_tenant_scope
# ---------------------------------------------------------------------------


def test_apply_tenant_scope_unscoped_filters_by_user_only(db_session, normal_user, admin_user):
    mine = _make_file(db_session, normal_user)
    _make_file(db_session, admin_user)

    query = db_helpers.apply_tenant_scope(
        db_session.query(MediaFile), MediaFile, user_id=normal_user.id, organization_id=UNSCOPED
    )
    results = query.all()

    assert [f.id for f in results] == [mine.id]


def test_apply_tenant_scope_explicit_org_filters_by_org(db_session, normal_user, admin_user):
    org_a = _make_org(db_session)
    org_b = _make_org(db_session)
    org_file = _make_file(db_session, normal_user, organization_id=org_a.id)
    _make_file(db_session, normal_user, organization_id=None)
    _make_file(db_session, admin_user, organization_id=org_b.id)

    query = db_helpers.apply_tenant_scope(
        db_session.query(MediaFile), MediaFile, user_id=normal_user.id, organization_id=org_a.id
    )
    results = query.all()

    assert [f.id for f in results] == [org_file.id]


def test_apply_tenant_scope_none_is_personal_scope_not_org(db_session, normal_user):
    """organization_id=None must mean 'this user's personal rows', not 'ignore org'."""
    org = _make_org(db_session)
    personal = _make_file(db_session, normal_user, organization_id=None)
    _make_file(db_session, normal_user, organization_id=org.id)

    query = db_helpers.apply_tenant_scope(
        db_session.query(MediaFile), MediaFile, user_id=normal_user.id, organization_id=None
    )
    results = query.all()

    assert [f.id for f in results] == [personal.id]


def test_apply_tenant_scope_none_excludes_other_users_personal_rows(
    db_session, normal_user, admin_user
):
    _make_file(db_session, admin_user, organization_id=None)

    query = db_helpers.apply_tenant_scope(
        db_session.query(MediaFile), MediaFile, user_id=normal_user.id, organization_id=None
    )

    assert query.all() == []


# ---------------------------------------------------------------------------
# get_user_files_query / get_user_files_query_for_context
# ---------------------------------------------------------------------------


def test_get_user_files_query_defaults_to_unscoped_user_filter(db_session, normal_user, admin_user):
    mine = _make_file(db_session, normal_user)
    _make_file(db_session, admin_user)

    results = db_helpers.get_user_files_query(db_session, normal_user.id).all()

    assert [f.id for f in results] == [mine.id]


def test_get_user_files_query_for_context_uses_context_org_scope(
    db_session, normal_user, admin_user
):
    from app.api.deps_context import RequestContext

    org = _make_org(db_session)
    org_file = _make_file(db_session, normal_user, organization_id=org.id)
    _make_file(db_session, normal_user, organization_id=None)

    ctx = RequestContext(user=normal_user, org_id=org.id)
    results = db_helpers.get_user_files_query_for_context(db_session, ctx).all()

    assert [f.id for f in results] == [org_file.id]


# ---------------------------------------------------------------------------
# get_or_create
# ---------------------------------------------------------------------------


def test_get_or_create_creates_when_missing(db_session, normal_user):
    name = f"tag-{_suffix()}"

    tag, created = db_helpers.get_or_create(
        db_session, Tag, defaults={"source": "manual"}, name=name, user_id=normal_user.id
    )

    assert created is True
    assert tag.id is not None
    assert tag.source == "manual"

    count = db_session.query(Tag).filter(Tag.name == name, Tag.user_id == normal_user.id).count()
    assert count == 1


def test_get_or_create_returns_existing_without_duplicating(db_session, normal_user):
    name = f"tag-{_suffix()}"
    first, created_first = db_helpers.get_or_create(
        db_session, Tag, defaults={"source": "manual"}, name=name, user_id=normal_user.id
    )
    second, created_second = db_helpers.get_or_create(
        db_session, Tag, defaults={"source": "auto_ai"}, name=name, user_id=normal_user.id
    )

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    # defaults are only applied on creation — the second call must not overwrite it.
    assert second.source == "manual"

    count = db_session.query(Tag).filter(Tag.name == name, Tag.user_id == normal_user.id).count()
    assert count == 1


# ---------------------------------------------------------------------------
# safe_get_by_id
# ---------------------------------------------------------------------------


def test_safe_get_by_id_found(db_session, normal_user):
    media_file = _make_file(db_session, normal_user)

    result = db_helpers.safe_get_by_id(db_session, MediaFile, media_file.id)

    assert result is not None
    assert result.id == media_file.id


def test_safe_get_by_id_not_found_returns_none(db_session):
    assert db_helpers.safe_get_by_id(db_session, MediaFile, 2**31 - 1) is None


def test_safe_get_by_id_filters_by_owning_user(db_session, normal_user, admin_user):
    media_file = _make_file(db_session, admin_user)

    result = db_helpers.safe_get_by_id(db_session, MediaFile, media_file.id, user_id=normal_user.id)

    assert result is None


def test_safe_get_by_id_skips_user_filter_when_model_has_no_user_id(db_session, normal_user):
    """FileTag has no user_id column — the hasattr guard must skip the filter, not error."""
    media_file = _make_file(db_session, normal_user)
    tag = Tag(name=f"tag-{_suffix()}", user_id=normal_user.id)
    db_session.add(tag)
    db_session.flush()
    link = FileTag(media_file_id=media_file.id, tag_id=tag.id, source="manual")
    db_session.add(link)
    db_session.flush()

    result = db_helpers.safe_get_by_id(db_session, FileTag, link.id, user_id=999999)

    assert result is not None
    assert result.id == link.id


def test_safe_get_by_id_swallows_query_error_and_returns_none(db_session):
    # Integer column compared against a value Postgres can't cast -> DataError.
    result = db_helpers.safe_get_by_id(db_session, MediaFile, "not-an-int")  # type: ignore[arg-type]  # deliberately wrong type to trigger a DB-level error

    assert result is None


def test_safe_get_by_id_leaves_session_usable_after_query_error(db_session, normal_user):
    """``safe_get_by_id``'s ``except SQLAlchemyError`` must call ``db.rollback()``,
    matching ``get_or_create``/``bulk_update`` in this same module — otherwise
    Postgres leaves the transaction aborted after the failed statement, and every
    later query on this session keeps failing with "current transaction is
    aborted" until something rolls it back.

    The user id is captured as a plain int BEFORE the error, not read off the
    ORM instance afterward: any ``db.rollback()`` (including the sibling
    functions' pre-existing, correct ones) expires already-loaded instances as a
    normal side effect, and refreshing an expired instance through this test
    harness's savepoint-based isolation is a separate, unrelated concern from
    "is the session still usable for new statements" — which is what this test
    actually checks.
    """
    user_id = normal_user.id

    db_helpers.safe_get_by_id(db_session, MediaFile, "not-an-int")  # type: ignore[arg-type]  # deliberately wrong type to trigger a DB-level error

    # A completely unrelated, well-formed query must still work.
    count = db_session.query(MediaFile).filter(MediaFile.user_id == user_id).count()
    assert count == 0


# ---------------------------------------------------------------------------
# bulk_update
# ---------------------------------------------------------------------------


def test_bulk_update_applies_all_updates(db_session, normal_user):
    file_a = _make_file(db_session, normal_user)
    file_b = _make_file(db_session, normal_user)
    db_session.commit()

    ok = db_helpers.bulk_update(
        db_session,
        MediaFile,
        [
            {"id": file_a.id, "duration": 12.5},
            {"id": file_b.id, "duration": 99.0},
        ],
    )

    assert ok is True
    db_session.expire_all()
    assert db_session.get(MediaFile, file_a.id).duration == 12.5
    assert db_session.get(MediaFile, file_b.id).duration == 99.0


def test_bulk_update_rolls_back_and_returns_false_on_constraint_violation(db_session, normal_user):
    file_a = _make_file(db_session, normal_user)

    # file_size is NOT NULL -> IntegrityError as soon as the UPDATE executes.
    ok = db_helpers.bulk_update(db_session, MediaFile, [{"id": file_a.id, "file_size": None}])

    assert ok is False
    # The session must come back usable: bulk_update's own db.rollback() has to
    # actually recover the aborted transaction, not just swallow the exception
    # and leave every later query on this session failing with "current
    # transaction is aborted". The rollback also undoes file_a's own (never
    # committed) insert, since it was never durable to begin with.
    count = db_session.query(MediaFile).filter(MediaFile.id == file_a.id).count()
    assert count == 0


# ---------------------------------------------------------------------------
# get_file_with_transcript_count
# ---------------------------------------------------------------------------


def test_get_file_with_transcript_count_counts_segments(db_session, normal_user):
    media_file = _make_file(db_session, normal_user)
    for i in range(3):
        db_session.add(
            TranscriptSegment(
                media_file_id=media_file.id, start_time=i, end_time=i + 1, text=f"seg {i}"
            )
        )
    db_session.flush()

    result, count = db_helpers.get_file_with_transcript_count(
        db_session, media_file.id, normal_user.id
    )

    assert result is not None
    assert result.id == media_file.id
    assert count == 3


def test_get_file_with_transcript_count_zero_segments(db_session, normal_user):
    media_file = _make_file(db_session, normal_user)

    result, count = db_helpers.get_file_with_transcript_count(
        db_session, media_file.id, normal_user.id
    )

    assert result is not None
    assert count == 0


def test_get_file_with_transcript_count_missing_file(db_session, normal_user):
    result, count = db_helpers.get_file_with_transcript_count(db_session, 2**31 - 1, normal_user.id)

    assert result is None
    assert count == 0


# ---------------------------------------------------------------------------
# get_user_speakers / get_unique_speakers_for_file
# ---------------------------------------------------------------------------


def test_get_user_speakers_scoped_to_owner(db_session, normal_user, admin_user):
    file_mine = _make_file(db_session, normal_user)
    file_theirs = _make_file(db_session, admin_user)
    mine = Speaker(user_id=normal_user.id, media_file_id=file_mine.id, name="SPEAKER_00")
    theirs = Speaker(user_id=admin_user.id, media_file_id=file_theirs.id, name="SPEAKER_00")
    db_session.add_all([mine, theirs])
    db_session.flush()

    results = db_helpers.get_user_speakers(db_session, normal_user.id)

    assert [s.id for s in results] == [mine.id]


def test_get_unique_speakers_for_file_scoped_to_file(db_session, normal_user):
    file_a = _make_file(db_session, normal_user)
    file_b = _make_file(db_session, normal_user)
    speaker_a = Speaker(user_id=normal_user.id, media_file_id=file_a.id, name="SPEAKER_00")
    speaker_b = Speaker(user_id=normal_user.id, media_file_id=file_b.id, name="SPEAKER_00")
    db_session.add_all([speaker_a, speaker_b])
    db_session.flush()

    results = db_helpers.get_unique_speakers_for_file(db_session, file_a.id)

    assert [s.id for s in results] == [speaker_a.id]


# ---------------------------------------------------------------------------
# get_file_tags
# ---------------------------------------------------------------------------


def test_get_file_tags_returns_tag_names(db_session, normal_user):
    media_file = _make_file(db_session, normal_user)
    tag_1 = Tag(name=f"alpha-{_suffix()}", user_id=normal_user.id)
    tag_2 = Tag(name=f"beta-{_suffix()}", user_id=normal_user.id)
    db_session.add_all([tag_1, tag_2])
    db_session.flush()
    db_session.add_all(
        [
            FileTag(media_file_id=media_file.id, tag_id=tag_1.id, source="manual"),
            FileTag(media_file_id=media_file.id, tag_id=tag_2.id, source="manual"),
        ]
    )
    db_session.flush()

    names = db_helpers.get_file_tags(db_session, media_file.id)

    assert sorted(names) == sorted([tag_1.name, tag_2.name])


def test_get_file_tags_empty_for_untagged_file(db_session, normal_user):
    media_file = _make_file(db_session, normal_user)

    assert db_helpers.get_file_tags(db_session, media_file.id) == []


def test_get_file_tags_swallows_query_error(db_session):
    # media_file_id is an Integer FK -> a non-numeric filter value raises DataError,
    # which the function is documented to catch and turn into [].
    assert db_helpers.get_file_tags(db_session, "not-an-int") == []  # type: ignore[arg-type]  # deliberately wrong type to trigger a DB-level error


def test_get_file_tags_leaves_session_usable_after_query_error(db_session, normal_user):
    """Same missing ``db.rollback()`` as ``safe_get_by_id`` — see that test's
    docstring for why the user id is captured before the error rather than read
    off the ORM instance afterward.
    """
    user_id = normal_user.id

    db_helpers.get_file_tags(db_session, "not-an-int")  # type: ignore[arg-type]  # deliberately wrong type to trigger a DB-level error

    count = db_session.query(MediaFile).filter(MediaFile.user_id == user_id).count()
    assert count == 0


# ---------------------------------------------------------------------------
# get_files_by_status
# ---------------------------------------------------------------------------


def test_get_files_by_status_filters_correctly(db_session, normal_user):
    completed = _make_file(db_session, normal_user, status=FileStatus.COMPLETED)
    _make_file(db_session, normal_user, status=FileStatus.PENDING)

    results = db_helpers.get_files_by_status(db_session, normal_user.id, FileStatus.COMPLETED.value)

    assert [f.id for f in results] == [completed.id]


def test_get_files_by_status_no_matches(db_session, normal_user):
    _make_file(db_session, normal_user, status=FileStatus.PENDING)

    results = db_helpers.get_files_by_status(db_session, normal_user.id, FileStatus.ERROR.value)

    assert results == []


# ---------------------------------------------------------------------------
# get_user_file_stats
# ---------------------------------------------------------------------------


def test_get_user_file_stats_aggregates_correctly(db_session, normal_user):
    _make_file(
        db_session,
        normal_user,
        status=FileStatus.COMPLETED,
        content_type="audio/wav",
        file_size=1000,
        duration=10.0,
    )
    _make_file(
        db_session,
        normal_user,
        status=FileStatus.COMPLETED,
        content_type="audio/wav",
        file_size=2000,
        duration=20.0,
    )
    _make_file(
        db_session,
        normal_user,
        status=FileStatus.PENDING,
        content_type="video/mp4",
        file_size=3000,
        duration=None,
    )

    stats = db_helpers.get_user_file_stats(db_session, normal_user.id)

    assert stats["total_files"] == 3
    assert stats["status_distribution"] == {FileStatus.COMPLETED: 2, FileStatus.PENDING: 1}
    assert stats["total_size_bytes"] == 6000
    assert stats["total_duration_seconds"] == 30.0
    assert stats["type_distribution"] == {"audio/wav": 2, "video/mp4": 1}


def test_get_user_file_stats_empty_for_user_with_no_files(db_session, normal_user):
    stats = db_helpers.get_user_file_stats(db_session, normal_user.id)

    assert stats == {
        "total_files": 0,
        "status_distribution": {},
        "total_size_bytes": 0,
        "total_duration_seconds": 0,
        "type_distribution": {},
    }


def test_get_user_file_stats_swallows_query_error(db_session):
    assert db_helpers.get_user_file_stats(db_session, "not-an-int") == {}  # type: ignore[arg-type]  # deliberately wrong type to trigger a DB-level error


def test_get_user_file_stats_leaves_session_usable_after_query_error(db_session, normal_user):
    """Same missing ``db.rollback()`` as ``safe_get_by_id`` — see that test's
    docstring for why the user id is captured before the error rather than read
    off the ORM instance afterward.
    """
    user_id = normal_user.id

    db_helpers.get_user_file_stats(db_session, "not-an-int")  # type: ignore[arg-type]  # deliberately wrong type to trigger a DB-level error

    count = db_session.query(MediaFile).filter(MediaFile.user_id == user_id).count()
    assert count == 0


# ---------------------------------------------------------------------------
# _invalidate_tag_cache_for_file
# ---------------------------------------------------------------------------


def test_invalidate_tag_cache_for_file_swallows_cache_failure(db_session, normal_user, monkeypatch):
    media_file = _make_file(db_session, normal_user)

    from app.services import redis_cache_service

    calls = []

    def _boom(db, file_id):
        calls.append(file_id)
        raise RuntimeError("cache backend unreachable")

    monkeypatch.setattr(
        redis_cache_service.redis_cache, "invalidate_tags_for_file", _boom, raising=True
    )

    # Must not raise: cache invalidation is documented as best-effort.
    db_helpers._invalidate_tag_cache_for_file(db_session, media_file.id)

    # Prove the swallow path was actually exercised, not skipped entirely.
    assert calls == [media_file.id]


def test_invalidate_tag_cache_for_file_calls_real_invalidation(
    db_session, normal_user, monkeypatch
):
    media_file = _make_file(db_session, normal_user)
    calls = []

    from app.services import redis_cache_service

    def _spy(db, file_id):
        calls.append(file_id)

    monkeypatch.setattr(
        redis_cache_service.redis_cache, "invalidate_tags_for_file", _spy, raising=True
    )

    db_helpers._invalidate_tag_cache_for_file(db_session, media_file.id)

    assert calls == [media_file.id]
