"""Every stage-completion point that exists today records its step (issue #1229).

Each test runs the REAL stage code against Postgres and fakes only the thin external
seams: the model, the LLM provider, audio staging, notifications and the session factory
(pointed at the test session, which the stage would otherwise open on its own). The
assertion is always the stored document, never a mock call.

Call sites covered:

| Step | Stage | Call |
|---|---|---|
| transcription + speakers | ``transcription/storage.update_media_file_transcription_status`` | ``record_transcription_run`` |
| speakers (post stage) | ``rediarize_task._save`` | ``record_diarization`` (plan engine, execution, merge) |
| voiceprints (inline) | ``transcription/postprocess._process_native_embeddings``, ``transcription/embeddings._process_speaker_embeddings_native`` | ``record_inline_voiceprints`` |
| voiceprints (extracted) | ``speaker_embedding_task`` (facts from the extractor's ``Voiceprints``) | ``record_stage(..., "voiceprints")`` |
| summary | ``summarization._persist_summary`` | ``record_stage(..., "summary")`` |
| topics | ``TopicExtractionService._persist_extraction`` | ``record_stage(..., "topics")`` |
| speaker names | ``speaker_identification_task._store_speaker_predictions`` | ``record_stage(..., "speaker_names")`` |
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.models.user import User
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.tasks.transcription.context import TranscriptionContext

WESPEAKER = "pyannote/wespeaker-voxceleb-resnet34-LM"


@pytest.fixture
def scope_of(db_session):
    @contextmanager
    def _scope():
        yield db_session
        db_session.commit()

    return _scope


@pytest.fixture
def media(db_session) -> MediaFile:
    user = User(
        email=f"provenance-hooks-{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name="Provenance Hooks",
        role="user",
    )
    db_session.add(user)
    db_session.commit()
    media_file = MediaFile(
        filename="provenance-hooks.wav",
        storage_path=f"x/{uuid.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
        status="processing",
        user_id=user.id,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


@pytest.fixture
def voiceprint_seams(monkeypatch, scope_of):
    """The seams every voiceprint stage shares: its session factory (pointed at the test
    session), profile matching (an OpenSearch kNN plane), the GPU sync and the task row."""
    from app.tasks import speaker_embedding_task
    from app.tasks.transcription import embeddings
    from app.tasks.transcription import postprocess

    for module in (embeddings, postprocess, speaker_embedding_task):
        monkeypatch.setattr(module, "session_scope", scope_of)
    for module in (embeddings, postprocess):
        monkeypatch.setattr(module, "update_task_status", lambda *a, **k: None)
    monkeypatch.setattr("app.utils.task_utils.update_task_status", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "SpeakerMatchingService", MagicMock())
    monkeypatch.setattr("app.services.speaker_matching_service.SpeakerMatchingService", MagicMock())
    monkeypatch.setattr(
        "app.services.permission_service.PermissionService.get_accessible_profile_ids_for_file",
        lambda *a, **k: [],
    )
    monkeypatch.setattr("app.utils.hardware_detection.detect_hardware", MagicMock())
    monkeypatch.setattr(embeddings, "_wait_for_migration_lock_to_clear", lambda: None)


@pytest.fixture
def rediarize_seams(monkeypatch, scope_of):
    """The post stage's seams: session factory, notifications and the attribute/LLM chain it
    dispatches. Its voiceprint stage dispatch is a Celery publish (patched suite-wide)."""
    from app.tasks import rediarize_task

    monkeypatch.setattr(rediarize_task, "session_scope", scope_of)
    for name in ("send_progress_notification", "send_completion_notification"):
        monkeypatch.setattr(f"app.tasks.transcription.notifications.{name}", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.tasks.speaker_attribute_task._is_speaker_attribute_detection_enabled", lambda: False
    )


@pytest.fixture
def summary_seams(monkeypatch, scope_of):
    """The summary task's seams: session factory, notifications and the task row."""
    from app.tasks import summarization

    monkeypatch.setattr(summarization, "session_scope", scope_of)
    monkeypatch.setattr(summarization, "_llm_is_configured", lambda *a: True)
    monkeypatch.setattr(summarization, "send_summary_notification", lambda *a, **k: None)
    monkeypatch.setattr(summarization, "_send_completion_notification", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.utils.task_utils.update_task_status_with_duration", lambda *a, **k: (None, None)
    )


def _steps(db_session, file_id: int) -> dict:
    document = db_session.execute(
        text("SELECT processing_provenance FROM media_file WHERE id = :i"), {"i": file_id}
    ).scalar()
    assert document is not None, "the stage recorded nothing"
    steps: dict = document["steps"]
    return steps


def _speakers(db_session, media_file, labels=("SPEAKER_00", "SPEAKER_01")) -> dict[str, int]:
    rows = [
        Speaker(media_file_id=media_file.id, user_id=media_file.user_id, name=n) for n in labels
    ]
    db_session.add_all(rows)
    db_session.commit()
    return {row.name: row.id for row in rows}


# ------------------------------------------------------------------ transcription completion


def test_saving_a_cloud_transcript_records_both_steps_and_starts_a_new_run(db_session, media):
    from app.services.file_provenance import record_stage
    from app.tasks.transcription.storage import update_media_file_transcription_status

    record_stage(db_session, media.id, "summary", provider="openai", model="from-an-old-run")

    update_media_file_transcription_status(
        db_session,
        media.id,
        [{"start": 0.0, "end": 1.0, "text": "hi"}],
        "en",
        asr_provider="openai",
        asr_model="gpt-4o-transcribe-diarize",
        diarization_provider="openai",
        diarization_model="gpt-4o-transcribe-diarize",
        diarization_engine="asr",
        diarization_execution="inline_asr",
        diarization_merge="segment",
    )

    steps = _steps(db_session, media.id)
    assert set(steps) == {"transcription", "diarization"}, "the old run's summary survived"
    assert steps["transcription"]["where"] == "cloud"
    assert steps["transcription"]["timing"] == "segment"
    diarization = steps["diarization"]
    assert (diarization["engine"], diarization["where"]) == ("asr", "cloud")
    assert (diarization["execution"], diarization["merge"]) == ("inline_asr", "segment")


def test_saving_a_local_transcript_with_speakers_skipped_records_off(db_session, media):
    from app.tasks.transcription.storage import update_media_file_transcription_status

    update_media_file_transcription_status(
        db_session,
        media.id,
        [{"start": 0.0, "end": 1.0, "text": "hi"}],
        "en",
        asr_provider="local",
        asr_model="large-v3-turbo",
        diarization_disabled=True,
    )

    steps = _steps(db_session, media.id)
    assert steps["transcription"]["where"] == "server"
    assert steps["transcription"]["timing"] == "word"
    assert steps["diarization"] == {
        "engine": "off",
        "completed_at": steps["diarization"]["completed_at"],
    }


# ------------------------------------------------------------------ speaker detection post stage


class _FakeDetector:
    """A detector at its seam: the plan's engine is built, and reports what served."""

    def __init__(self, provider: str, model: str, *, local: bool):
        self._provider, self._model, self._local = provider, model, local

    def diarize(self, audio_path, config, progress_callback=None):
        from app.services.diarization.types import DiarizeResult
        from app.services.diarization.types import DiarizeSegment

        frame = pd.DataFrame([{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}])
        return DiarizeResult(
            segments=[] if self._local else [DiarizeSegment(0.0, 5.0, "SPEAKER_00")],
            num_speakers=1,
            provider_name=self._provider,
            model_name=self._model,
            metadata={"diarize_df": frame, "overlap_info": {"regions": []}} if self._local else {},
        )


@pytest.mark.parametrize(
    ("engine", "provider", "expected"),
    [
        # The local in-process engine, even after a cloud transcription.
        ("local", "pyannote", ("local", "server", "post_stage_local")),
        ("pyannote_ai", "pyannote.ai", ("pyannote_ai", "cloud", "post_stage_cloud")),
        # A vendor named as the detector keeps its own id; the pyannote.ai STT service
        # reports ``pyannote``, the local engine's spelling, and is still the cloud vendor.
        ("asr_config", "pyannote", ("asr", "cloud", "post_stage_cloud")),
        ("asr_config", "gladia", ("asr", "cloud", "post_stage_cloud")),
    ],
)
def test_the_post_stage_records_the_engine_that_served(
    db_session, media, rediarize_seams, engine, provider, expected
):
    from app.tasks import rediarize_task as mod

    labels = _speakers(db_session, media, labels=("SPEAKER_00",))
    db_session.add(
        TranscriptSegment(
            media_file_id=media.id,
            speaker_id=labels["SPEAKER_00"],
            start_time=0.0,
            end_time=5.0,
            text="one segment",
        )
    )
    media.asr_provider = "deepgram"
    media.status = "completed"
    db_session.commit()
    detector = _FakeDetector(provider, "m-1", local=engine == "local")

    with (
        patch.object(mod, "_prepare_audio", return_value=("fake.wav", None)),
        patch.object(mod, "_build_detector", return_value=detector),
    ):
        result = mod.rediarize_task.apply(
            kwargs={"file_uuid": str(media.uuid), "engine": engine, "merge": "segment"}
        ).result

    assert result["status"] == "success", result
    db_session.refresh(media)
    assert (media.diarization_provider, media.diarization_model) == (provider, "m-1")
    diarization = _steps(db_session, media.id)["diarization"]
    assert diarization["provider"] == provider
    assert (diarization["engine"], diarization["where"], diarization["execution"]) == expected
    assert diarization["merge"] == "segment"


# ------------------------------------------------------------------ voiceprints


def test_inline_native_voiceprints_name_the_diarizer_and_the_vector_space(
    db_session, media, voiceprint_seams
):
    from app.tasks.transcription import postprocess

    media.diarization_provider = "native"
    db_session.commit()
    labels = _speakers(db_session, media)
    vectors = {label: np.ones(256).tolist() for label in labels}

    postprocess._process_native_embeddings(media.id, media.user_id, "t", vectors, labels)

    voiceprints = _steps(db_session, media.id)["voiceprints"]
    assert voiceprints["provider"] == "native"
    assert (voiceprints["model"], voiceprints["dims"]) == (WESPEAKER, 256)
    assert (voiceprints["where"], voiceprints["execution"]) == ("server", "inline_native")


def _ctx(media) -> TranscriptionContext:
    return TranscriptionContext(
        task_id="t",
        file_id=media.id,
        file_uuid=str(media.uuid),
        user_id=media.user_id,
        file_path=media.storage_path,
        file_name=media.filename,
        content_type=media.content_type,
    )


def test_rediarize_inline_voiceprints_are_recorded(db_session, media, voiceprint_seams):
    from app.tasks.transcription import embeddings

    media.diarization_provider = "pyannote"
    db_session.commit()
    labels = _speakers(db_session, media)
    vectors = {label: np.ones(256) for label in labels}

    embeddings._process_speaker_embeddings_native(_ctx(media), vectors, [], labels)

    voiceprints = _steps(db_session, media.id)["voiceprints"]
    assert (voiceprints["provider"], voiceprints["execution"]) == ("pyannote", "inline_native")


def test_the_cpu_embedding_stage_records_the_extractor_that_ran(
    db_session, media, voiceprint_seams
):
    from app.tasks import speaker_embedding_task as mod

    labels = _speakers(db_session, media)
    service = MagicMock()
    service.backend = "native"
    service.model_name = WESPEAKER
    service.get_embedding_dimension.return_value = 256
    service.extract_embeddings_for_segments.return_value = {0: [np.ones(256)], 1: [np.ones(256)]}
    service.aggregate_embeddings.side_effect = lambda vectors: vectors[0]
    segments = [
        {"speaker_id": speaker_id, "start": 0.0, "end": 1.0} for speaker_id in labels.values()
    ]
    inputs = {"file_id": media.id, "user_id": media.user_id, "processed_segments": segments}

    with (
        patch.object(mod, "_stage_audio_for_embeddings", return_value="a.wav"),
        patch.object(mod, "_set_task_progress"),
        patch(
            "app.services.speaker_embedding_service.SpeakerEmbeddingService", return_value=service
        ),
    ):
        aggregated = mod._extract_aggregated_embeddings(str(media.uuid), "t", inputs)
        mod._match_and_finalize_embeddings(inputs, "t", aggregated, None, False)

    voiceprints = _steps(db_session, media.id)["voiceprints"]
    assert (voiceprints["provider"], voiceprints["model"]) == ("native", WESPEAKER)
    assert (voiceprints["dims"], voiceprints["where"]) == (256, "server")
    assert voiceprints["execution"] == "embedding_stage"


# ------------------------------------------------------------------ LLM steps


def _llm(provider: LLMProvider, base_url: str | None, model: str) -> MagicMock:
    service = MagicMock()
    service.config = LLMConfig(provider=provider, model=model, base_url=base_url, api_key="k")
    service.user_context_window = 8192
    return service


def test_the_summary_records_its_llm_and_never_its_endpoint(db_session, media, summary_seams):
    from app.tasks import summarization as mod

    service = _llm(LLMProvider.VLLM, "http://127.0.0.1:8012/v1", "gemma-4-e4b")
    service.generate_summary.return_value = {"bluf": "b", "metadata": {}}
    inputs = {
        "file_id": media.id,
        "user_id": media.user_id,
        "filename": "f",
        "full_transcript": "text",
        "speaker_stats": {},
        "output_language": "en",
        "organization_context": "",
        "source_fingerprint": "fp",
    }

    with (
        patch.object(mod, "_load_summarization_inputs", return_value=inputs),
        patch.object(mod.LLMService, "create_from_user_settings", return_value=service),
    ):
        result = mod.summarize_transcript_task.apply(kwargs={"file_uuid": str(media.uuid)}).result

    assert result["status"] == "success", result
    summary = _steps(db_session, media.id)["summary"]
    assert (summary["provider"], summary["model"], summary["where"]) == (
        "vllm",
        "gemma-4-e4b",
        "server",
    )
    assert "8012" not in str(summary)


def test_topics_record_their_llm(db_session, media, scope_of):
    from app.schemas.topic import LLMSuggestionResponse
    from app.services import topic_extraction_service as mod

    inputs = {"user_id": media.user_id, "transcript": "t", "duration": 1.0, "output_language": "en"}
    service = mod.TopicExtractionService()
    with (
        patch.object(mod, "session_scope", scope_of),
        patch.object(service, "_load_extraction_inputs", return_value=inputs),
        patch.object(
            mod.LLMService,
            "create_from_settings",
            return_value=_llm(LLMProvider.OPENAI, None, "gpt-4.1"),
        ),
        patch.object(
            service,
            "_call_llm_for_extraction",
            return_value=LLMSuggestionResponse(suggested_collections=[], suggested_tags=[]),
        ),
    ):
        result = service.extract_topics(media.id)

    assert result is not None
    topics = _steps(db_session, media.id)["topics"]
    assert (topics["provider"], topics["model"], topics["where"]) == ("openai", "gpt-4.1", "cloud")


def test_speaker_name_suggestions_record_their_llm(db_session, media, scope_of):
    from app.tasks import speaker_identification_task as mod

    _speakers(db_session, media, labels=("SPEAKER_00",))
    service = _llm(LLMProvider.ANTHROPIC, None, "claude-x")
    service.identify_speakers.return_value = {
        "speaker_predictions": [
            {"speaker_label": "SPEAKER_00", "predicted_name": "Ada", "confidence": 0.9}
        ]
    }
    inputs = {
        "file_id": media.id,
        "user_id": media.user_id,
        "full_transcript": "t",
        "speaker_segments": [],
        "known_speakers": [],
        "metadata_context": "",
        "output_language": "en",
    }
    with (
        patch.object(mod, "session_scope", scope_of),
        patch.object(mod, "_load_identification_inputs", return_value=inputs),
        patch.object(mod, "_create_llm_service", return_value=service),
        patch.object(mod, "send_ws_event_for_file"),
    ):
        result = mod.identify_speakers_llm_task.apply(kwargs={"file_uuid": str(media.uuid)}).result

    assert result["status"] == "success", result
    names = _steps(db_session, media.id)["speaker_names"]
    assert (names["provider"], names["model"], names["where"]) == ("anthropic", "claude-x", "cloud")


def test_a_failed_speaker_name_run_records_nothing(db_session, media, scope_of):
    from app.tasks import speaker_identification_task as mod

    _speakers(db_session, media, labels=("SPEAKER_00",))
    service = _llm(LLMProvider.ANTHROPIC, None, "claude-x")
    service.identify_speakers.side_effect = RuntimeError("provider down")
    inputs = {
        "file_id": media.id,
        "user_id": media.user_id,
        "full_transcript": "t",
        "speaker_segments": [],
        "known_speakers": [],
        "metadata_context": "",
        "output_language": "en",
    }
    with (
        patch.object(mod, "session_scope", scope_of),
        patch.object(mod, "_load_identification_inputs", return_value=inputs),
        patch.object(mod, "_create_llm_service", return_value=service),
        patch.object(mod, "send_ws_event_for_file"),
    ):
        mod.identify_speakers_llm_task.apply(kwargs={"file_uuid": str(media.uuid)})

    document = db_session.execute(
        text("SELECT processing_provenance FROM media_file WHERE id = :i"), {"i": media.id}
    ).scalar()
    assert document is None
