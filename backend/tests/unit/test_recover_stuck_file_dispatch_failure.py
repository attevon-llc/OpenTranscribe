"""``task_utils.recover_stuck_file`` — a dispatch failure one frame down (inside
``_start_transcription_task``) must not vanish into a bare ``logger.error`` (issue #906).

``_recover_failed_file``/``_recover_orphaned_file`` COMMIT a state change (retry reset,
or PENDING + cleared task pointers) *before* they call ``_start_transcription_task``. If
that dispatch then raises, ``recover_stuck_file``'s outer ``except`` used to log and
return ``False`` with no other trace: ``recovery_attempts`` was never incremented, so a
repeatedly-failing file looks exactly like one that was never attempted. It now records
the attempt on the way out via ``db.rollback()`` + a fresh read + ``_update_recovery_
tracking``. The file's own ``last_error_message`` is deliberately NOT written here —
S0/#865 already own that, at the point ``dispatch_transcription_pipeline`` itself fails.

**Three of these four tests manage their own real ``SessionLocal()`` rows, rather than
the savepoint-nested ``db_session`` fixture.** ``Session.rollback()`` (no args) "always
rolls back the topmost database transaction, discarding any nested transactions" — that
is SQLAlchemy's own documented behaviour, not a bug. Under ``db_session``, EVERY
"commit" in a test (including the fixture's own row setup) is really just a SAVEPOINT
release inside one never-truly-committed outer transaction, so the ``db.rollback()`` this
fix calls unwinds ALL the way back to the start of the test, wiping the very row
``_recover_orphaned_file``/``_recover_failed_file`` just committed — confirmed by
reproducing it against the live Postgres before writing it this way (same shape as
``test_redaction_task_tracking.py``'s ``test_same_task_id_across_two_sessions_does_not_
duplicate_task_row``, which documents the identical trap). In production this is a
non-issue: a prior commit there is a REAL commit, immune to a later rollback. Only the
CONTROL test (success path, `db.rollback()` never runs) uses the ordinary fixture.
"""

from __future__ import annotations

import uuid

from sqlalchemy import update

from app.db.base import SessionLocal
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User

#: A message ``categorize_error`` maps to NETWORK_ERROR, which the default retry
#: budget allows for a first attempt — precondition for the ERROR-status case below.
_RETRIABLE_ERROR = "Connection timeout while downloading"


def _fail_dispatch(monkeypatch) -> None:
    """``_start_transcription_task`` imports ``dispatch_transcription_pipeline`` fresh
    from ``app.tasks.transcription`` on every call — patch it there, at source."""
    import app.tasks.transcription as transcription_pkg

    monkeypatch.delenv("SKIP_CELERY", raising=False)
    monkeypatch.setattr(
        transcription_pkg,
        "dispatch_transcription_pipeline",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("broker down")),
    )


class _RealRow:
    """A real, independently-committed User + MediaFile, cleaned up manually.

    Not the ``db_session``/``normal_user`` fixtures — see the module docstring for why.
    """

    def __init__(self, **media_file_overrides):
        self.session = SessionLocal()
        self.user_id: int | None = None
        self.media_file_id: int | None = None

        user = User(
            email=f"recover-dispatch-fail-{uuid.uuid4()}@example.com",
            full_name="Recover Dispatch Failure Test User",
            hashed_password="not-a-real-hash",  # noqa: S106 — throwaway fixture row
            is_active=True,
            is_superuser=False,
            role="user",
        )
        self.session.add(user)
        self.session.commit()
        self.user_id = user.id

        defaults = {
            "uuid": str(uuid.uuid4()),
            "user_id": self.user_id,
            "filename": f"f-{uuid.uuid4().hex[:8]}.wav",
            "storage_path": f"user_{self.user_id}/{uuid.uuid4().hex[:8]}.wav",
            "file_size": 1024,
            "content_type": "audio/wav",
        }
        defaults.update(media_file_overrides)
        media_file = MediaFile(**defaults)
        self.session.add(media_file)
        self.session.commit()
        self.media_file_id = media_file.id

    def null_out(self, *columns: str) -> None:
        """Set ``columns`` to SQL NULL with an explicit UPDATE, then prove it landed."""
        self.session.execute(
            update(MediaFile)
            .where(MediaFile.id == self.media_file_id)
            .values(**dict.fromkeys(columns))
        )
        self.session.commit()
        row = self.session.get(MediaFile, self.media_file_id)
        self.session.refresh(row)
        for column in columns:
            assert getattr(row, column) is None, f"MediaFile.{column} did not land as NULL"

    @property
    def id(self) -> int:
        assert self.media_file_id is not None
        return self.media_file_id

    def refresh(self) -> MediaFile:
        row = self.session.query(MediaFile).filter(MediaFile.id == self.media_file_id).one()
        self.session.refresh(row)
        return row

    def cleanup(self) -> None:
        try:
            if self.media_file_id is not None:
                self.session.query(MediaFile).filter(MediaFile.id == self.media_file_id).delete()
            if self.user_id is not None:
                self.session.query(User).filter(User.id == self.user_id).delete()
            self.session.commit()
        finally:
            self.session.close()


class TestRecoverStuckFileDispatchFailure:
    def test_a_failed_restart_of_an_orphaned_file_records_the_attempt(self, monkeypatch):
        _fail_dispatch(monkeypatch)
        row = _RealRow(status=FileStatus.ORPHANED)
        try:
            row.null_out("recovery_attempts")

            from app.utils import task_utils

            result = task_utils.recover_stuck_file(row.session, row.id)

            assert result is False
            assert row.refresh().recovery_attempts == 1
        finally:
            row.cleanup()

    def test_a_failed_restart_logs_a_traceback(self, monkeypatch, caplog):
        _fail_dispatch(monkeypatch)
        row = _RealRow(status=FileStatus.ORPHANED)
        try:
            from app.utils import task_utils

            with caplog.at_level("ERROR"):
                task_utils.recover_stuck_file(row.session, row.id)

            matching = [r for r in caplog.records if "Failed to recover stuck file" in r.message]
            assert matching, "expected a 'Failed to recover stuck file' error log record"
            assert matching[0].exc_info is not None
            formatted = matching[0].getMessage()
            assert str(row.id) in formatted
            assert "broker down" in formatted
        finally:
            row.cleanup()

    def test_a_successful_recovery_still_increments_recovery_attempts_exactly_once(
        self, db_session, normal_user, monkeypatch
    ):
        """CONTROL: same shape, dispatch succeeds — `db.rollback()` never runs, so the
        ordinary savepoint-nested ``db_session`` fixture is safe to use here."""
        from app.utils import task_utils

        monkeypatch.setenv("SKIP_CELERY", "true")

        media_file = MediaFile(
            user_id=normal_user.id,
            filename=f"f-{uuid.uuid4().hex[:8]}.wav",
            storage_path=f"user_{normal_user.id}/{uuid.uuid4().hex[:8]}.wav",
            file_size=1024,
            content_type="audio/wav",
            status=FileStatus.PROCESSING,
            retry_count=0,
            active_task_id=None,
        )
        db_session.add(media_file)
        db_session.commit()
        db_session.refresh(media_file)
        db_session.execute(
            update(MediaFile).where(MediaFile.id == media_file.id).values(recovery_attempts=None)
        )
        db_session.commit()
        db_session.refresh(media_file)
        assert media_file.recovery_attempts is None

        result = task_utils.recover_stuck_file(db_session, int(media_file.id))

        assert result is True
        db_session.refresh(media_file)
        assert media_file.recovery_attempts == 1
        assert media_file.status == FileStatus.ORPHANED

    def test_a_null_recovery_attempts_is_normalised_not_a_type_error(self, monkeypatch):
        """The OTHER commit-then-dispatch branch, ``_recover_failed_file`` (ERROR status,
        within the retry budget): a NULL ``recovery_attempts`` at the moment of the
        secondary write must normalise to 1, not raise ``TypeError`` on ``None + 1``
        and swallow the whole recovery a second, different way."""
        _fail_dispatch(monkeypatch)
        row = _RealRow(
            status=FileStatus.ERROR,
            last_error_message=_RETRIABLE_ERROR,
            active_task_id=None,
        )
        try:
            row.null_out("recovery_attempts")

            from app.utils import task_utils

            result = task_utils.recover_stuck_file(row.session, row.id)

            assert result is False
            assert row.refresh().recovery_attempts == 1
        finally:
            row.cleanup()
