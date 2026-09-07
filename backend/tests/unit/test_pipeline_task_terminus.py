"""The cloud-ASR pipeline must not leave its transcription task stuck at 90%.

``transcription/postprocess.py`` deliberately does NOT complete the transcription
task on the cloud-ASR + provider-diarization path. It sets
``in_progress / progress=0.90`` and delegates the terminus to
``extract_speaker_embeddings_task`` via a ``pipeline_task_id`` kwarg.

That delegation was only ever honoured for the *notification*. ``pipeline_task_id``
was read for metering (``run_id=``) and benchmark marks and nothing else, so the
transcription task ROW was never closed — and the notification panel reads its
percentage from that row. Result: "Processing speaker identification — 90%" sat
there forever on a file that had fully finished.

These pin that every terminal path of the embedding task closes the delegating
task, including the two that are easy to forget: the early "no segments" return
and the exception handler.
"""

import contextlib
import uuid
from unittest.mock import patch

import pytest

from app.models.media import MediaFile
from app.models.media import Task
from app.tasks.speaker_embedding_task import _close_pipeline_task
from app.utils.task_utils import update_task_status


@pytest.fixture(autouse=True)
def _use_test_session(db_session):
    """Point the task's own ``session_scope`` at the test session.

    ``_close_pipeline_task`` deliberately opens its own session (it runs inside a
    Celery task, outside any request scope). Under this suite's savepoint
    isolation a separate session cannot see the uncommitted row the test just
    created, so the update would silently log "Task not found" and assert
    nothing — the documented harness trap in ``backend/tests/CLAUDE.md``.
    """

    @contextlib.contextmanager
    def _scope():
        yield db_session

    with patch("app.tasks.speaker_embedding_task.session_scope", _scope):
        yield


@pytest.fixture
def parked_file(db_session, normal_user):
    """A completed media file, as it exists when the transcription task is parked."""
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=normal_user.id,
        filename="terminus_test.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
    )
    db_session.add(media_file)
    db_session.commit()
    return media_file


def _make_task(db, user_id: int, file_id: int, task_id: str) -> Task:
    """A transcription task parked exactly where postprocess leaves it."""
    task = Task(
        id=task_id,
        user_id=user_id,
        media_file_id=file_id,
        task_type="transcription",
        status="in_progress",
        progress=0.90,
    )
    db.add(task)
    db.commit()
    return task


class TestPipelineTaskTerminus:
    def test_closes_the_delegating_transcription_task(self, db_session, normal_user, parked_file):
        task_id = str(uuid.uuid4())
        _make_task(db_session, normal_user.id, parked_file.id, task_id)

        _close_pipeline_task(task_id)

        db_session.expire_all()
        task = db_session.query(Task).filter(Task.id == task_id).first()
        assert task.status == "completed"
        assert task.progress == pytest.approx(1.0)

    def test_marks_the_delegating_task_failed_when_the_run_errored(
        self, db_session, normal_user, parked_file
    ):
        task_id = str(uuid.uuid4())
        _make_task(db_session, normal_user.id, parked_file.id, task_id)

        _close_pipeline_task(task_id, error="embedding model unavailable")

        db_session.expire_all()
        task = db_session.query(Task).filter(Task.id == task_id).first()
        assert task.status == "failed"
        # A failed run must not read as 100% done in the notification panel.
        assert task.progress != pytest.approx(1.0)
        assert "embedding model unavailable" in (task.error_message or "")

    def test_is_a_no_op_when_nothing_was_delegated(self, db_session):
        """The local-ASR path completes its own task and passes no pipeline id."""
        before = db_session.query(Task).count()

        _close_pipeline_task(None)

        assert db_session.query(Task).count() == before

    def test_a_missing_task_row_does_not_raise(self, db_session):
        """A stale/purged id must not take the embedding task down with it."""
        missing_id = str(uuid.uuid4())
        before = db_session.query(Task).count()

        _close_pipeline_task(missing_id)

        # Logged and skipped: no exception, and no row conjured for the id.
        assert db_session.query(Task).filter(Task.id == missing_id).first() is None
        assert db_session.query(Task).count() == before

    def test_update_task_status_actually_moves_a_parked_row(
        self, db_session, normal_user, parked_file
    ):
        """Calibration: proves the 0.90 parked state is reachable and mutable,
        so the assertions above are testing a real transition rather than a
        row that was already completed."""
        task_id = str(uuid.uuid4())
        task = _make_task(db_session, normal_user.id, parked_file.id, task_id)
        assert task.status == "in_progress"
        assert task.progress == pytest.approx(0.90)

        update_task_status(db_session, task_id, "completed", progress=1.0, completed=True)
        db_session.commit()

        db_session.expire_all()
        assert db_session.query(Task).filter(Task.id == task_id).first().status == "completed"
