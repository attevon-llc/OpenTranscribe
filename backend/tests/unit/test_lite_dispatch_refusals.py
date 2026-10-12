"""Lite dispatch refuses every local transcription and local speaker detection (issue #1228).

A lite image ships no faster-whisper and, by the owner's decision, runs no local speaker
detection. Dispatch must refuse such a run with the plan's fixed sentence, categorised as a
permanent configuration failure, BEFORE any task record exists or anything is published:
never a ``ModuleNotFoundError`` deep inside a task on a worker (plan A7's hazard: a per-file
``tiny``/``base`` model used to route to ``cpu-transcribe``, whose lite worker has no
faster-whisper), and never a rediarize-on-CPU run.

Each refusal has a control on the same fixture where only the deployment or the choice
differs, so a dispatcher that refused everything (or nothing) fails by name. The real
``dispatch_transcription_pipeline`` runs against real rows; only the out-of-process seams
are replaced: the session bridge (the dispatcher opens its own session), the Redis liveness
marker, and the Celery publish, which is recorded instead.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager

import pytest

from app.core.config import settings
from app.core.constants import CeleryQueues
from app.core.exceptions import PipelinePlanRefusedError
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.models.prompt import UserSetting
from app.models.user_asr_settings import UserASRSettings
from app.services.pipeline_plan import ISSUE_MESSAGES
from app.utils.error_classification import ErrorCategory

_DISPATCH = "app.tasks.transcription.dispatch"


@contextmanager
def _bridged(db_session):
    yield db_session
    db_session.commit()


@pytest.fixture
def published(db_session, monkeypatch):
    """Record every chain dispatch would publish, with each link's task and queue."""
    from app.tasks.transcription import dispatch

    sent: list[list[tuple[str, str]]] = []

    class _Chain:
        def __init__(self, *links):
            self.links = [(link.task, link.options.get("queue")) for link in links]

        def apply_async(self, **kwargs):
            sent.append(self.links)

    monkeypatch.setattr(dispatch, "session_scope", lambda: _bridged(db_session))
    monkeypatch.setattr(dispatch, "chain", _Chain)
    monkeypatch.setattr(dispatch, "mark_queued", lambda task_id: None)
    monkeypatch.setattr(dispatch, "gpu_split_enabled", lambda: False)
    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    return sent


@pytest.fixture
def user_file(db_session, normal_user):
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        filename="lite.wav",
        storage_path=f"user_{normal_user.id}/lite.wav",
        file_size=1,
        content_type="audio/wav",
        status=FileStatus.PENDING,
    )
    db_session.add(media)
    db_session.commit()
    return media


def _activate(db_session, user, provider: str, model: str) -> None:
    cfg = UserASRSettings(
        user_id=user.id,
        name=f"{provider}-{uuid_pkg.uuid4().hex[:6]}",
        provider=provider,
        model_name=model,
        is_active=True,
    )
    db_session.add(cfg)
    db_session.commit()
    db_session.add(
        UserSetting(user_id=user.id, setting_key="active_asr_config_id", setting_value=str(cfg.id))
    )
    db_session.commit()


def _choose(db_session, user, source: str) -> None:
    db_session.add(
        UserSetting(
            user_id=user.id, setting_key="transcription_diarization_source", setting_value=source
        )
    )
    db_session.commit()


def _dispatch(media: MediaFile, **kwargs) -> str:
    from app.tasks.transcription.dispatch import dispatch_transcription_pipeline

    return dispatch_transcription_pipeline(str(media.uuid), **kwargs)


def _assert_refused(db_session, media: MediaFile, published, refused, code: str) -> None:
    assert refused.value.code == code
    assert str(refused.value) == ISSUE_MESSAGES[code]
    db_session.refresh(media)
    assert media.status == FileStatus.ERROR
    assert media.last_error_message == ISSUE_MESSAGES[code]
    assert media.error_category == ErrorCategory.CONFIGURATION_REQUIRED.value
    assert db_session.query(Task).filter(Task.media_file_id == media.id).count() == 0
    assert published == []


# --- A7: a per-file lightweight model on lite -------------------------------------------


@pytest.mark.parametrize("model", ["tiny", "base", "tiny.en", "base.en"])
def test_lite_refuses_a_per_file_cpu_model_before_anything_is_queued(
    db_session, normal_user, user_file, published, monkeypatch, model
):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "lite")
    _activate(db_session, normal_user, "deepgram", "nova-3")

    with pytest.raises(PipelinePlanRefusedError) as refused:
        _dispatch(user_file, whisper_model=model)

    _assert_refused(db_session, user_file, published, refused, "local_asr_unavailable")


def test_the_full_image_sends_the_same_request_to_cpu_transcribe(
    db_session, normal_user, user_file, published, monkeypatch
):
    """Control: the A7 route exists on the full image; only lite refuses it."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "full")
    _activate(db_session, normal_user, "deepgram", "nova-3")

    _dispatch(user_file, whisper_model="tiny")

    assert len(published) == 1
    assert published[0][1] == ("transcription.cpu_transcribe", CeleryQueues.CPU_TRANSCRIBE)


def test_a_cpu_transcribe_message_that_reaches_a_lite_worker_fails_with_the_sentence(
    db_session, normal_user, user_file, monkeypatch
):
    """Defence in depth: a message queued before the upgrade must fail with the plan's
    sentence, never import faster-whisper or download any audio first."""
    from app.services import minio_service
    from app.tasks.transcription import cpu_task
    from tests.unit._plan_contexts import make_plan

    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "lite")
    failures: list[str] = []

    def _record_failure(ctx, task_id, raw_error, error_type):
        failures.append(raw_error)
        return {"status": "error"}

    monkeypatch.setattr(cpu_task, "_handle_transcription_failure", _record_failure)

    def _no_download(*args, **kwargs):
        raise AssertionError("downloaded audio for a run lite cannot transcribe")

    monkeypatch.setattr(minio_service, "download_temp_audio", _no_download)
    monkeypatch.setattr(cpu_task, "session_scope", lambda: _bridged(db_session))
    monkeypatch.setattr(cpu_task, "superseded_or_none", lambda *a, **k: None)

    context = {
        "task_id": f"run-{uuid_pkg.uuid4().hex[:10]}",
        "file_uuid": str(user_file.uuid),
        "file_id": user_file.id,
        "user_id": normal_user.id,
        "storage_path": user_file.storage_path,
        "file_name": user_file.filename,
        "content_type": user_file.content_type,
        "whisper_model": "tiny",
        "pipeline_plan": make_plan("local_cpu", "off").to_context(),
    }
    with pytest.raises(PipelinePlanRefusedError):
        cpu_task.transcribe_cpu_task(context)

    assert failures == [ISSUE_MESSAGES["local_asr_unavailable"]]


# --- lite has no local speaker detection --------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "model", "source", "code"),
    [
        ("deepgram", "nova-3", "local", "local_diarization_unavailable"),
        ("openai", "whisper-1", "local", "local_diarization_unavailable"),
        ("openai", "whisper-1", "provider", "automatic_needs_speaker_provider"),
    ],
)
def test_lite_refuses_a_run_whose_speakers_would_be_detected_on_this_server(
    db_session, normal_user, user_file, published, monkeypatch, provider, model, source, code
):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "lite")
    _activate(db_session, normal_user, provider, model)
    _choose(db_session, normal_user, source)

    with pytest.raises(PipelinePlanRefusedError) as refused:
        _dispatch(user_file)

    _assert_refused(db_session, user_file, published, refused, code)


@pytest.mark.parametrize(
    ("provider", "model", "source"),
    [
        ("deepgram", "nova-3", "provider"),  # the vendor's own speakers
        ("deepgram", "nova-3", "off"),
        ("openai", "whisper-1", "off"),
    ],
)
def test_lite_dispatches_cloud_combinations_to_cloud_asr(
    db_session, normal_user, user_file, published, monkeypatch, provider, model, source
):
    """Control: lite runs every cloud combination; the transcription link is on cloud-asr."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "lite")
    _activate(db_session, normal_user, provider, model)
    _choose(db_session, normal_user, source)

    _dispatch(user_file)

    assert len(published) == 1
    assert published[0][1] == ("transcription.gpu_transcribe", CeleryQueues.CLOUD_ASR)
    db_session.refresh(user_file)
    assert user_file.status == FileStatus.PROCESSING


def test_the_full_image_runs_the_same_local_choice(
    db_session, normal_user, user_file, published, monkeypatch
):
    """Control for the first refusal: the full image detects these speakers on this server."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "full")
    _activate(db_session, normal_user, "deepgram", "nova-3")
    _choose(db_session, normal_user, "local")

    _dispatch(user_file)

    assert len(published) == 1
