"""Issue #786 (lane A) — the three places a failure notice reaches a client must never
carry the raw exception text.

1. `send_error_notification` (`tasks/transcription/notifications.py`) is the chokepoint the
   plan asks for: every caller (preprocess, and both context.py failure handlers) routes
   through it, so fixing it here fixes the notifications-panel leak in one place.
2. `_send_dispatch_failed_ws_event` (`api/endpoints/files/upload.py`) is the one path by
   which raw text has ever reached the gallery — its `file` payload is spread wholesale into
   the client's file object.
3. `_handle_outer_exception` (`tasks/transcription/context.py`) must still persist the RAW
   message server-side (`media_file.last_error_message`) even though nothing sends it to a
   client any more — that's where an operator/admin actually diagnoses a failure.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager

import pytest

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services.error_categorization_service import ErrorCategorizationService
from app.tasks.transcription import context as transcription_context
from app.tasks.transcription import notifications as transcription_notifications

SENTINEL = "SENTINEL-/srv/internal/secret-path"


@pytest.mark.unit
def test_the_error_notification_is_not_the_raw_exception(monkeypatch, caplog):
    captured: dict = {}

    def fake_send_notification_with_retry(user_id, file_id, status, message, progress=0):
        captured["message"] = message
        return True

    monkeypatch.setattr(
        transcription_notifications,
        "send_notification_with_retry",
        fake_send_notification_with_retry,
    )

    raw_message = f"Audio preprocessing failed: {SENTINEL}"
    expected_user_message = ErrorCategorizationService.get_error_info(raw_message)["user_message"]

    with caplog.at_level(logging.ERROR):
        transcription_notifications.send_error_notification(1, 42, raw_message)

    assert SENTINEL not in captured["message"]
    assert captured["message"] == expected_user_message

    assert SENTINEL in caplog.text
    assert "42" in caplog.text


@pytest.mark.unit
def test_the_dispatch_failed_ws_payload_carries_no_raw_message(monkeypatch):
    from app.api.endpoints.files import upload as upload_module

    captured: dict = {}

    def fake_send_ws_event_for_file(user_id, event_type, data, *, file_id=None, **kwargs):
        captured["data"] = data

    monkeypatch.setattr(upload_module, "send_ws_event_for_file", fake_send_ws_event_for_file)

    media_file = MediaFile()
    media_file.id = 99
    media_file.uuid = uuid.uuid4()
    media_file.filename = "meeting.mp4"
    media_file.content_type = "video/mp4"
    media_file.file_size = 1024
    media_file.duration = 10.0
    media_file.upload_time = None
    media_file.status = FileStatus.ERROR

    raw_message = f"Dispatch failed: {SENTINEL}"
    upload_module._send_dispatch_failed_ws_event(media_file, 1, raw_message)

    file_data = captured["data"]["file"]
    assert "last_error_message" not in file_data
    assert SENTINEL not in str(file_data)
    assert file_data["error_reason"]
    assert file_data["user_message"]
    assert file_data["error_suggestions"]
    assert "is_retryable" in file_data


@pytest.mark.unit
def test_the_outer_exception_handler_persists_the_raw_message(db_session, normal_user, monkeypatch):
    media_file = MediaFile(
        uuid=str(uuid.uuid4()),
        filename=f"outer_exc_{uuid.uuid4().hex[:8]}.wav",
        title="outer exception persists raw message",
        storage_path=f"user/test/outer_exc_{uuid.uuid4().hex[:8]}.wav",
        content_type="audio/wav",
        file_size=2048,
        status=FileStatus.PROCESSING,
        is_public=False,
        retry_count=0,
        user_id=normal_user.id,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    ctx = transcription_context.TranscriptionContext(
        task_id="nonexistent-task-id",
        file_id=media_file.id,
        file_uuid=str(media_file.uuid),
        user_id=normal_user.id,
        file_path=str(media_file.storage_path),
        file_name=str(media_file.filename),
        content_type=str(media_file.content_type),
    )

    raw_error = RuntimeError(f"boom: {SENTINEL}")
    monkeypatch.setattr(transcription_context, "send_error_notification", lambda *a, **kw: None)

    # `context.py` imports `session_scope` by name (`from ... import session_scope`), so it
    # must be patched on `transcription_context` itself, not on `app.db.session_utils` — the
    # same bridging `test_chat_endpoints.py`'s `stub_llm` fixture documents for the same
    # reason: a genuinely separate connection can't see rows this test hasn't committed to
    # the outer (savepoint) transaction.
    @contextmanager
    def _test_session_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(transcription_context, "session_scope", _test_session_scope)

    transcription_context._handle_outer_exception(ctx, "nonexistent-task-id", raw_error)

    db_session.expire_all()
    refreshed = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).one()
    assert refreshed.last_error_message is not None
    assert SENTINEL in refreshed.last_error_message
