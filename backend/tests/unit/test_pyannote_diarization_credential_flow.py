"""The pyannote.ai credential reaches the provider, and its absence is never silent (#1204).

Before #1204 ``DiarizationProviderFactory.create_for_user`` returned ``None`` for a user who
chose ``pyannote`` without a stored key, and the cloud pipeline then transcribed with speaker
detection switched OFF, logging a warning nobody saw. These tests pin:

1. a stored key builds a ``PyAnnoteCloudDiarizationProvider`` configured with that key;
2. a missing or undecryptable key raises ``DiarizationNotConfiguredError`` instead;
3. the cloud pipeline raises it BEFORE calling the (billable) ASR provider;
4. the failure lands on the file as the fixed ``diarization_not_configured`` reason, is not
   retried automatically, and carries no internal detail.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.models import UserSetting
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.models.user_diarization_settings import UserDiarizationSettings
from app.services.diarization.factory import DiarizationNotConfiguredError
from app.services.diarization.factory import DiarizationProviderFactory
from app.services.diarization.pyannote_provider import PyAnnoteCloudDiarizationProvider
from app.services.error_categorization_service import ErrorCategorizationService
from app.services.error_categorization_service import UserErrorReason
from app.utils.encryption import encrypt_api_key
from app.utils.error_classification import ErrorCategory

_KEY = "fixture-key-cccc-3333"


def _select_pyannote(db, user) -> None:
    db.add(
        UserSetting(
            user_id=user.id,
            setting_key="transcription_diarization_source",
            setting_value="pyannote",
        )
    )
    db.commit()


def _store_key(db, user, stored: str | None) -> None:
    db.add(
        UserDiarizationSettings(
            user_id=user.id,
            name="pyannote.ai",
            provider="pyannote",
            model_name="precision-3",
            api_key=stored,
            is_active=True,
        )
    )
    db.commit()


class TestFactory:
    def test_stored_key_builds_a_configured_provider(self, db_session, normal_user):
        _select_pyannote(db_session, normal_user)
        _store_key(db_session, normal_user, encrypt_api_key(_KEY))

        provider = DiarizationProviderFactory.create_for_user(normal_user.id, db_session)

        assert isinstance(provider, PyAnnoteCloudDiarizationProvider)
        assert provider._api_key == _KEY
        assert provider._model_name == "precision-3"

    def test_missing_key_raises_instead_of_returning_none(self, db_session, normal_user):
        _select_pyannote(db_session, normal_user)

        with pytest.raises(DiarizationNotConfiguredError) as raised:
            DiarizationProviderFactory.create_for_user(normal_user.id, db_session)

        assert str(raised.value) == DiarizationNotConfiguredError.MESSAGE

    def test_undecryptable_key_is_treated_as_missing(self, db_session, normal_user):
        """A key encrypted under a rotated ENCRYPTION_KEY must not reach the vendor as ''."""
        _select_pyannote(db_session, normal_user)
        _store_key(db_session, normal_user, "v3:not-valid-ciphertext")

        with pytest.raises(DiarizationNotConfiguredError):
            DiarizationProviderFactory.create_for_user(normal_user.id, db_session)

    def test_other_users_key_is_never_used(self, db_session, normal_user, admin_user):
        _select_pyannote(db_session, normal_user)
        _store_key(db_session, admin_user, encrypt_api_key(_KEY))

        with pytest.raises(DiarizationNotConfiguredError):
            DiarizationProviderFactory.create_for_user(normal_user.id, db_session)

    def test_sources_without_a_cloud_provider_still_resolve_to_none(self, db_session, normal_user):
        assert DiarizationProviderFactory.create_for_user(normal_user.id, db_session) is None


# ---------------------------------------------------------------------------
# The pipeline: fail before the billable call, visibly, on the file
# ---------------------------------------------------------------------------


@contextmanager
def _bridged_scope(db_session):
    yield db_session
    db_session.commit()


class _CountingASR:
    provider_name = "deepgram"

    def __init__(self) -> None:
        self.calls = 0

    def supports_diarization(self) -> bool:
        return True

    def supports_translation(self) -> bool:
        return True

    def transcribe(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("cloud ASR must not run when diarization is misconfigured")


def _ctx(user, media_file, task_id):
    from app.tasks.transcription.context import TranscriptionContext

    return TranscriptionContext(
        task_id=task_id,
        file_id=media_file.id,
        file_uuid=str(media_file.uuid),
        user_id=user.id,
        file_path=str(media_file.storage_path),
        file_name=str(media_file.filename),
        content_type=str(media_file.content_type),
    )


@pytest.fixture
def processing_file(db_session, normal_user):
    media_file = MediaFile(
        uuid=str(uuid.uuid4()),
        user_id=normal_user.id,
        filename=f"p1204-{uuid.uuid4().hex[:8]}.wav",
        storage_path=f"user/test/p1204-{uuid.uuid4().hex[:8]}.wav",
        content_type="audio/wav",
        file_size=2048,
        status=FileStatus.PROCESSING,
        retry_count=0,
    )
    db_session.add(media_file)
    db_session.commit()
    task = Task(
        id=f"task-1204-{uuid.uuid4()}",
        user_id=normal_user.id,
        media_file_id=media_file.id,
        task_type="transcription",
        status="in_progress",
    )
    db_session.add(task)
    db_session.commit()
    return media_file, task


def test_pipeline_fails_before_calling_cloud_asr(
    db_session, normal_user, processing_file, monkeypatch
):
    from app.tasks.transcription import cloud_asr

    media_file, task = processing_file
    _select_pyannote(db_session, normal_user)
    monkeypatch.setattr(cloud_asr, "session_scope", lambda: _bridged_scope(db_session))
    asr = _CountingASR()

    with (
        patch(f"{cloud_asr.__name__}.send_progress_notification"),
        pytest.raises(DiarizationNotConfiguredError),
    ):
        cloud_asr._run_cloud_asr_pipeline(
            _ctx(normal_user, media_file, task.id),
            "/fake/audio.wav",
            min_speakers=None,
            max_speakers=None,
            num_speakers=None,
            provider=asr,
            diarization_source="pyannote",
        )

    assert asr.calls == 0


def test_failure_is_recorded_on_the_file_as_a_fixed_non_retried_reason(
    db_session, normal_user, processing_file, monkeypatch
):
    from app.services.task_recovery_service import TaskRecoveryService
    from app.tasks.transcription import context as transcription_context

    media_file, task = processing_file
    notified: list[str] = []
    retried: list[int] = []
    monkeypatch.setattr(transcription_context, "session_scope", lambda: _bridged_scope(db_session))
    monkeypatch.setattr(
        "app.services.transcription_retry.session_scope", lambda: _bridged_scope(db_session)
    )
    monkeypatch.setattr(
        "app.tasks.transcription.notifications.send_error_notification",
        lambda _u, _f, msg: notified.append(msg),
    )

    def _record_retry(_self, file_id, countdown=None) -> bool:
        retried.append(file_id)
        return True

    monkeypatch.setattr(TaskRecoveryService, "schedule_file_retry", _record_retry)

    error = DiarizationNotConfiguredError()
    result = transcription_context._handle_transcription_failure(
        _ctx(normal_user, media_file, task.id), task.id, str(error), "processing_error"
    )

    db_session.expire_all()
    stored = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).one()
    assert stored.status == FileStatus.ERROR
    assert stored.last_error_message == DiarizationNotConfiguredError.MESSAGE
    assert stored.error_category == ErrorCategory.CONFIGURATION_REQUIRED.value
    assert retried == []
    assert notified == [DiarizationNotConfiguredError.MESSAGE]
    assert result["status"] == "error"

    fields = ErrorCategorizationService.error_fields_for(stored)
    assert fields is not None
    assert fields["error_reason"] == UserErrorReason.DIARIZATION_NOT_CONFIGURED.value
    assert fields["user_message"] == DiarizationNotConfiguredError.MESSAGE
    assert fields["is_retryable"] is False
    assert fields["error_suggestions"]


def test_the_fixed_sentence_carries_no_retryable_signal():
    """If the sentence ever gained "connection"/"timeout"/"503", the substring retry
    classifier would turn a config problem into an endless retry loop."""
    failure = ErrorCategorizationService.classify_failure(DiarizationNotConfiguredError.MESSAGE)
    assert failure.reason is UserErrorReason.DIARIZATION_NOT_CONFIGURED
    assert failure.retry_category is ErrorCategory.CONFIGURATION_REQUIRED
