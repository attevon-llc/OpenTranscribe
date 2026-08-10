"""Behavioral tests for accepting and rejecting auto-labeled tags.

Covers ``app/services/tag_review.py``. The scenario that decides whether this
code is correct is **reject on a tag that also carries hand-applied
associations**: ``Tag.source`` records which path created the row *first*, not
who endorsed it, so an auto-labeled tag can accumulate any amount of manual
tagging. A reject implemented as "delete the tag" destroys that work. Reject
therefore keys on ``FileTag.source == TAG_SOURCE_AUTO_AI`` and removes the tag
row only once no association is left.

``Tag.source`` is nullable and migration v230 never backfilled it, so every tag
predating auto-labeling carries NULL. NULL is manual: such a tag is not awaiting
review and cannot be accepted.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.constants import TAG_SOURCE_AI_ACCEPTED
from app.core.constants import TAG_SOURCE_AUTO_AI
from app.core.constants import TAG_SOURCE_MANUAL
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag
from app.models.watch_source import WatchSource
from app.services.tag_operations import TagNotFoundError
from app.services.tag_review import REVIEW_ACCEPTED
from app.services.tag_review import REVIEW_NOT_APPLICABLE
from app.services.tag_review import REVIEW_REJECTED
from app.services.tag_review import accept_tags
from app.services.tag_review import preview_tag_review
from app.services.tag_review import reject_tags
from app.services.tag_service import normalize_tag_name


def _suffix() -> str:
    """Tag names are globally unique — every created name needs its own suffix."""
    return uuid.uuid4().hex[:8]


def _make_tag(
    db_session, name: str, *, source: str | None = TAG_SOURCE_AUTO_AI, user_id: int | None = None
) -> Tag:
    """Create a tag. ``user_id=None`` makes a **system** tag — pass an owner
    unless the test is specifically about the shared vocabulary."""
    tag = Tag(name=name, user_id=user_id, source=source, normalized_name=normalize_tag_name(name))
    db_session.add(tag)
    db_session.flush()
    return tag


def _make_file(db_session, owner) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="tag_review_test.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _attach(db_session, media_file, tag, *, source=TAG_SOURCE_AUTO_AI, confidence=None) -> FileTag:
    link = FileTag(
        media_file_id=media_file.id,
        tag_id=tag.id,
        source=source,
        ai_confidence=confidence,
    )
    db_session.add(link)
    db_session.flush()
    return link


def _links_for(db_session, tag) -> list[FileTag]:
    return db_session.query(FileTag).filter(FileTag.tag_id == tag.id).all()


def _entry(report, tag) -> object:
    matches = [entry for entry in report.tags if entry.uuid == tag.uuid]
    assert matches, f"tag {tag.name} missing from {report.tags}"
    return matches[0]


# ---------------------------------------------------------------------------
# Accept
# ---------------------------------------------------------------------------


def test_accept_marks_the_tag_accepted_and_leaves_associations_alone(db_session, normal_user):
    """Accepting endorses the tag row; it is not an operation on files."""
    tag = _make_tag(db_session, f"podcast-{_suffix()}", user_id=normal_user.id)
    media_file = _make_file(db_session, normal_user)
    _attach(db_session, media_file, tag, confidence=0.8)

    report = accept_tags(db_session, [tag.id], user_id=normal_user.id)

    db_session.refresh(tag)
    assert tag.source == TAG_SOURCE_AI_ACCEPTED
    assert _entry(report, tag).outcome == REVIEW_ACCEPTED
    links = _links_for(db_session, tag)
    assert len(links) == 1
    assert links[0].source == TAG_SOURCE_AUTO_AI, "accept must not rewrite association origins"


def test_accepted_tag_no_longer_reads_as_awaiting_review(db_session, normal_user):
    """Awaiting-review is the query for the auto-labeler's own origin value."""
    tag = _make_tag(db_session, f"awaiting-{_suffix()}", user_id=normal_user.id)
    assert tag.source == TAG_SOURCE_AUTO_AI

    accept_tags(db_session, [tag.id], user_id=normal_user.id)

    db_session.refresh(tag)
    awaiting = (
        db_session.query(Tag).filter(Tag.id == tag.id, Tag.source == TAG_SOURCE_AUTO_AI).first()
    )
    assert awaiting is None


def test_accepting_a_manual_tag_is_not_applicable(db_session, normal_user):
    """A manual tag was never awaiting review, so there is nothing to accept."""
    tag = _make_tag(
        db_session, f"manual-{_suffix()}", source=TAG_SOURCE_MANUAL, user_id=normal_user.id
    )

    report = accept_tags(db_session, [tag.id], user_id=normal_user.id)

    db_session.refresh(tag)
    assert tag.source == TAG_SOURCE_MANUAL
    assert _entry(report, tag).outcome == REVIEW_NOT_APPLICABLE


def test_accepting_a_null_origin_tag_is_not_applicable(db_session, normal_user):
    """Legacy rows carry NULL; NULL is manual, not "unknown, try it"."""
    tag = _make_tag(db_session, f"legacy-{_suffix()}", source=None, user_id=normal_user.id)

    report = accept_tags(db_session, [tag.id], user_id=normal_user.id)

    db_session.refresh(tag)
    assert tag.source is None
    assert _entry(report, tag).outcome == REVIEW_NOT_APPLICABLE


def test_accepting_an_already_accepted_tag_is_not_applicable(db_session, normal_user):
    """Endorsement is not repeatable — an accepted tag left the review set already."""
    tag = _make_tag(
        db_session, f"endorsed-{_suffix()}", source=TAG_SOURCE_AI_ACCEPTED, user_id=normal_user.id
    )

    report = accept_tags(db_session, [tag.id], user_id=normal_user.id)

    assert _entry(report, tag).outcome == REVIEW_NOT_APPLICABLE


def test_bulk_accept_over_a_mixed_selection_reports_per_tag_outcomes(db_session, normal_user):
    """The list allows multi-select across filters, so a selection mixes origins."""
    suffix = _suffix()
    auto = _make_tag(db_session, f"auto-{suffix}", user_id=normal_user.id)
    manual = _make_tag(
        db_session, f"manual-{suffix}", source=TAG_SOURCE_MANUAL, user_id=normal_user.id
    )
    legacy = _make_tag(db_session, f"legacy-{suffix}", source=None, user_id=normal_user.id)

    report = accept_tags(db_session, [auto.id, manual.id, legacy.id], user_id=normal_user.id)

    assert _entry(report, auto).outcome == REVIEW_ACCEPTED
    assert _entry(report, manual).outcome == REVIEW_NOT_APPLICABLE
    assert _entry(report, legacy).outcome == REVIEW_NOT_APPLICABLE
    db_session.refresh(auto)
    db_session.refresh(manual)
    assert auto.source == TAG_SOURCE_AI_ACCEPTED
    assert manual.source == TAG_SOURCE_MANUAL


def test_review_of_an_unknown_tag_fails_loudly(db_session, normal_user):
    tag = _make_tag(db_session, f"vanishing-{_suffix()}", user_id=normal_user.id)
    missing_id = tag.id
    db_session.delete(tag)
    db_session.flush()

    with pytest.raises(TagNotFoundError):
        accept_tags(db_session, [missing_id], user_id=normal_user.id)


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


def test_reject_removes_the_tag_when_every_association_was_auto_labeled(db_session, normal_user):
    """Nothing human is left behind, so the tag row goes with its associations."""
    tag = _make_tag(db_session, f"allauto-{_suffix()}", user_id=normal_user.id)
    for _ in range(3):
        _attach(db_session, _make_file(db_session, normal_user), tag, confidence=0.9)

    report = reject_tags(db_session, [tag.id], user_id=normal_user.id)

    entry = _entry(report, tag)
    assert entry.outcome == REVIEW_REJECTED
    assert entry.removed_association_count == 3
    assert entry.retained_association_count == 0
    assert entry.tag_removed is True
    assert db_session.query(Tag).filter(Tag.id == tag.id).first() is None
    assert db_session.query(FileTag).filter(FileTag.tag_id == tag.id).count() == 0


def test_reject_keeps_hand_applied_associations_and_the_tag(db_session, normal_user):
    """The whole point: a reject may not destroy manual tagging work.

    ``Tag.source`` says the auto-labeler created the row first — it says nothing
    about who applied it to each file. Deleting the tag here would strip it from
    every file a person tagged by hand.
    """
    tag = _make_tag(db_session, f"mixed-{_suffix()}", user_id=normal_user.id)
    auto_files = [_make_file(db_session, normal_user) for _ in range(2)]
    manual_files = [_make_file(db_session, normal_user) for _ in range(3)]
    for media_file in auto_files:
        _attach(db_session, media_file, tag, source=TAG_SOURCE_AUTO_AI, confidence=0.7)
    for media_file in manual_files:
        _attach(db_session, media_file, tag, source=TAG_SOURCE_MANUAL)

    report = reject_tags(db_session, [tag.id], user_id=normal_user.id)

    assert db_session.query(Tag).filter(Tag.id == tag.id).first() is not None, (
        "reject deleted a tag that still carries hand-applied associations"
    )
    surviving = _links_for(db_session, tag)
    assert {link.media_file_id for link in surviving} == {f.id for f in manual_files}
    entry = _entry(report, tag)
    assert entry.removed_association_count == 2
    assert entry.retained_association_count == 3
    assert entry.tag_removed is False


def test_reject_retains_legacy_null_origin_associations(db_session, normal_user):
    """A NULL association origin predates auto-labeling — it is a human's row."""
    tag = _make_tag(db_session, f"nullassoc-{_suffix()}", user_id=normal_user.id)
    legacy_file = _make_file(db_session, normal_user)
    auto_file = _make_file(db_session, normal_user)
    _attach(db_session, legacy_file, tag, source=None)
    _attach(db_session, auto_file, tag, source=TAG_SOURCE_AUTO_AI)

    report = reject_tags(db_session, [tag.id], user_id=normal_user.id)

    surviving = _links_for(db_session, tag)
    assert [link.media_file_id for link in surviving] == [legacy_file.id]
    assert db_session.query(Tag).filter(Tag.id == tag.id).first() is not None
    assert _entry(report, tag).retained_association_count == 1


def test_reject_of_a_manual_tag_is_not_applicable_and_touches_nothing(db_session, normal_user):
    """Only the auto-labeler's own tags are reviewable."""
    tag = _make_tag(
        db_session, f"handmade-{_suffix()}", source=TAG_SOURCE_MANUAL, user_id=normal_user.id
    )
    media_file = _make_file(db_session, normal_user)
    _attach(db_session, media_file, tag, source=TAG_SOURCE_AUTO_AI)

    report = reject_tags(db_session, [tag.id], user_id=normal_user.id)

    assert _entry(report, tag).outcome == REVIEW_NOT_APPLICABLE
    assert len(_links_for(db_session, tag)) == 1
    assert db_session.query(Tag).filter(Tag.id == tag.id).first() is not None


def test_bulk_reject_over_a_mixed_selection_rejects_only_the_eligible_tags(db_session, normal_user):
    suffix = _suffix()
    auto = _make_tag(db_session, f"bulkauto-{suffix}", user_id=normal_user.id)
    manual = _make_tag(
        db_session, f"bulkmanual-{suffix}", source=TAG_SOURCE_MANUAL, user_id=normal_user.id
    )
    _attach(db_session, _make_file(db_session, normal_user), auto)
    _attach(db_session, _make_file(db_session, normal_user), manual, source=TAG_SOURCE_AUTO_AI)

    report = reject_tags(db_session, [auto.id, manual.id], user_id=normal_user.id)

    assert _entry(report, auto).outcome == REVIEW_REJECTED
    assert _entry(report, manual).outcome == REVIEW_NOT_APPLICABLE
    assert db_session.query(Tag).filter(Tag.id == auto.id).first() is None
    assert len(_links_for(db_session, manual)) == 1
    assert auto.uuid in report.deleted_uuids
    assert manual.uuid not in report.deleted_uuids


def test_reject_strips_the_removed_name_from_watch_sources(db_session, normal_user):
    """A stored name would recreate the tag on the next poll."""
    tag = _make_tag(db_session, f"polled-{_suffix()}", user_id=normal_user.id)
    _attach(db_session, _make_file(db_session, normal_user), tag)
    watch = WatchSource(
        name=f"watch-{_suffix()}",
        source_type="local",
        local_path=f"/watch/{_suffix()}",
        user_id=normal_user.id,
        tag_names=["keepme", tag.name],
    )
    db_session.add(watch)
    db_session.flush()

    reject_tags(db_session, [tag.id], user_id=normal_user.id)

    db_session.refresh(watch)
    assert watch.tag_names == ["keepme"]


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def test_reject_preview_reports_removed_and_retained_separately_without_applying(
    db_session, normal_user
):
    """A caller cannot judge a reject from one number — both halves are reported."""
    tag = _make_tag(db_session, f"preview-{_suffix()}", user_id=normal_user.id)
    for _ in range(4):
        _attach(db_session, _make_file(db_session, normal_user), tag)
    for _ in range(2):
        _attach(db_session, _make_file(db_session, normal_user), tag, source=TAG_SOURCE_MANUAL)

    report = preview_tag_review(db_session, [tag.id], action="reject", user_id=normal_user.id)

    assert report.applied is False
    assert report.removed_association_count == 4
    assert report.retained_association_count == 2
    entry = _entry(report, tag)
    assert entry.removed_association_count == 4
    assert entry.retained_association_count == 2
    assert entry.tag_removed is False
    # Nothing was applied.
    assert len(_links_for(db_session, tag)) == 6
    assert db_session.query(Tag).filter(Tag.id == tag.id).first() is not None


def test_reject_preview_counts_only_the_files_that_lose_the_tag(
    db_session, normal_user, other_user
):
    """The U3 impact shape, narrowed to the associations a reject actually removes."""
    tag = _make_tag(db_session, f"scoped-{_suffix()}", user_id=normal_user.id)
    mine = _make_file(db_session, normal_user)
    theirs = _make_file(db_session, other_user)
    untouched = _make_file(db_session, normal_user)
    _attach(db_session, mine, tag)
    _attach(db_session, theirs, tag)
    _attach(db_session, untouched, tag, source=TAG_SOURCE_MANUAL)

    report = preview_tag_review(db_session, [tag.id], action="reject", user_id=normal_user.id)

    assert report.impact.total_file_count == 2
    assert report.impact.accessible_file_count == 1


def test_preview_rejects_an_unknown_action(db_session, normal_user):
    """Guessing would report an accept's numbers in front of a reject."""
    tag = _make_tag(db_session, f"badaction-{_suffix()}", user_id=normal_user.id)

    with pytest.raises(ValueError, match="Unknown tag review action"):
        preview_tag_review(db_session, [tag.id], action="discard", user_id=normal_user.id)


def test_accept_preview_applies_nothing(db_session, normal_user):
    tag = _make_tag(db_session, f"acceptpreview-{_suffix()}", user_id=normal_user.id)

    report = preview_tag_review(db_session, [tag.id], action="accept", user_id=normal_user.id)

    assert report.applied is False
    assert _entry(report, tag).outcome == REVIEW_ACCEPTED
    db_session.refresh(tag)
    assert tag.source == TAG_SOURCE_AUTO_AI


# ---------------------------------------------------------------------------
# The shared cache + search-refresh hook
# ---------------------------------------------------------------------------


class _RecordingTask:
    """Stands in for the Celery task so the enqueued payload is observable."""

    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    def delay(self, file_ids):  # noqa: ANN001,ANN202 - mirrors Task.delay
        self.calls.append(list(file_ids))


@pytest.fixture
def recorded_reindex(monkeypatch):
    from app.tasks import search_indexing_task

    recorder = _RecordingTask()
    monkeypatch.setattr(search_indexing_task, "update_file_tags_index", recorder)
    return recorder


class _RecordingCache:
    """Captures the cache busts ``on_tags_changed`` performs."""

    def __init__(self) -> None:
        self.global_busts = 0
        self.users: list[int] = []

    def invalidate_tags_global(self) -> None:
        self.global_busts += 1

    def invalidate_tags(self, user_id: int) -> None:
        self.users.append(user_id)

    def invalidate_user_files(self, user_id: int) -> None:
        pass


@pytest.fixture
def recorded_cache(monkeypatch):
    from app.services import redis_cache_service

    recorder = _RecordingCache()
    monkeypatch.setattr(redis_cache_service, "redis_cache", recorder)
    return recorder


def test_reject_refreshes_the_search_documents_of_the_files_it_stripped(
    db_session, normal_user, recorded_reindex
):
    """Without the refresh the index keeps serving a tag the file no longer has."""
    tag = _make_tag(db_session, f"reindex-{_suffix()}", user_id=normal_user.id)
    stripped = _make_file(db_session, normal_user)
    kept = _make_file(db_session, normal_user)
    _attach(db_session, stripped, tag, source=TAG_SOURCE_AUTO_AI)
    _attach(db_session, kept, tag, source=TAG_SOURCE_MANUAL)

    reject_tags(db_session, [tag.id], user_id=normal_user.id)

    assert recorded_reindex.calls == [[stripped.id]]


def test_accept_busts_the_tag_cache_without_enqueuing_a_reindex(
    db_session, normal_user, recorded_reindex, recorded_cache
):
    """Accept changes the tag row, not which tags files carry."""
    tag = _make_tag(db_session, f"cachebust-{_suffix()}", user_id=normal_user.id)
    _attach(db_session, _make_file(db_session, normal_user), tag)

    accept_tags(db_session, [tag.id], user_id=normal_user.id)

    # An *owned* tag is in one account's list, so dropping every account's key
    # would be a keyspace-wide sweep to publish a change nobody else can see.
    assert recorded_cache.global_busts == 0
    assert normal_user.id in recorded_cache.users
    assert recorded_reindex.calls == []


def test_accept_on_a_system_tag_busts_every_users_cache(
    db_session, normal_user, recorded_reindex, recorded_cache
):
    """The one case that earns the keyspace-wide drop.

    A system tag (``user_id IS NULL``) appears in every account's list, so
    nothing narrower than a global bust leaves the others correct — they would
    read the stale row until ``TTL_TAGS`` expired.
    """
    tag = _make_tag(db_session, f"cachebust-sys-{_suffix()}", user_id=None)
    _attach(db_session, _make_file(db_session, normal_user), tag)

    accept_tags(db_session, [tag.id], user_id=normal_user.id)

    assert recorded_cache.global_busts == 1
