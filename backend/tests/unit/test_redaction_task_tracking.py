"""Task-row visibility for redaction_detect_task (issue #622).

Before this, ``redaction_detect_task`` never called ``create_task_record`` —
like ``detect_speaker_attributes_task``, a run had no status, no duration, and
nothing in the Tasks UI/API. These tests assert on the real ``Task`` row the
task leaves behind (issue #431's standard: a mocked
``assert_called_once_with`` proves the code called a function, not that it
left the state a human debugging the Tasks UI would see), using
``.apply(...)`` so ``self.request.id`` is a real value rather than the
default-context ``None``.
"""

from __future__ import annotations

import uuid as uuid_mod
from contextlib import contextmanager

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.models.media import TranscriptSegment
from app.tasks.redaction_task import redaction_detect_task


@contextmanager
def _real_scope(db_session):
    """A `session_scope` stand-in bound to the test's savepoint session.

    Without this, `redaction_detect_task`'s own (lazily-imported)
    `session_scope()` opens a brand new `SessionLocal()` on a separate
    connection, which cannot see anything written through `db_session` until
    the test's outer transaction commits — it never does, so the task sees an
    empty database and every fixture-created file reads back as
    "file_not_found".
    """
    try:
        yield db_session
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise


def _make_completed_file_with_segments(db_session, user, *, segments: int = 2):
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename="redact.mp4",
        storage_path="test/redact.mp4",
        content_type="video/mp4",
        file_size=1000,
        status=FileStatus.COMPLETED,
    )
    db_session.add(media_file)
    db_session.flush()

    for i in range(segments):
        db_session.add(
            TranscriptSegment(
                uuid=str(uuid_mod.uuid4()),
                media_file_id=media_file.id,
                start_time=float(i) * 4.0,
                end_time=float(i) * 4.0 + 4.0,
                text="call me at 555-123-4567",
            )
        )
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _make_reprocessing_file_without_segments(db_session, user):
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename="redact-reproc.mp4",
        storage_path="test/redact-reproc.mp4",
        content_type="video/mp4",
        file_size=1000,
        status=FileStatus.PROCESSING,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _task_row_for(db_session, media_file_id: int) -> Task | None:
    db_session.expire_all()
    return (  # type: ignore[no-any-return]
        db_session.query(Task).filter(Task.media_file_id == media_file_id).first()
    )


@pytest.fixture
def redaction_env(db_session, monkeypatch):
    """Route the task's DB access through the test session; stub the WS notify.

    The WebSocket notification is a real side path this suite doesn't cover.
    """
    monkeypatch.setattr("app.db.session_utils.session_scope", lambda: _real_scope(db_session))
    monkeypatch.setattr("app.tasks.redaction_task._notify", lambda *a, **kw: None)


def test_normal_completion_creates_and_completes_task_row(
    db_session, normal_user, redaction_env, monkeypatch
):
    """(a) A real detection run must leave a completed, matched Task row."""
    media_file = _make_completed_file_with_segments(db_session, normal_user)
    monkeypatch.setattr(
        "app.services.redaction.service.RedactionService.detect_and_store",
        lambda db, file_id: {"status": "success", "segments": 2, "pii_entities_found": 1},
    )

    result = redaction_detect_task.apply(
        args=[media_file.id], kwargs={"user_id": normal_user.id}
    ).get()

    assert result["status"] == "success", result

    task = _task_row_for(db_session, media_file.id)
    assert task is not None, "no Task row was created — issue #622 regression"
    assert task.task_type == "redaction_detection"
    assert task.status == "completed"
    assert task.progress == 1.0
    assert task.completed_at is not None
    assert task.user_id == normal_user.id


def test_no_segments_skip_path_creates_skipped_task_row(
    db_session, normal_user, redaction_env, monkeypatch
):
    """(b) The mid-reprocess guard must still leave an honest, visible Task row."""
    media_file = _make_reprocessing_file_without_segments(db_session, normal_user)
    called = {"detect": False}

    def fake_detect(db, file_id):
        called["detect"] = True
        return {"status": "success"}

    monkeypatch.setattr(
        "app.services.redaction.service.RedactionService.detect_and_store", fake_detect
    )

    result = redaction_detect_task.apply(
        args=[media_file.id], kwargs={"user_id": normal_user.id}
    ).get()

    assert result == {"status": "skipped", "reason": "no_segments"}
    assert called["detect"] is False, "the guard did not actually skip detection"

    task = _task_row_for(db_session, media_file.id)
    assert task is not None, "the no_segments skip path left no Task row"
    assert task.status == "skipped"
    assert task.completed_at is not None


def test_file_not_found_creates_no_task_row(db_session, normal_user, redaction_env):
    """A nonexistent file_id must not produce a Task row: media_file_id is a
    foreign key, and there is nothing for it to reference."""
    result = redaction_detect_task.apply(
        args=[999_999_999], kwargs={"user_id": normal_user.id}
    ).get()

    assert result == {"status": "skipped", "reason": "file_not_found"}

    orphans = db_session.query(Task).filter(Task.media_file_id == 999_999_999).all()
    assert orphans == []


def test_final_failure_after_retries_exhausted_creates_failed_task_row(
    db_session, normal_user, redaction_env, monkeypatch
):
    """(d) Once retries are exhausted, the row must read failed with the error."""
    media_file = _make_completed_file_with_segments(db_session, normal_user)

    def boom(db, file_id):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("app.services.redaction.service.RedactionService.detect_and_store", boom)

    # `retries=2` simulates the LAST attempt: max_retries is also 2, so
    # `self.request.retries < self.max_retries` is False and the task must
    # give up rather than call `self.retry()` again.
    result = redaction_detect_task.apply(
        args=[media_file.id], kwargs={"user_id": normal_user.id}, retries=2
    ).get()

    assert result["status"] == "failed"
    assert "provider unavailable" in result["error"]

    task = _task_row_for(db_session, media_file.id)
    assert task is not None, "a failed run left no Task row"
    assert task.status == "failed"
    assert task.error_message and "provider unavailable" in task.error_message
    assert task.completed_at is not None


def test_same_task_id_across_two_sessions_does_not_duplicate_task_row():
    """A retry keeps Celery's task_id stable across attempts — it re-runs the
    SAME task id after its countdown, on a fresh session, rather than
    dispatching a new one. This proves `create_task_record`'s own
    IntegrityError-and-reuse path (`app/utils/task_utils.py`) is what keeps
    that from producing a second row, using two genuinely independent
    `SessionLocal()` sessions — one per simulated attempt — which is what two
    real retry attempts actually get (`redaction_detect_task` opens its own
    `session_scope()` fresh on every invocation; it is never the same Python
    session object twice). A single shared, savepoint-nested test session
    (`db_session`) cannot stand in for that: reusing one session across two
    "attempts" hits SQLAlchemy identity-map/savepoint interactions that a
    real retry, on two distinct connections, never encounters — confirmed by
    reproducing this exact scenario against a live Postgres outside the test
    harness before writing it this way.

    This test therefore manages and cleans up its own real rows — including
    its own `User` — rather than relying on `db_session`'s savepoint
    rollback: a user created via the `normal_user` fixture lives only inside
    `db_session`'s own uncommitted savepoint and is invisible to any other,
    genuinely independent connection.
    """
    import uuid as uuid_helper

    from app.db.base import SessionLocal
    from app.models.user import User
    from app.utils.task_utils import create_task_record

    setup_session = SessionLocal()
    user_id: int | None = None
    media_file_id: int | None = None
    try:
        user = User(
            email=f"redaction-retry-{uuid_helper.uuid4()}@example.com",
            full_name="Redaction Retry Test User",
            hashed_password="not-a-real-hash",
            is_active=True,
            is_superuser=False,
            role="user",
        )
        setup_session.add(user)
        setup_session.commit()
        user_id = user.id

        media_file = MediaFile(
            uuid=str(uuid_mod.uuid4()),
            user_id=user_id,
            filename="redact-retry.mp4",
            storage_path="test/redact-retry.mp4",
            content_type="video/mp4",
            file_size=1000,
            status=FileStatus.COMPLETED,
        )
        setup_session.add(media_file)
        setup_session.commit()
        media_file_id = media_file.id

        fixed_task_id = f"retry-fixed-{uuid_mod.uuid4()}"

        # Read `.id` back while each session is still open — SQLAlchemy expires
        # attributes on commit by default, and refreshing an expired attribute
        # after the owning session has closed raises DetachedInstanceError.
        session_a = SessionLocal()
        try:
            first = create_task_record(
                session_a, fixed_task_id, user_id, media_file_id, "redaction_detection"
            )
            first_id = first.id
        finally:
            session_a.close()

        session_b = SessionLocal()
        try:
            second = create_task_record(
                session_b, fixed_task_id, user_id, media_file_id, "redaction_detection"
            )
            second_id = second.id
        finally:
            session_b.close()

        assert first_id == second_id == fixed_task_id

        rows = setup_session.query(Task).filter(Task.media_file_id == media_file_id).all()
        assert len(rows) == 1, f"the same task_id produced {len(rows)} Task rows, expected 1"
    finally:
        if media_file_id is not None:
            setup_session.query(Task).filter(Task.media_file_id == media_file_id).delete(
                synchronize_session=False
            )
            setup_session.query(MediaFile).filter(MediaFile.id == media_file_id).delete()
        if user_id is not None:
            setup_session.query(User).filter(User.id == user_id).delete()
        setup_session.commit()
        setup_session.close()
