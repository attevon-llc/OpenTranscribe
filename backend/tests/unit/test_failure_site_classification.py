"""Issue #959 — a failure is classified ONCE, at the failure site, and the raw exception is
never stored.

#786 sanitized every read edge but left the write untouched: the pipeline's failure handlers
still wrote the interpolated raw exception (paths and all) to ``media_file.last_error_message``
and ``task.error_message``, and retry policy re-derived ``error_category`` by substring-matching
that stored prose — so rewording a message silently changed automatic retries.

These tests pin the new contract end to end, against real rows:

1. every pipeline failure handler stores the FIXED user-facing sentence, never the raw text;
2. the stored retry code (``error_category``) is derived from the RAW text at the failure site
   — a signal the fixed sentence no longer carries;
3. retry policy and GPU-OOM detection read that stored code, never the stored prose;
4. a stored fixed sentence reads back as the reason it was written for;
5. the duplicate-URL 409, a read edge #786 missed, no longer echoes the raw column (the other,
   ``/my-files/{uuid}/status`` task rows, is covered in ``tests/api/test_user_files.py``).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from fastapi import HTTPException

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services.error_categorization_service import INPUT_ERROR_REASONS
from app.services.error_categorization_service import ErrorCategorizationService
from app.services.error_categorization_service import UserErrorReason
from app.utils.error_classification import ErrorCategory
from app.utils.error_classification import categorize_error

SENTINEL = "SENTINEL-/srv/internal/secret-path"
RAW_GPU_OOM = f"CUDA out of memory. Tried to allocate 2.00 GiB at {SENTINEL}"
RAW_CORRUPT = f"Command '['ffmpeg', '-i', '{SENTINEL}']' failed: file is corrupted"


def _media_file(db, user, **kwargs) -> MediaFile:
    defaults = {
        "uuid": str(uuid.uuid4()),
        "user_id": user.id,
        "filename": f"f959-{uuid.uuid4().hex[:8]}.wav",
        "storage_path": f"user/test/f959-{uuid.uuid4().hex[:8]}.wav",
        "content_type": "audio/wav",
        "file_size": 2048,
        "status": FileStatus.PROCESSING,
        "retry_count": 0,
    }
    media_file = MediaFile(**{**defaults, **kwargs})
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _task(db, user, media_file) -> Task:
    task = Task(
        id=f"task-959-{uuid.uuid4()}",
        user_id=user.id,
        media_file_id=media_file.id,
        task_type="transcription",
        status="in_progress",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@contextmanager
def _bridged_scope(db_session):
    yield db_session
    db_session.commit()


# ---------------------------------------------------------------------------
# 1 + 2: the failure sites store the fixed sentence and the RAW-derived retry code
# ---------------------------------------------------------------------------


def _route_retry_policy(monkeypatch, db_session) -> tuple[list[str], list[int]]:
    """Point the retry policy's sessions at the test session; record notices and retries."""
    from app.services.task_recovery_service import TaskRecoveryService

    notified: list[str] = []
    retried: list[int] = []
    monkeypatch.setattr(
        "app.services.transcription_retry.session_scope", lambda: _bridged_scope(db_session)
    )
    monkeypatch.setattr(
        "app.tasks.transcription.notifications.send_error_notification",
        lambda _u, _f, msg: notified.append(msg),
    )
    monkeypatch.setattr(
        "app.tasks.transcription.notifications.send_progress_notification",
        lambda *_a, **_kw: None,
    )

    def _record_retry(_self, file_id, countdown=None):
        retried.append(file_id)
        return True

    monkeypatch.setattr(TaskRecoveryService, "schedule_file_retry", _record_retry)
    return notified, retried


@pytest.mark.unit
def test_preprocess_failure_stores_no_raw_exception(db_session, normal_user, monkeypatch):
    """A GPU OOM is TRANSIENT, so the run is retired and the file requeued -- and nothing on
    the way stores the raw text."""
    from app.tasks.transcription import preprocess

    media_file = _media_file(db_session, normal_user)
    task = _task(db_session, normal_user, media_file)
    monkeypatch.setattr(preprocess, "session_scope", lambda: _bridged_scope(db_session))
    notified, retried = _route_retry_policy(monkeypatch, db_session)

    preprocess._mark_pipeline_error(str(media_file.uuid), task.id, RAW_GPU_OOM)

    db_session.expire_all()
    stored = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).one()
    stored_task = db_session.query(Task).filter(Task.id == task.id).one()
    fixed = ErrorCategorizationService.get_error_info(RAW_GPU_OOM)["user_message"]

    assert stored.status == FileStatus.PENDING
    assert retried == [media_file.id]
    assert stored_task.error_message == fixed
    assert SENTINEL not in (stored.last_error_message or "")
    assert SENTINEL not in (stored_task.error_message or "")
    # The fixed sentence says nothing about CUDA; the retry code still knows it was a GPU OOM
    # because it was derived from the raw text before the raw text was dropped.
    assert stored.error_category == ErrorCategory.GPU_OOM.value
    assert notified == []


@pytest.mark.unit
def test_a_permanent_preprocess_failure_stores_no_raw_exception(
    db_session, normal_user, monkeypatch
):
    from app.tasks.transcription import preprocess

    media_file = _media_file(db_session, normal_user)
    task = _task(db_session, normal_user, media_file)
    monkeypatch.setattr(preprocess, "session_scope", lambda: _bridged_scope(db_session))
    notified, retried = _route_retry_policy(monkeypatch, db_session)

    preprocess._mark_pipeline_error(str(media_file.uuid), task.id, RAW_CORRUPT)

    db_session.expire_all()
    stored = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).one()
    stored_task = db_session.query(Task).filter(Task.id == task.id).one()
    fixed = ErrorCategorizationService.get_error_info(RAW_CORRUPT)["user_message"]

    assert stored.status == FileStatus.ERROR
    assert stored.last_error_message == fixed
    assert stored_task.error_message == fixed
    assert SENTINEL not in (stored.last_error_message or "")
    assert stored.error_category == ErrorCategory.INVALID_MEDIA.value
    assert retried == []
    assert notified == [fixed]


@pytest.mark.unit
def test_transcription_failure_handler_stores_no_raw_exception(
    db_session, normal_user, monkeypatch
):
    from app.tasks.transcription import context as transcription_context

    media_file = _media_file(db_session, normal_user)
    task = _task(db_session, normal_user, media_file)
    monkeypatch.setattr(transcription_context, "session_scope", lambda: _bridged_scope(db_session))
    _route_retry_policy(monkeypatch, db_session)
    ctx = transcription_context.TranscriptionContext(
        task_id=task.id,
        file_id=media_file.id,
        file_uuid=str(media_file.uuid),
        user_id=normal_user.id,
        file_path=str(media_file.storage_path),
        file_name=str(media_file.filename),
        content_type=str(media_file.content_type),
    )

    result = transcription_context._handle_transcription_failure(
        ctx, task.id, RAW_CORRUPT, "gpu_processing_error"
    )

    db_session.expire_all()
    stored = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).one()
    stored_task = db_session.query(Task).filter(Task.id == task.id).one()
    fixed = ErrorCategorizationService.get_error_info(RAW_CORRUPT)["user_message"]

    assert stored.last_error_message == fixed
    assert stored_task.error_message == fixed
    assert result["message"] == fixed
    assert SENTINEL not in str(result)
    # The stored sentence must still read back as the reason the raw text was classified to.
    assert (
        ErrorCategorizationService.get_error_info(stored.last_error_message)["category"]
        == UserErrorReason.FILE_QUALITY.value
    )


@pytest.mark.unit
def test_the_friendly_message_layer_is_gone():
    """#959 item 3: one diagnosis path. The fourth layer must not come back."""
    from app.tasks.transcription import context as transcription_context

    assert not hasattr(transcription_context, "_get_user_friendly_error_message")


# ---------------------------------------------------------------------------
# 3: retry policy reads the stored code, never the stored prose
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recovery_keys_off_the_stored_category_not_the_message(db_session, normal_user):
    """A permanent stored code wins over a message that carries no category signal.

    The stored sentence is the fixed PERMISSION one, which the retry classifier's substring
    rules read as UNKNOWN (retriable). Re-deriving from it — what recovery used to do —
    would retry a private/removed video forever.
    """
    from app.services.task_recovery_service import TaskRecoveryService

    permission_sentence = ErrorCategorizationService.get_error_info("access denied")["user_message"]
    media_file = _media_file(
        db_session,
        normal_user,
        last_error_message=permission_sentence,
        error_category=ErrorCategory.PRIVATE_OR_REMOVED.value,
    )

    TaskRecoveryService()._update_media_file_if_no_active_tasks(db_session, media_file)

    db_session.refresh(media_file)
    assert media_file.status == FileStatus.ERROR
    assert media_file.error_category == ErrorCategory.PRIVATE_OR_REMOVED.value
    assert int(media_file.retry_count or 0) == 0


@pytest.mark.unit
def test_rewording_the_stored_message_cannot_change_retry(db_session, normal_user, monkeypatch):
    """The converse: a retriable stored code is retried whatever the stored prose says."""
    from app.services.task_recovery_service import TaskRecoveryService

    media_file = _media_file(
        db_session,
        normal_user,
        last_error_message="This is a private video",
        error_category=ErrorCategory.NETWORK_ERROR.value,
    )
    service = TaskRecoveryService()
    monkeypatch.setattr(service, "schedule_file_retry", lambda _file_id: True)

    service._update_media_file_if_no_active_tasks(db_session, media_file)

    db_session.refresh(media_file)
    assert media_file.status == FileStatus.PENDING
    assert media_file.retry_count == 1


@pytest.mark.unit
def test_gpu_oom_detection_reads_the_stored_code(db_session, normal_user):
    """The OOM backoff path used to ``ilike`` the stored message for "cuda" + "out of memory".
    That text is no longer stored, so detection must key off the stored GPU-OOM code — and a
    host-RAM OOM (``oom``) must still stay out of the GPU path."""
    from app.services.task_detection_service import TaskDetectionService

    fixed = ErrorCategorizationService.get_error_info(RAW_GPU_OOM)["user_message"]
    gpu = _media_file(
        db_session,
        normal_user,
        status=FileStatus.ERROR,
        last_error_message=fixed,
        error_category=ErrorCategory.GPU_OOM.value,
        last_recovery_attempt=datetime.now(UTC) - timedelta(days=1),
    )
    host = _media_file(
        db_session,
        normal_user,
        status=FileStatus.ERROR,
        last_error_message=fixed,
        error_category=ErrorCategory.OOM_ERROR.value,
        last_recovery_attempt=datetime.now(UTC) - timedelta(days=1),
    )

    ids = {f.id for f in TaskDetectionService().identify_oom_error_files(db_session)}
    assert gpu.id in ids
    assert host.id not in ids


# ---------------------------------------------------------------------------
# 4: a stored fixed sentence reads back as its own reason
# ---------------------------------------------------------------------------

_RAW_BY_REASON = {
    UserErrorReason.FILE_QUALITY: "moov atom not found: file is corrupted",
    UserErrorReason.NO_AUDIO_TRACK: "Output file does not contain any audio stream",
    UserErrorReason.NO_SPEECH: "no speech detected in segment",
    UserErrorReason.FORMAT_ISSUE: "unsupported codec: pcm_f64be",
    UserErrorReason.NETWORK_ERROR: "Read timeout on storage",
    UserErrorReason.PERMISSION_ERROR: "Permission denied: '/data/x'",
    UserErrorReason.PROCESSING_ERROR: "IndexError: list index out of range",
}


@pytest.mark.unit
@pytest.mark.parametrize("reason", list(_RAW_BY_REASON))
def test_a_stored_sentence_round_trips_to_its_reason(reason):
    """Several fixed sentences do not contain their own category's substring patterns (the
    network one never says "network"), so a stored FORMAT_ISSUE used to read back as a
    generic PROCESSING_ERROR, with the wrong suggestions and the wrong retry button."""
    written = ErrorCategorizationService.get_error_info(_RAW_BY_REASON[reason])
    assert written["category"] == reason.value

    read_back = ErrorCategorizationService.get_error_info(written["user_message"])
    assert read_back == written


@pytest.mark.unit
@pytest.mark.parametrize("reason", list(_RAW_BY_REASON))
def test_classify_failure_is_the_read_edge_classification_plus_the_retry_code(reason):
    raw = _RAW_BY_REASON[reason]
    failure = ErrorCategorizationService.classify_failure(raw)
    info = ErrorCategorizationService.get_error_info(raw)

    assert failure.reason.value == info["category"]
    assert failure.user_message == info["user_message"]
    # An unusable input is PERMANENT whatever the raw text's own retry signal; every other
    # reason keeps the code derived from the raw text.
    if reason in INPUT_ERROR_REASONS:
        assert failure.retry_category == ErrorCategory.INVALID_MEDIA
    else:
        assert failure.retry_category == categorize_error(raw)


# ---------------------------------------------------------------------------
# 5: the read edges #786 missed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_duplicate_url_409_does_not_echo_the_stored_error():
    from app.api.endpoints.files.url_processing import _raise_duplicate_error

    existing = MediaFile()
    existing.status = FileStatus.ERROR
    existing.last_error_message = f"ERROR: [youtube] abc: Unable to download {SENTINEL}"

    with pytest.raises(HTTPException) as raised:
        _raise_duplicate_error(existing)

    assert raised.value.status_code == 409
    assert SENTINEL not in str(raised.value.detail)
