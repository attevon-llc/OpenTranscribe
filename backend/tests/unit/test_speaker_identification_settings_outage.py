"""A failed read of the LLM settings fails speaker identification; it is not a success.

The service used to be resolved inside the ``try`` that turns a provider fault into empty
predictions, so a database outage was stored as "succeeded, 0 suggestions": nothing to
review and nothing telling anyone to run it again.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.models.media import Task as TaskModel
from tests.unit._session_lifetime_trackers import ScopeTracker
from tests.unit._session_lifetime_trackers import make_transcribed_file

LEAK_MARKER = "SELECT secret_column FROM user_llm_settings"


@pytest.fixture
def identification_task(db_session, monkeypatch):
    from app.tasks import speaker_identification_task as sid

    tracker = ScopeTracker(db_session)
    monkeypatch.setattr(sid, "session_scope", tracker.scope)
    monkeypatch.setattr(sid, "resolve_llm_masking", lambda db, media_file: None)
    # The status report and the takedown check open their own sessions, which cannot see
    # this test's savepoint-isolated rows.
    monkeypatch.setattr("app.services.post_processing_status.report", lambda *a, **kw: False)
    monkeypatch.setattr(
        "app.services.file_availability.is_notification_suppressed", lambda *a, **kw: False
    )

    def _unreadable(user_id):
        raise OperationalError(LEAK_MARKER, {}, Exception("connection reset"))

    monkeypatch.setattr(sid.LLMService, "create_from_user_settings", _unreadable)
    return sid.identify_speakers_llm_task


def test_a_settings_outage_marks_speaker_identification_failed(
    db_session, normal_user, identification_task
):
    media_file, _ = make_transcribed_file(db_session, normal_user)

    result = identification_task.apply(args=[str(media_file.uuid)]).get()

    assert result["status"] == "error", result
    db_session.expire_all()
    task_row = db_session.query(TaskModel).filter(TaskModel.media_file_id == media_file.id).one()
    assert task_row.status == "failed"
    assert task_row.error_message == "OperationalError"
    assert LEAK_MARKER not in str(result)
