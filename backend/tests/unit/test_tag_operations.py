"""Behavioral tests for tag rename / merge / delete and name-reference rewriting.

Covers ``app/services/tag_operations.py``. The scenarios that matter here are
the ones a naive implementation gets wrong:

* ``file_tag`` carries ``UNIQUE(media_file_id, tag_id)``, so merging two tags
  that share a file cannot be a bare ``UPDATE file_tag SET tag_id = survivor``
  — that is an ``IntegrityError`` on exactly the near-duplicate pairs merge
  exists for.
* ``Tag.source`` / ``FileTag.source`` are nullable and were never backfilled, so
  every tag predating auto-labeling carries NULL. NULL has to compare as manual
  or the promotion ordering is undefined for the very rows being merged.
* ``WatchSource.tag_names`` stores tag *names*, reapplied on every poll. Merge a
  tag away without rewriting them and the next poll recreates it.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event

from app.core.constants import TAG_SOURCE_AI_ACCEPTED
from app.core.constants import TAG_SOURCE_AUTO_AI
from app.core.constants import TAG_SOURCE_MANUAL
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag
from app.models.watch_source import WatchSource
from app.services.tag_operations import TagNotFoundError
from app.services.tag_operations import delete_tags
from app.services.tag_operations import merge_tags
from app.services.tag_operations import preview_tag_impact
from app.services.tag_operations import rename_tag
from app.services.tag_service import normalize_tag_name


def _suffix() -> str:
    """Tag names are globally unique — every created name needs its own suffix."""
    return uuid.uuid4().hex[:8]


def _make_tag(db_session, name: str, *, source: str | None = TAG_SOURCE_MANUAL) -> Tag:
    tag = Tag(name=name, source=source, normalized_name=normalize_tag_name(name))
    db_session.add(tag)
    db_session.flush()
    return tag


def _make_file(db_session, owner) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="tag_ops_test.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _attach(db_session, media_file, tag, *, source=TAG_SOURCE_MANUAL, confidence=None) -> FileTag:
    link = FileTag(
        media_file_id=media_file.id,
        tag_id=tag.id,
        source=source,
        ai_confidence=confidence,
    )
    db_session.add(link)
    db_session.flush()
    return link


def _links_for(db_session, media_file, tag) -> list[FileTag]:
    return (
        db_session.query(FileTag)
        .filter(FileTag.media_file_id == media_file.id, FileTag.tag_id == tag.id)
        .all()
    )


def _make_watch_source(db_session, owner, tag_names: list[str]) -> WatchSource:
    source = WatchSource(
        name=f"watch-{_suffix()}",
        source_type="local",
        local_path=f"/watch/{_suffix()}",
        user_id=owner.id,
        tag_names=tag_names,
    )
    db_session.add(source)
    db_session.flush()
    return source


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def test_merge_moves_every_association_and_removes_the_source(db_session, normal_user):
    """Every file carrying the source tag ends up carrying the survivor."""
    suffix = _suffix()
    survivor = _make_tag(db_session, f"interview-{suffix}")
    doomed = _make_tag(db_session, f"interviews-{suffix}")
    first = _make_file(db_session, normal_user)
    second = _make_file(db_session, normal_user)
    _attach(db_session, first, doomed)
    _attach(db_session, second, doomed)

    merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)

    assert len(_links_for(db_session, first, survivor)) == 1
    assert len(_links_for(db_session, second, survivor)) == 1
    assert db_session.query(Tag).filter(Tag.id == doomed.id).first() is None


def test_merge_collapses_a_file_that_carries_both_tags(db_session, normal_user):
    """A file holding both tags keeps exactly one association — no IntegrityError.

    ``file_tag`` has ``UNIQUE(media_file_id, tag_id)``; reassigning the source
    association onto the survivor would collide with the row already there.
    """
    suffix = _suffix()
    survivor = _make_tag(db_session, f"budget-{suffix}")
    doomed = _make_tag(db_session, f"budgets-{suffix}")
    shared = _make_file(db_session, normal_user)
    _attach(db_session, shared, survivor)
    _attach(db_session, shared, doomed)

    merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)

    assert len(_links_for(db_session, shared, survivor)) == 1
    assert db_session.query(Tag).filter(Tag.id == doomed.id).first() is None


def test_collapsed_association_keeps_more_human_origin_and_higher_confidence(
    db_session, normal_user
):
    """Collapsing two associations must not lose the human one or the better score."""
    suffix = _suffix()
    survivor = _make_tag(db_session, f"legal-{suffix}", source=TAG_SOURCE_AUTO_AI)
    doomed = _make_tag(db_session, f"legals-{suffix}")
    shared = _make_file(db_session, normal_user)
    _attach(db_session, shared, survivor, source=TAG_SOURCE_AUTO_AI, confidence=0.62)
    _attach(db_session, shared, doomed, source=TAG_SOURCE_MANUAL, confidence=0.91)

    merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)

    kept = _links_for(db_session, shared, survivor)
    assert len(kept) == 1
    assert kept[0].source == TAG_SOURCE_MANUAL
    assert kept[0].ai_confidence == pytest.approx(0.91)


def test_merge_into_auto_labeler_tag_marks_the_survivor_accepted(db_session, normal_user):
    """A person merged into it, so the auto-labeled survivor is now endorsed."""
    suffix = _suffix()
    survivor = _make_tag(db_session, f"roadmap-{suffix}", source=TAG_SOURCE_AUTO_AI)
    doomed = _make_tag(db_session, f"roadmaps-{suffix}")

    merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)

    db_session.refresh(survivor)
    assert survivor.source == TAG_SOURCE_AI_ACCEPTED


def test_merge_into_manual_tag_leaves_it_manual(db_session, normal_user):
    """Endorsement only promotes an auto-labeled tag — manual is already human."""
    suffix = _suffix()
    survivor = _make_tag(db_session, f"hiring-{suffix}", source=TAG_SOURCE_MANUAL)
    doomed = _make_tag(db_session, f"hirings-{suffix}")

    merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)

    db_session.refresh(survivor)
    assert survivor.source == TAG_SOURCE_MANUAL


def test_merge_treats_null_origin_as_manual(db_session, normal_user):
    """Tags predating auto-labeling carry NULL — it must order as manual, not crash."""
    suffix = _suffix()
    survivor = _make_tag(db_session, f"legacy-{suffix}", source=TAG_SOURCE_AUTO_AI)
    doomed = _make_tag(db_session, f"legacies-{suffix}", source=None)
    shared = _make_file(db_session, normal_user)
    _attach(db_session, shared, survivor, source=TAG_SOURCE_AUTO_AI, confidence=0.5)
    _attach(db_session, shared, doomed, source=None)

    merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)

    kept = _links_for(db_session, shared, survivor)
    assert len(kept) == 1
    assert kept[0].source == TAG_SOURCE_MANUAL, "a NULL origin must outrank auto_ai"


def test_merge_of_three_tags_succeeds_in_one_call(db_session, normal_user):
    """Cleaning up a family of near-duplicates is one operation, not three."""
    suffix = _suffix()
    survivor = _make_tag(db_session, f"q3-{suffix}")
    doomed = [_make_tag(db_session, f"q3-report-{suffix}-{n}") for n in range(3)]
    media_file = _make_file(db_session, normal_user)
    for tag in doomed:
        _attach(db_session, media_file, tag)

    outcome = merge_tags(db_session, survivor.id, [t.id for t in doomed], user_id=normal_user.id)

    assert outcome.merged is True
    assert len(_links_for(db_session, media_file, survivor)) == 1
    assert db_session.query(Tag).filter(Tag.id.in_([t.id for t in doomed])).count() == 0


def test_opposite_direction_merges_do_not_both_destroy_the_survivor(db_session, normal_user):
    """The second, now-stale merge fails loudly instead of deleting the winner."""
    suffix = _suffix()
    left = _make_tag(db_session, f"left-{suffix}")
    right = _make_tag(db_session, f"right-{suffix}")

    merge_tags(db_session, right.id, [left.id], user_id=normal_user.id)

    with pytest.raises(TagNotFoundError):
        merge_tags(db_session, left.id, [right.id], user_id=normal_user.id)

    assert db_session.query(Tag).filter(Tag.id == right.id).first() is not None


def test_merge_locks_tag_rows_in_id_order(db_session, normal_user):
    """Deterministic lock order is what stops two opposite merges deadlocking."""
    suffix = _suffix()
    survivor = _make_tag(db_session, f"lock-{suffix}")
    doomed = _make_tag(db_session, f"locks-{suffix}")
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    # Listen on the session's own Connection: the engine-level dispatch is
    # snapshotted when a Connection is created, so a listener added to the
    # engine afterwards never fires for this already-open connection.
    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", _capture)
    try:
        merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)
    finally:
        event.remove(connection, "before_cursor_execute", _capture)

    locking = [s for s in statements if "FOR UPDATE" in s.upper() and "FROM tag" in s]
    assert locking, f"merge never locked the tag rows: {statements}"
    assert any("ORDER BY tag.id" in s for s in locking), locking


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------


def test_rename_to_unused_name_updates_name_and_normalization(db_session, normal_user):
    """A plain rename refreshes the stored normalized form too, or dedup breaks."""
    tag = _make_tag(db_session, f"olde-name-{_suffix()}")
    new_name = f"New_Name-{_suffix()}"

    outcome = rename_tag(db_session, tag.id, new_name, user_id=normal_user.id)

    assert outcome.merged is False
    assert outcome.requires_confirmation is False
    db_session.refresh(tag)
    assert tag.name == new_name
    assert tag.normalized_name == normalize_tag_name(new_name)


def test_rename_onto_existing_tag_previews_impact_without_applying(db_session, normal_user):
    """Renaming onto another tag is a merge — it needs confirmation first."""
    suffix = _suffix()
    target = _make_tag(db_session, f"Interview-{suffix}")
    tag = _make_tag(db_session, f"interviewing-{suffix}")
    media_file = _make_file(db_session, normal_user)
    _attach(db_session, media_file, tag)

    outcome = rename_tag(db_session, tag.id, target.name.upper(), user_id=normal_user.id)

    assert outcome.requires_confirmation is True
    assert outcome.merged is False
    assert outcome.impact.total_file_count == 1
    db_session.refresh(tag)
    assert tag.name == f"interviewing-{suffix}", "the rename was applied without confirmation"
    assert db_session.query(Tag).filter(Tag.id == target.id).first() is not None


def test_confirmed_rename_onto_existing_tag_merges(db_session, normal_user):
    """With confirmation the rename becomes the merge it always was."""
    suffix = _suffix()
    target = _make_tag(db_session, f"Interview-{suffix}")
    tag = _make_tag(db_session, f"interviewing-{suffix}")
    media_file = _make_file(db_session, normal_user)
    _attach(db_session, media_file, tag)

    outcome = rename_tag(
        db_session, tag.id, target.name, confirm_merge=True, user_id=normal_user.id
    )

    assert outcome.merged is True
    assert db_session.query(Tag).filter(Tag.id == tag.id).first() is None
    assert len(_links_for(db_session, media_file, target)) == 1


def test_rename_to_a_case_variant_of_its_own_name_is_a_plain_rename(db_session, normal_user):
    """``interview`` → ``Interview`` matches only itself; that is not a self-merge."""
    name = f"interview-{_suffix()}"
    tag = _make_tag(db_session, name)

    outcome = rename_tag(db_session, tag.id, name.upper(), user_id=normal_user.id)

    assert outcome.merged is False
    assert outcome.requires_confirmation is False
    db_session.refresh(tag)
    assert tag.name == name.upper()
    assert db_session.query(Tag).filter(Tag.id == tag.id).first() is not None


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_reports_affected_file_count_before_removing_anything(db_session, normal_user):
    """The caller learns the blast radius from the same call that applies it."""
    tag = _make_tag(db_session, f"doomed-{_suffix()}")
    first = _make_file(db_session, normal_user)
    second = _make_file(db_session, normal_user)
    _attach(db_session, first, tag)
    _attach(db_session, second, tag)

    outcome = delete_tags(db_session, [tag.id], user_id=normal_user.id)

    assert outcome.impact.total_file_count == 2
    assert outcome.impact.accessible_file_count == 2
    assert db_session.query(Tag).filter(Tag.id == tag.id).first() is None
    assert db_session.query(FileTag).filter(FileTag.tag_id == tag.id).count() == 0


# ---------------------------------------------------------------------------
# Impact preview
# ---------------------------------------------------------------------------


def test_impact_preview_separates_accessible_from_global_counts(
    db_session, normal_user, other_user
):
    """A preview reading "1 file" must not precede a delete that strips it from 2."""
    tag = _make_tag(db_session, f"shared-{_suffix()}")
    mine = _make_file(db_session, normal_user)
    theirs = _make_file(db_session, other_user)
    _attach(db_session, mine, tag)
    _attach(db_session, theirs, tag)

    report = preview_tag_impact(db_session, [tag.id], user_id=normal_user.id)

    assert report.accessible_file_count == 1
    assert report.total_file_count == 2
    assert len(report.tags) == 1
    assert report.tags[0].accessible_file_count == 1
    assert report.tags[0].total_file_count == 2
    assert report.tags[0].uuid == tag.uuid


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


def test_merge_refreshes_the_search_documents_of_the_moved_files(
    db_session, normal_user, recorded_reindex
):
    """Without the refresh the index keeps serving the tag that no longer exists."""
    suffix = _suffix()
    survivor = _make_tag(db_session, f"reindex-{suffix}")
    doomed = _make_tag(db_session, f"reindexes-{suffix}")
    moved = _make_file(db_session, normal_user)
    untouched = _make_file(db_session, normal_user)
    _attach(db_session, moved, doomed)
    _attach(db_session, untouched, survivor)

    merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)

    assert recorded_reindex.calls == [[moved.id]]


def test_rename_refreshes_the_search_documents_of_its_files(
    db_session, normal_user, recorded_reindex
):
    tag = _make_tag(db_session, f"renamed-{_suffix()}")
    media_file = _make_file(db_session, normal_user)
    _attach(db_session, media_file, tag)

    rename_tag(db_session, tag.id, f"renamed-again-{_suffix()}", user_id=normal_user.id)

    assert recorded_reindex.calls == [[media_file.id]]


def test_delete_refreshes_the_search_documents_of_its_files(
    db_session, normal_user, recorded_reindex
):
    tag = _make_tag(db_session, f"deleted-{_suffix()}")
    media_file = _make_file(db_session, normal_user)
    _attach(db_session, media_file, tag)

    delete_tags(db_session, [tag.id], user_id=normal_user.id)

    assert recorded_reindex.calls == [[media_file.id]]


# ---------------------------------------------------------------------------
# Stored name references (WatchSource.tag_names)
# ---------------------------------------------------------------------------


def test_merge_rewrites_watch_source_tag_names_and_the_value_persists(db_session, normal_user):
    """``tag_names`` is plain JSON — an in-place edit silently fails to persist."""
    suffix = _suffix()
    survivor = _make_tag(db_session, f"weekly-{suffix}")
    doomed = _make_tag(db_session, f"weeklies-{suffix}")
    watch = _make_watch_source(db_session, normal_user, ["keepme", doomed.name])

    merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)

    db_session.commit()
    db_session.expire_all()
    db_session.refresh(watch)
    assert watch.tag_names == ["keepme", survivor.name]


def test_merge_rewrites_a_case_variant_stored_on_a_watch_source(db_session, normal_user):
    """Entries predating normalization are matched on their normalized form."""
    suffix = _suffix()
    survivor = _make_tag(db_session, f"weekly-{suffix}")
    doomed = _make_tag(db_session, f"weeklies-{suffix}")
    watch = _make_watch_source(db_session, normal_user, [doomed.name.upper()])

    merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)

    db_session.refresh(watch)
    assert watch.tag_names == [survivor.name]


def test_rename_rewrites_stored_watch_source_names(db_session, normal_user):
    tag = _make_tag(db_session, f"before-{_suffix()}")
    watch = _make_watch_source(db_session, normal_user, ["alpha", tag.name, "omega"])
    new_name = f"after-{_suffix()}"

    rename_tag(db_session, tag.id, new_name, user_id=normal_user.id)

    db_session.refresh(watch)
    assert watch.tag_names == ["alpha", new_name, "omega"]


def test_delete_strips_the_entry_and_leaves_the_others_in_order(db_session, normal_user):
    tag = _make_tag(db_session, f"gone-{_suffix()}")
    watch = _make_watch_source(db_session, normal_user, ["alpha", tag.name, "omega"])

    delete_tags(db_session, [tag.id], user_id=normal_user.id)

    db_session.refresh(watch)
    assert watch.tag_names == ["alpha", "omega"]


def test_watch_source_without_a_reference_is_left_untouched(db_session, normal_user):
    suffix = _suffix()
    survivor = _make_tag(db_session, f"unrelated-{suffix}")
    doomed = _make_tag(db_session, f"unrelateds-{suffix}")
    watch = _make_watch_source(db_session, normal_user, ["alpha", "omega"])

    merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)

    db_session.refresh(watch)
    assert watch.tag_names == ["alpha", "omega"]
