"""`update_task_status`'s transient `duration_seconds` attribute (issue #753).

DEFECT THIS GUARDS AGAINST: the duration chip's data does not exist anywhere
in the app today, and the ONE place the timing information is briefly
available is INSIDE this function, in the terminal-transition branch, a few
lines before it clears ``MediaFile.task_started_at`` — the only record of
when the task began. A caller that reads ``media_file.task_started_at`` any
later (as a naive implementation of this feature would) always finds ``None``
and silently reports no duration; a caller that instead substitutes
``MediaFile.duration`` (the length of the RECORDING) gets a plausible-looking
but completely wrong number on every file. Both traps are covered below.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.utils.task_utils import create_task_record
from app.utils.task_utils import update_task_status


def _make_file(db_session, user, *, duration: float | None = None) -> MediaFile:
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename="duration-chip.mp4",
        storage_path="test/duration-chip.mp4",
        content_type="video/mp4",
        file_size=1000,
        status=FileStatus.PENDING,
        # The length of the RECORDING — a deliberately different, much larger
        # number than the processing time below, so a test that accidentally
        # reads this field instead of the real duration fails loudly rather
        # than by coincidence matching.
        duration=duration,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


class TestDurationComputedBeforeTaskStartedAtIsCleared:
    def test_completed_task_reports_the_real_elapsed_time(self, db_session, normal_user):
        media_file = _make_file(db_session, normal_user, duration=5400.0)  # 90-minute recording
        task = create_task_record(
            db_session, str(uuid_mod.uuid4()), normal_user.id, media_file.id, "summarization"
        )

        # Backdate the start so the elapsed time is deterministic and — the
        # point of this test — nowhere near the recording's 5400s length.
        db_session.refresh(media_file)
        media_file.task_started_at = datetime.now(UTC) - timedelta(seconds=45)
        db_session.commit()

        updated = update_task_status(db_session, task.id, "completed", progress=1.0, completed=True)

        assert updated is not None
        duration_seconds = updated.duration_seconds
        assert duration_seconds is not None
        assert 44 <= duration_seconds <= 46
        # The trap: must never equal (or even resemble) the recording length.
        assert duration_seconds != media_file.duration
        assert duration_seconds < 100

    def test_clears_task_started_at_in_the_same_call_that_computed_the_duration(
        self, db_session, normal_user
    ):
        """Documents WHY duration must be read here, not by a later caller.

        If a future change moves this clear earlier, or a caller tries to
        recompute the duration itself after calling this function, this test
        will start failing the instant `task_started_at` reads as `None` —
        which is exactly the silent-no-duration bug this design avoids.
        """
        media_file = _make_file(db_session, normal_user)
        task = create_task_record(
            db_session, str(uuid_mod.uuid4()), normal_user.id, media_file.id, "topic_extraction"
        )

        updated = update_task_status(db_session, task.id, "completed", progress=1.0, completed=True)

        db_session.refresh(media_file)
        assert media_file.task_started_at is None
        assert updated is not None
        assert updated.duration_seconds is not None

    def test_no_duration_when_task_was_never_marked_completed(self, db_session, normal_user):
        """A terminal status reached WITHOUT `completed=True` (e.g. some
        `failed` call sites) must not fabricate a duration from a missing
        `completed_at` — `None` is the honest answer, not `0`.
        """
        media_file = _make_file(db_session, normal_user)
        task = create_task_record(
            db_session, str(uuid_mod.uuid4()), normal_user.id, media_file.id, "search_indexing"
        )

        updated = update_task_status(db_session, task.id, "failed", error_message="boom")

        assert updated is not None
        assert updated.duration_seconds is None

    def test_no_duration_when_task_has_no_media_file(self, db_session, normal_user):
        """A corpus-wide task (`media_file_id=None`) has no `MediaFile` to read
        a start time from — must report `None`, never raise.
        """
        task_id = str(uuid_mod.uuid4())
        task = Task(
            id=task_id,
            user_id=normal_user.id,
            media_file_id=None,
            task_type="reembed",
            status="pending",
        )
        db_session.add(task)
        db_session.commit()

        updated = update_task_status(db_session, task_id, "completed", progress=1.0, completed=True)

        assert updated is not None
        assert updated.duration_seconds is None

    def test_in_progress_update_reports_no_duration(self, db_session, normal_user):
        """A non-terminal progress tick must not compute or claim a duration —
        the task hasn't finished."""
        media_file = _make_file(db_session, normal_user)
        task = create_task_record(
            db_session, str(uuid_mod.uuid4()), normal_user.id, media_file.id, "summarization"
        )

        updated = update_task_status(db_session, task.id, "in_progress", progress=0.5)

        assert updated is not None
        assert getattr(updated, "duration_seconds", None) is None
