"""Superseded task rows are resolved, and a file never holds two active rows of a step (#1185).

A late writer from a superseded run could leave a task row ``in_progress`` forever after a newer
run of the same step completed for the same file. These tests drive the real rows through:

* the recovery sweep and the one predicate shared with the ``task_rows_superseded_active`` gauge,
* ``create_task_record`` meeting the database's one-active-row index (``v448``) by superseding,
* the two paths that reopen a finished row, which must give way to a newer active run,
* the health check actually running the sweep, first.

Every "is resolved" case has a sibling that must be left alone, so a sweep that fails everything
(or nothing) cannot pass.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services import task_row_supersede
from app.services.task_detection_service import RECOVERED_AFTER_STUCK
from app.services.task_recovery_service import TaskRecoveryService
from app.services.task_row_supersede import count_superseded_active_rows
from app.services.task_row_supersede import is_active_row_conflict
from app.services.task_row_supersede import resolve_superseded_task_rows
from app.services.task_row_supersede import superseded_active_rows
from app.utils import task_utils
from app.utils.task_utils import create_task_record
from app.utils.task_utils import update_task_status
from tests.conftest import engine
from tests.user_owned_rows import make_media_file
from tests.user_owned_rows import make_user

_T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _file(db, user, **kwargs) -> MediaFile:
    media_file = MediaFile(
        user_id=user.id,
        filename=f"sup-{uuid.uuid4().hex[:8]}.wav",
        storage_path=f"user_{user.id}/{uuid.uuid4().hex[:8]}.wav",
        file_size=1024,
        content_type="audio/wav",
        status=FileStatus.COMPLETED,
        **kwargs,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _task(db, user, media_file, task_type, status, day, task_id=None) -> Task:
    task = Task(
        id=task_id or f"sup-{uuid.uuid4().hex[:10]}",
        user_id=user.id,
        media_file_id=None if media_file is None else media_file.id,
        task_type=task_type,
        status=status,
        progress=0.5,
        created_at=_T0 + timedelta(days=day),
        updated_at=_T0 + timedelta(days=day),
    )
    db.add(task)
    db.commit()
    return task


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _status(db, task_id: str) -> tuple[str, str | None]:
    db.expire_all()
    row = db.get(Task, task_id)
    return str(row.status), row.error_message


# =============================================================================
# the predicate and the sweep
# =============================================================================
def test_the_sweep_fails_a_row_a_newer_completed_row_superseded(db_session, normal_user):
    media_file = _file(db_session, normal_user)
    stuck = _task(db_session, normal_user, media_file, "summarization", "in_progress", 0)
    done = _task(db_session, normal_user, media_file, "summarization", "completed", 1)
    media_file.active_task_id = stuck.id
    db_session.commit()
    before = _sample("task_rows_superseded_total", {"task_type": "summarization"})

    result = resolve_superseded_task_rows(db_session)

    assert result == {"found": 1, "resolved": 1}
    assert _status(db_session, stuck.id) == ("failed", f"superseded by {done.id}")
    assert db_session.get(Task, stuck.id).completed_at is not None
    assert _status(db_session, done.id)[0] == "completed"
    db_session.refresh(media_file)
    assert media_file.active_task_id is None
    assert _sample("task_rows_superseded_total", {"task_type": "summarization"}) == before + 1


def test_pending_rows_are_superseded_too(db_session, normal_user):
    media_file = _file(db_session, normal_user)
    waiting = _task(db_session, normal_user, media_file, "analytics", "pending", 0)
    done = _task(db_session, normal_user, media_file, "analytics", "completed", 1)

    resolve_superseded_task_rows(db_session)

    assert _status(db_session, waiting.id) == ("failed", f"superseded by {done.id}")


def test_the_newest_completed_row_is_the_one_named(db_session, normal_user):
    media_file = _file(db_session, normal_user)
    stuck = _task(db_session, normal_user, media_file, "summarization", "in_progress", 0)
    _task(db_session, normal_user, media_file, "summarization", "completed", 1)
    newest = _task(db_session, normal_user, media_file, "summarization", "completed", 2)

    resolve_superseded_task_rows(db_session)

    assert _status(db_session, stuck.id)[1] == f"superseded by {newest.id}"


def test_rows_that_are_not_superseded_are_left_alone(db_session, normal_user):
    """Each is an active row the sweep must NOT touch; the first case would be the dangerous one."""
    media_file = _file(db_session, normal_user)
    other_file = _file(db_session, normal_user)
    # a completed row that is OLDER than the active one: the active run is the newer
    newer_active = _task(db_session, normal_user, media_file, "summarization", "in_progress", 5)
    _task(db_session, normal_user, media_file, "summarization", "completed", 1)
    # completed rows of another step / another file / a failed or skipped newer row
    other_step = _task(db_session, normal_user, media_file, "analytics", "in_progress", 0)
    _task(db_session, normal_user, media_file, "topic_extraction", "completed", 9)
    other_file_row = _task(db_session, normal_user, other_file, "analytics", "in_progress", 0)
    failed_newer = _task(db_session, normal_user, other_file, "rediarize", "in_progress", 0)
    _task(db_session, normal_user, other_file, "rediarize", "failed", 3)
    skipped_newer = _task(db_session, normal_user, other_file, "speaker_attribute", "pending", 0)
    _task(db_session, normal_user, other_file, "speaker_attribute", "skipped", 3)
    # corpus-wide rows have no file to be superseded within
    no_file = _task(db_session, normal_user, None, "search_reembed", "in_progress", 0)
    _task(db_session, normal_user, None, "search_reembed", "completed", 9)
    untouched = [newer_active, other_step, other_file_row, failed_newer, skipped_newer, no_file]

    assert superseded_active_rows(db_session) == []
    result = resolve_superseded_task_rows(db_session)

    assert result == {"found": 0, "resolved": 0}
    for row in untouched:
        assert _status(db_session, row.id)[0] in ("in_progress", "pending"), row.task_type


def test_the_gauge_counts_exactly_what_the_sweep_resolves(db_session, normal_user):
    """One predicate: an alert on the gauge always names rows the sweep can act on."""
    from app.core.backup_metrics import update_task_row_metrics

    media_file = _file(db_session, normal_user)
    for step in ("summarization", "analytics", "topic_extraction"):
        _task(db_session, normal_user, media_file, step, "in_progress", 0)
        _task(db_session, normal_user, media_file, step, "completed", 1)
    other = _file(db_session, normal_user)
    _task(db_session, normal_user, other, "analytics", "in_progress", 0)  # not superseded
    # the rows other tests committed to the shared database count too, so compare deltas
    baseline = count_superseded_active_rows(db_session)
    assert baseline >= 3

    update_task_row_metrics(db_session)
    assert _sample("task_rows_superseded_active") == baseline
    assert len(superseded_active_rows(db_session)) == baseline

    resolve_superseded_task_rows(db_session, limit=10_000)
    update_task_row_metrics(db_session)
    assert _sample("task_rows_superseded_active") == 0


def test_a_row_finished_between_the_read_and_the_write_is_not_clobbered(db_session, normal_user):
    media_file = _file(db_session, normal_user)
    row = _task(db_session, normal_user, media_file, "summarization", "completed", 0)
    before = _sample("task_rows_superseded_total", {"task_type": "summarization"})

    # the sweep read it as active, then its own task finished it
    with patch.object(
        task_row_supersede,
        "superseded_active_rows",
        return_value=[(row.id, "summarization", "someone-newer")],
    ):
        result = resolve_superseded_task_rows(db_session)

    assert result == {"found": 1, "resolved": 0}
    assert _status(db_session, row.id) == ("completed", None)
    assert _sample("task_rows_superseded_total", {"task_type": "summarization"}) == before


def test_the_sweep_is_bounded_and_drains_across_runs(db_session, normal_user):
    media_file = _file(db_session, normal_user)
    for step in ("summarization", "analytics", "topic_extraction"):
        _task(db_session, normal_user, media_file, step, "in_progress", 0)
        _task(db_session, normal_user, media_file, step, "completed", 1)
    start = count_superseded_active_rows(db_session)

    first = resolve_superseded_task_rows(db_session, limit=1)

    assert first == {"found": 1, "resolved": 1}
    assert count_superseded_active_rows(db_session) == start - 1


def test_the_oldest_superseded_rows_are_resolved_first(db_session, normal_user):
    """A bounded sweep drains oldest first, so a backlog cannot starve its longest-stuck rows."""
    media_file = _file(db_session, normal_user)
    mine = {}
    # inserted newest first, so physical order is the opposite of the order we expect
    for step, day in (("topic_extraction", 20), ("analytics", 10), ("summarization", 0)):
        mine[step] = _task(db_session, normal_user, media_file, step, "in_progress", day).id
        _task(db_session, normal_user, media_file, step, "completed", day + 1)

    everything = [row[0] for row in superseded_active_rows(db_session)]
    ours = [tid for tid in everything if tid in mine.values()]

    assert ours == [mine["summarization"], mine["analytics"], mine["topic_extraction"]]
    assert [row[0] for row in superseded_active_rows(db_session, limit=1)] == everything[:1]


# =============================================================================
# the guard meets the writers
# =============================================================================
def test_a_new_row_supersedes_the_files_active_row_of_the_same_step(db_session, normal_user):
    media_file = _file(db_session, normal_user)
    older = create_task_record(
        db_session, "sup-old", normal_user.id, media_file.id, "summarization"
    )
    before = _sample("task_rows_superseded_total", {"task_type": "summarization"})

    newer = create_task_record(
        db_session, "sup-new", normal_user.id, media_file.id, "summarization"
    )

    assert _status(db_session, older.id) == ("failed", "superseded by sup-new")
    assert _status(db_session, newer.id) == ("pending", None)
    db_session.refresh(media_file)
    assert media_file.active_task_id == "sup-new"
    assert _sample("task_rows_superseded_total", {"task_type": "summarization"}) == before + 1


def test_creating_a_row_leaves_other_steps_files_and_finished_rows_alone(db_session, normal_user):
    media_file = _file(db_session, normal_user)
    other_file = _file(db_session, normal_user)
    other_step = create_task_record(db_session, "sup-a", normal_user.id, media_file.id, "analytics")
    other_file_row = create_task_record(
        db_session, "sup-b", normal_user.id, other_file.id, "summarization"
    )
    finished = _task(db_session, normal_user, media_file, "summarization", "completed", 0)

    create_task_record(db_session, "sup-c", normal_user.id, media_file.id, "summarization")

    assert _status(db_session, other_step.id)[0] == "pending"
    assert _status(db_session, other_file_row.id)[0] == "pending"
    assert _status(db_session, finished.id) == ("completed", None)


def test_corpus_wide_rows_may_overlap(db_session, normal_user):
    first = create_task_record(db_session, "sup-n1", normal_user.id, None, "search_reembed")
    second = create_task_record(db_session, "sup-n2", normal_user.id, None, "search_reembed")

    assert _status(db_session, first.id)[0] == "pending"
    assert _status(db_session, second.id)[0] == "pending"


# The next three drive ``create_task_record``'s ROLLBACK path. ``db_session``'s savepoint harness
# cannot host them: its ``rollback()`` discards the row committed one statement earlier (the
# redelivery case fails identically on the unmodified code). So they use real, committed rows in
# their own sessions and remove them in the fixture teardown.
_Real = sessionmaker(bind=engine)


@pytest.fixture
def committed_world():
    setup = _Real()
    user = make_user(setup, "taskrows")
    media_file = make_media_file(setup, user.id)
    ids = (int(user.id), int(media_file.id))
    setup.close()
    try:
        yield ids
    finally:
        cleanup = _Real()
        cleanup.execute(text("DELETE FROM task WHERE media_file_id = :f"), {"f": ids[1]})
        cleanup.execute(text("DELETE FROM media_file WHERE id = :f"), {"f": ids[1]})
        cleanup.execute(text('DELETE FROM "user" WHERE id = :u'), {"u": ids[0]})
        cleanup.commit()
        cleanup.close()


def _committed_status(task_id: str) -> tuple[str, str | None] | None:
    check = _Real()
    try:
        row = check.execute(
            text("SELECT status, error_message FROM task WHERE id = :i"), {"i": task_id}
        ).one_or_none()
        return None if row is None else (row[0], row[1])
    finally:
        check.close()


def test_a_redelivered_task_id_still_reuses_its_row(committed_world):
    """The primary-key collision is a different situation from the index and keeps its answer."""
    user_id, file_id = committed_world
    first_session, second_session = _Real(), _Real()  # the redelivery is another process
    try:
        first = create_task_record(first_session, "sup-same", user_id, file_id, "analytics")

        again = create_task_record(second_session, "sup-same", user_id, file_id, "analytics")

        assert again.id == first.id
    finally:
        first_session.close()
        second_session.close()
    assert _committed_status("sup-same") == ("pending", None)


def test_a_creator_that_loses_the_race_supersedes_the_winner_and_lands(committed_world):
    """The rival committed between our supersede and our insert: the index refuses, we retry."""
    user_id, file_id = committed_world
    s = _Real()
    try:
        create_task_record(s, "sup-rival", user_id, file_id, "summarization")
        real = task_utils.supersede_active_siblings
        calls: list[int] = []

        def blind_first_time(*args, **kwargs):
            calls.append(1)
            return [] if len(calls) == 1 else real(*args, **kwargs)

        with patch.object(task_utils, "supersede_active_siblings", side_effect=blind_first_time):
            create_task_record(s, "sup-late", user_id, file_id, "summarization")
    finally:
        s.close()

    assert len(calls) == 2
    assert _committed_status("sup-rival") == ("failed", "superseded by sup-late")
    assert _committed_status("sup-late") == ("pending", None)


def test_a_conflict_that_never_clears_is_raised_not_swallowed(committed_world):
    user_id, file_id = committed_world
    s = _Real()
    try:
        create_task_record(s, "sup-holder", user_id, file_id, "summarization")

        with patch.object(task_utils, "supersede_active_siblings", return_value=[]):
            with pytest.raises(IntegrityError) as err:
                create_task_record(s, "sup-never", user_id, file_id, "summarization")
    finally:
        s.close()

    assert is_active_row_conflict(err.value)
    assert _committed_status("sup-never") is None
    assert _committed_status("sup-holder") == ("pending", None)


def test_any_other_integrity_error_is_not_mistaken_for_the_index(committed_world):
    _, file_id = committed_world
    s = _Real()
    try:
        # a user that does not exist: a foreign key violation, the pre-existing "cannot create" path
        with pytest.raises(ValueError, match="Failed to create or find task"):
            create_task_record(s, "sup-fk", 2_000_000_000, file_id, "analytics")
    finally:
        s.close()


def test_a_finished_row_is_not_reopened_beside_a_newer_active_one(db_session, normal_user):
    media_file = _file(db_session, normal_user)
    old = _task(db_session, normal_user, media_file, "summarization", "failed", 0)
    newer = _task(db_session, normal_user, media_file, "summarization", "in_progress", 1)

    assert update_task_status(db_session, old.id, "in_progress", progress=0.3) is None

    assert _status(db_session, old.id)[0] == "failed"
    assert _status(db_session, newer.id)[0] == "in_progress"


def test_a_retry_still_reopens_its_own_finished_row_when_nothing_else_is_active(
    db_session, normal_user
):
    """The control for the case above: a task that fails then self.retry()s must still reopen."""
    media_file = _file(db_session, normal_user)
    row = _task(db_session, normal_user, media_file, "summarization", "failed", 0)

    reopened = update_task_status(db_session, row.id, "in_progress", progress=0.3)

    assert reopened is not None and reopened.status == "in_progress"
    assert _status(db_session, row.id)[0] == "in_progress"


def test_two_reopens_racing_give_way_instead_of_crashing(db_session, normal_user):
    """The slot looked free to the guard's read; the index, checked at the UPDATE, refuses."""
    media_file = _file(db_session, normal_user)
    row = _task(db_session, normal_user, media_file, "summarization", "failed", 0)
    _task(db_session, normal_user, media_file, "summarization", "in_progress", 1)

    # Neutralise the slot check so the UPDATE is exactly what a racing writer issues: it runs
    # while the other run is not yet visible to it. Only the index is left to refuse it.
    real_select = task_row_supersede.select

    def slot_check_that_sees_nothing(*args, **kwargs):
        return real_select(Task.id).where(Task.id == "no-such-task")

    with patch.object(task_row_supersede, "select", slot_check_that_sees_nothing):
        reopened = task_row_supersede.reopen_if_slot_free(
            db_session, row, {"status": "in_progress", "updated_at": datetime.now(UTC)}
        )

    assert reopened is False


def test_the_false_positive_reset_skips_a_row_whose_step_has_a_newer_active_run(
    db_session, normal_user
):
    media_file = _file(db_session, normal_user)
    skipped = _task(db_session, normal_user, media_file, "summarization", "failed", 0)
    _task(db_session, normal_user, media_file, "summarization", "in_progress", 1)
    other_file = _file(db_session, normal_user)
    eligible = _task(db_session, normal_user, other_file, "summarization", "failed", 0)
    for row in (skipped, eligible):
        row.error_message = RECOVERED_AFTER_STUCK
    db_session.commit()

    stats = TaskRecoveryService().recover_false_positive_failed_tasks(
        db_session, [skipped, eligible]
    )

    assert stats == {"tasks_reset": 1}
    assert _status(db_session, skipped.id)[0] == "failed"
    assert _status(db_session, eligible.id)[0] == "pending"


# =============================================================================
# the sweep is wired into the existing recovery, first
# =============================================================================
@contextmanager
def _session_of(db):
    yield db


def _run_health_check(db_session, sweep) -> list[str]:
    from app.tasks import recovery

    order: list[str] = []

    def stuck(_db):
        order.append("stuck-tasks")
        raise RuntimeError("stop after the first step")

    def swept(db):
        order.append("sweep")
        return sweep(db)

    with (
        patch.object(recovery, "session_scope", lambda: _session_of(db_session)),
        patch.object(recovery, "resolve_superseded_task_rows", swept),
        patch.object(recovery.task_detection_service, "identify_stuck_tasks", stuck),
        patch.object(recovery, "_check_opensearch_health", lambda summary: None),
    ):
        recovery.periodic_health_check_task()
    return order


def test_the_health_check_runs_the_sweep_before_stuck_task_recovery(db_session):
    order = _run_health_check(db_session, lambda db: {"found": 2, "resolved": 2})

    assert order == ["sweep", "stuck-tasks"]


def test_a_failing_sweep_does_not_stop_the_rest_of_recovery(db_session):
    def broken(db):
        raise RuntimeError("sweep exploded")

    order = _run_health_check(db_session, broken)

    assert order == ["sweep", "stuck-tasks"]


# =============================================================================
# a refused or pointless reopen (review findings on #1185)
# =============================================================================
def _false_positive(db, user, media_file, day, other=None):
    row = _task(db, user, media_file, "summarization", "failed", day)
    row.error_message = RECOVERED_AFTER_STUCK
    db.commit()
    return row


def test_a_reopen_the_index_refuses_does_not_undo_the_resets_staged_before_it(
    db_session, normal_user
):
    """The batch is one transaction: the loser's rollback must take only its own UPDATE."""
    eligible = _false_positive(db_session, normal_user, _file(db_session, normal_user), 0)
    raced_file = _file(db_session, normal_user)
    raced = _false_positive(db_session, normal_user, raced_file, 0)
    _task(db_session, normal_user, raced_file, "summarization", "in_progress", 1)

    # The slot check sees nothing, as it does when the other run is not yet visible to it; only
    # the unique index is left to refuse the second reopen (as in the racing-reopens test).
    real_select = task_row_supersede.select

    def slot_check_that_sees_nothing(*args, **kwargs):
        return real_select(Task.id).where(Task.id == "no-such-task")

    with patch.object(task_row_supersede, "select", slot_check_that_sees_nothing):
        stats = TaskRecoveryService().recover_false_positive_failed_tasks(
            db_session, [eligible, raced]
        )

    assert stats == {"tasks_reset": 1}
    assert _status(db_session, eligible.id)[0] == "pending", "the earlier reset was rolled back"
    assert _status(db_session, raced.id)[0] == "failed"


def test_the_false_positive_reset_skips_a_row_a_newer_completed_run_superseded(
    db_session, normal_user
):
    """Reopening it only hands the supersede sweep a row to fail again on its next run."""
    media_file = _file(db_session, normal_user)
    old = _false_positive(db_session, normal_user, media_file, 0)
    done = _task(db_session, normal_user, media_file, "summarization", "completed", 1)

    stats = TaskRecoveryService().recover_false_positive_failed_tasks(db_session, [old])

    assert stats == {"tasks_reset": 0}
    assert _status(db_session, old.id)[0] == "failed"
    assert old.id not in {r[0] for r in superseded_active_rows(db_session)}
    assert _status(db_session, done.id)[0] == "completed"


def test_an_older_completed_run_does_not_stop_the_false_positive_reset(db_session, normal_user):
    """Control for the skip above: only a NEWER completed sibling supersedes a row."""
    media_file = _file(db_session, normal_user)
    _task(db_session, normal_user, media_file, "summarization", "completed", -1)
    row = _false_positive(db_session, normal_user, media_file, 0)

    stats = TaskRecoveryService().recover_false_positive_failed_tasks(db_session, [row])

    assert stats == {"tasks_reset": 1}
    assert _status(db_session, row.id)[0] == "pending"


def test_a_late_write_cannot_reopen_a_row_a_newer_completed_run_superseded(db_session, normal_user):
    media_file = _file(db_session, normal_user)
    old = _task(db_session, normal_user, media_file, "summarization", "failed", 0)
    _task(db_session, normal_user, media_file, "summarization", "completed", 1)

    assert update_task_status(db_session, old.id, "in_progress", progress=0.3) is None

    assert _status(db_session, old.id)[0] == "failed"
