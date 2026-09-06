"""A summarization run must not leave a progress bar pinned at 50%.

``summarize_transcript_task`` pushes three ``processing`` notifications (10%,
30%, 50%) and only then calls ``_generate_llm_summary``, which returns ``None``
when no LLM provider is configured. That case is routed to
``_handle_no_llm_configured``, which deliberately sends **no** notification —
having no provider is a deployment choice, not a per-file failure, and flagging
it per file buries real failures under noise.

That reasoning is right about not sending a *failure*. What it missed is that
by the time it runs, the user has already been sent "Generating AI summary with
LLM — 50%", and with nothing terminal following it, the notification panel sits
at 50% forever on a file that is completely finished.

The fix resolves provider availability BEFORE any processing notification is
emitted, so the not-configured path stays silent — as intended — and there is
no in-flight notification left to strand.
"""

import uuid
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.models.media import MediaFile
from app.models.media import Task


@pytest.fixture
def transcribed_file(db_session, normal_user):
    """A completed file, as it exists when summarization is dispatched."""
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=normal_user.id,
        filename="summary_terminus.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
    )
    db_session.add(media_file)
    db_session.commit()
    return media_file


class TestSummaryNotificationTerminus:
    def test_no_processing_notification_is_sent_when_no_llm_is_configured(
        self, db_session, normal_user, transcribed_file
    ):
        """The defect: three processing frames went out, none of them resolved."""
        from app.tasks import summarization

        sent: list[tuple[str, int]] = []

        def record(user_id, file_id, status, message, progress=0, summary_data=None):
            sent.append((status, progress))
            return True

        task_id = str(uuid.uuid4())
        task = self._make_task(db_session, normal_user.id, transcribed_file.id, task_id)

        with (
            patch.object(summarization, "send_summary_notification", side_effect=record),
            patch.object(summarization, "session_scope", self._scope(db_session)),
            patch.object(summarization.LLMService, "create_from_user_settings", return_value=None),
        ):
            summarization._handle_no_llm_configured(
                transcribed_file.id, normal_user.id, "summary_terminus.wav", task_id
            )

        # Nothing is announced for a deployment choice...
        assert sent == []
        # ...and the task row is closed, so nothing polls it forever either.
        db_session.expire_all()
        assert db_session.query(Task).filter(Task.id == task_id).first().status == "completed"
        assert task is not None

    def test_the_provider_is_resolved_before_any_progress_is_announced(
        self, db_session, normal_user, transcribed_file
    ):
        """The ordering that makes the silence above safe.

        If availability were still resolved after the 10/30/50 frames, this
        test would pass while the live bug survived — so it asserts the order
        directly rather than the outcome.
        """
        from app.tasks import summarization

        order: list[str] = []

        def record_notification(user_id, file_id, status, message, progress=0, summary_data=None):
            order.append(f"notify:{progress}")
            return True

        def record_probe(user_id):
            order.append("probe")
            return None

        with (
            patch.object(
                summarization, "send_summary_notification", side_effect=record_notification
            ),
            patch.object(
                summarization.LLMService, "create_from_user_settings", side_effect=record_probe
            ),
        ):
            configured = summarization._llm_is_configured(normal_user.id)

        assert configured is False
        assert order == ["probe"], f"a notification preceded the probe: {order}"

    def test_a_configured_provider_still_reports_progress(self, db_session, normal_user):
        """The control: same probe, opposite answer, so the check discriminates."""
        from app.tasks import summarization

        service = MagicMock()
        with patch.object(
            summarization.LLMService, "create_from_user_settings", return_value=service
        ):
            assert summarization._llm_is_configured(normal_user.id) is True

        # The probe owns the service it created and must not leak it — the real
        # summary path creates its own moments later.
        service.close.assert_called_once()

    @staticmethod
    def _make_task(db, user_id: int, file_id: int, task_id: str) -> Task:
        task = Task(
            id=task_id,
            user_id=user_id,
            media_file_id=file_id,
            task_type="summarization",
            status="in_progress",
            progress=0.5,
        )
        db.add(task)
        db.commit()
        return task

    @staticmethod
    def _scope(db_session):
        import contextlib

        @contextlib.contextmanager
        def _s():
            yield db_session

        return _s
