"""``file_facts`` backfill — the two halves of #383/#403's coverage guarantee.

Files that completed before ``file_facts`` (v390) existed permanently lack it, and
nothing else ever revisits a COMPLETED file. Two things had to change together:

1. ``search_maintenance_task._dispatch_facts_backfill`` — the periodic arm that finds
   those files and dispatches artifact generation for them (this module).
2. ``chat.mapreduce.scope_digest_hits`` — the outer join that COUNTS a file with no
   ``file_facts`` row into ``coverage["files_without_artifacts"]`` instead of an INNER
   JOIN silently dropping it, so a "we covered every file in scope" claim stays honest
   even before the backfill above has caught up on a given file.

These run against the real dev Postgres via the savepoint-isolated ``db_session``
fixture (see ``backend/tests/CLAUDE.md``): both queries scan the WHOLE
``media_file``/``file_facts`` tables with no per-test filter (the backfill arm is
explicitly "all users", and the coverage map is scoped by uuid list, not by owner), so
every assertion here is either a **membership** check on a freshly created row (never an
exact-count/exact-list assertion, which the live dev data would make flaky) or a
**delta** — matching the pattern ``test_stats_helpers.py`` already established for the
same reason.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.core.enums import FileStatus
from app.models.file_facts import FileFacts
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.user import User
from app.services.chat.mapreduce import DigestScopeHits
from app.services.chat.mapreduce import scope_digest_hits
from app.tasks.search_maintenance_task import _dispatch_facts_backfill


def _make_user(db_session) -> User:
    from app.core.security import get_password_hash

    uid = uuid_pkg.uuid4().hex[:10]
    user = User(
        email=f"facts-backfill-fixture-{uid}@example.com",
        full_name="Facts Backfill Fixture",
        hashed_password=get_password_hash("password123"),  # noqa: S106 — throwaway fixture row
        is_active=True,
        role="user",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _make_completed_file(db_session, user_id: int, *, with_segment: bool = True) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=fuuid,
        filename=f"facts-backfill-{fuuid.hex[:8]}.wav",
        storage_path=f"media/facts-backfill/{fuuid}.wav",
        content_type="audio/wav",
        file_size=100,
        user_id=user_id,
        status=FileStatus.COMPLETED,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    if with_segment:
        db_session.add(
            TranscriptSegment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=media_file.id,
                start_time=0.0,
                end_time=1.0,
                text="hello there",
            )
        )
        db_session.commit()

    return media_file


def _make_facts_row(db_session, media_file: MediaFile, *, sections: bool = True) -> FileFacts:
    digest = (
        {
            "sections": [
                {
                    "index": 0,
                    "text": "Section text.",
                    "start_time": 0.0,
                    "end_time": 1.0,
                    "speakers": [],
                }
            ]
        }
        if sections
        else {"sections": []}
    )
    row = FileFacts(
        media_file_id=media_file.id,
        generator_version="2.1.1",
        source_fingerprint="0" * 64,
        language="en",
        facts={},
        digest=digest,
        keyphrases={},
        digest_word_count=2,
        section_count=1 if sections else 0,
    )
    db_session.add(row)
    db_session.commit()
    return row


# ------------------------------------------------------------- the maintenance arm


def test_a_completed_file_with_no_facts_row_is_dispatched(db_session, monkeypatch):
    user = _make_user(db_session)
    media_file = _make_completed_file(db_session, user.id)

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.tasks.ingest_artifacts_task.dispatch_file_facts",
        lambda file_id, pipeline_task_id=None: dispatched.append(file_id),
    )

    stats: dict = {}
    # Our file was JUST created, so its id is the highest of any COMPLETED file with
    # no file_facts row anywhere in the (shared, live) database — batch_size=1 with
    # newest-first ordering guarantees it is exactly the one dispatched, without
    # depending on how large the pre-existing backlog is.
    _dispatch_facts_backfill(db_session, stats, batch_size=1)

    assert dispatched == [media_file.id]
    assert stats["missing_facts_files"] >= 1
    assert stats["facts_backfill_dispatched"] == 1


def test_a_file_that_already_has_facts_is_not_dispatched(db_session, monkeypatch):
    user = _make_user(db_session)
    media_file = _make_completed_file(db_session, user.id)
    _make_facts_row(db_session, media_file)

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.tasks.ingest_artifacts_task.dispatch_file_facts",
        lambda file_id, pipeline_task_id=None: dispatched.append(file_id),
    )

    stats: dict = {}
    # A generous batch so this file would be included if the filter were wrong —
    # asserting non-membership under a tiny batch would prove nothing (it could be
    # cut off by the cap rather than genuinely excluded by the ~has_facts filter).
    _dispatch_facts_backfill(db_session, stats, batch_size=500)

    assert media_file.id not in dispatched


def test_a_completed_file_with_no_segments_is_not_dispatched(db_session, monkeypatch):
    """Mirrors the reindex arm's own ``has_segments`` guard: a file still in PROCESSING
    or one whose transcript was cleared has nothing for ``generate_file_artifacts`` to
    summarise (it returns ``None``), so dispatching it would be pure waste."""
    user = _make_user(db_session)
    media_file = _make_completed_file(db_session, user.id, with_segment=False)

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.tasks.ingest_artifacts_task.dispatch_file_facts",
        lambda file_id, pipeline_task_id=None: dispatched.append(file_id),
    )

    stats: dict = {}
    _dispatch_facts_backfill(db_session, stats, batch_size=500)

    assert media_file.id not in dispatched


def test_the_batch_size_bounds_dispatch_to_the_most_recently_completed_files(
    db_session, monkeypatch
):
    user = _make_user(db_session)
    older = _make_completed_file(db_session, user.id)
    newer = _make_completed_file(db_session, user.id)
    assert newer.id > older.id, "the fixture must produce increasing ids for this to test anything"

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.tasks.ingest_artifacts_task.dispatch_file_facts",
        lambda file_id, pipeline_task_id=None: dispatched.append(file_id),
    )

    stats: dict = {}
    _dispatch_facts_backfill(db_session, stats, batch_size=1)

    assert dispatched == [newer.id], "newest-first ordering means only `newer` fits in the cap"
    assert stats["facts_backfill_dispatched"] == 1
    # The true backlog (uncapped) is at least the 2 files just created, even though
    # only 1 was dispatched this tick — the cap must not lie about how much is left.
    assert stats["missing_facts_files"] >= 2


def test_a_zero_batch_size_dispatches_nothing_even_with_a_real_backlog(db_session, monkeypatch):
    """Control for the zero-dispatch branch: create a genuine backlog entry (a
    completed file with a segment and no facts row) and cap the batch at 0. Proves the
    guard is the batch size, not an accidental "nothing to do" — ``missing_facts_files``
    must still report the backlog while ``facts_backfill_dispatched`` stays 0."""
    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.tasks.ingest_artifacts_task.dispatch_file_facts",
        lambda file_id, pipeline_task_id=None: dispatched.append(file_id),
    )
    user = _make_user(db_session)
    _make_completed_file(db_session, user.id)  # a real backlog entry, deliberately unfetched

    stats: dict = {}
    _dispatch_facts_backfill(db_session, stats, batch_size=0)

    assert dispatched == []
    assert stats["facts_backfill_dispatched"] == 0
    assert stats["missing_facts_files"] >= 1


# --------------------------------------------------------- scope_digest_hits coverage


def test_a_file_with_no_facts_row_is_counted_not_dropped(db_session):
    """THE regression this exists to prevent: an INNER JOIN made a file with no
    ``file_facts`` row vanish from the map with no signal at all — indistinguishable
    from the file never having been in scope. The outer join must find it and report
    it in ``coverage["files_without_artifacts"]``."""
    user = _make_user(db_session)
    missing = _make_completed_file(db_session, user.id)
    present = _make_completed_file(db_session, user.id)
    _make_facts_row(db_session, present)

    hits = scope_digest_hits(db_session, [str(missing.uuid), str(present.uuid)])

    assert isinstance(hits, DigestScopeHits)
    assert hits.coverage["files_without_artifacts"] == 1
    assert all(hit.file_uuid != str(missing.uuid) for hit in hits)
    assert any(hit.file_uuid == str(present.uuid) for hit in hits)


def test_a_file_with_an_empty_digest_is_not_counted_as_missing_artifacts(db_session):
    """A `file_facts` row with zero sections (e.g. a 10-second clip, a real, documented
    outcome per `ingest_artifacts/CLAUDE.md`) is NOT the same gap as no row at all — it
    was covered, it just had nothing to say. Only a genuinely absent row counts."""
    user = _make_user(db_session)
    media_file = _make_completed_file(db_session, user.id)
    _make_facts_row(db_session, media_file, sections=False)

    hits = scope_digest_hits(db_session, [str(media_file.uuid)])

    assert hits.coverage["files_without_artifacts"] == 0
    assert list(hits) == []


def test_every_file_covered_reports_zero_missing(db_session):
    """Control: guards against a detector that always reports a gap."""
    user = _make_user(db_session)
    media_file = _make_completed_file(db_session, user.id)
    _make_facts_row(db_session, media_file)

    hits = scope_digest_hits(db_session, [str(media_file.uuid)])

    assert hits.coverage["files_without_artifacts"] == 0
    assert len(hits) == 1


def test_an_empty_scope_reports_zero_without_touching_the_database(db_session):
    hits = scope_digest_hits(db_session, [])
    assert hits == []
    assert hits.coverage == {"files_without_artifacts": 0}


@pytest.mark.parametrize("sections_per_file", [1, 2])
def test_coverage_survives_a_nonstandard_sections_per_file(db_session, sections_per_file):
    """Guard the guard: coverage must not depend on the caller's ``sections_per_file``."""
    user = _make_user(db_session)
    missing = _make_completed_file(db_session, user.id)

    hits = scope_digest_hits(db_session, [str(missing.uuid)], sections_per_file=sections_per_file)

    assert hits.coverage["files_without_artifacts"] == 1
