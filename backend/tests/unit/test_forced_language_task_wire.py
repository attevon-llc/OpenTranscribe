"""The GPU task hands a forced language to the cloud provider adapter (issue #1263).

Drives ``transcribe_gpu_task`` itself, the code that reads ``source_language`` out of the
dispatch context and passes it on, with a cloud plan. The saved language is ``en``; the forced
one is ``fr``. Deepgram-style adapters take the bare code; the real Azure adapter is run against
a recording SDK and must be handed the locale ``fr-FR``.
"""

from __future__ import annotations

import sys
import types
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.services.asr.azure_provider import AzureASRProvider
from app.services.asr.types import ASRConfig
from tests.unit._asr_stubs import StubASRProvider
from tests.unit._plan_contexts import make_plan

_CORE = "app.tasks.transcription.core"


class _StopError(Exception):
    pass


class _RecordingProvider(StubASRProvider):
    def __init__(self) -> None:
        super().__init__("deepgram")
        self.config: ASRConfig | None = None

    def transcribe(self, audio_path, config, progress_callback=None):
        self.config = config
        raise _StopError


@contextmanager
def _no_session():
    yield None


def _fake_azure_sdk(seen: dict) -> types.ModuleType:
    class _SpeechConfig:
        def __init__(self, subscription, region):
            seen["config"] = self

    class _OutputFormat:
        Detailed = 1

    class _AudioConfig:
        def __init__(self, filename):
            raise _StopError

    sdk = types.ModuleType("azure.cognitiveservices.speech")
    sdk.SpeechConfig = _SpeechConfig  # type: ignore[attr-defined]
    sdk.OutputFormat = _OutputFormat  # type: ignore[attr-defined]
    sdk.audio = types.SimpleNamespace(AudioConfig=_AudioConfig)  # type: ignore[attr-defined]
    return sdk


def _run_task(provider, source_language, saved_language="en") -> None:
    from app.tasks.transcription import core

    context = {
        "task_id": f"task-{uuid.uuid4().hex[:8]}",
        "file_uuid": str(uuid.uuid4()),
        "file_id": 1,
        "user_id": 1,
        "storage_path": "files/test.wav",
        "file_name": "test.wav",
        "content_type": "audio/wav",
        "pipeline_plan": make_plan("cloud_speakers", "provider").to_context(),
        "source_language": source_language,
        "min_speakers": None,
        "max_speakers": None,
        "num_speakers": None,
    }

    def _download(file_uuid, path):
        open(path, "wb").close()

    with (
        patch(f"{_CORE}.session_scope", _no_session),
        patch(f"{_CORE}.update_task_status"),
        patch(f"{_CORE}.send_progress_notification"),
        patch("app.tasks.transcription.cloud_asr.session_scope", _no_session),
        patch("app.tasks.transcription.cloud_asr.update_task_status"),
        patch("app.tasks.transcription.cloud_asr.send_progress_notification"),
        patch(
            "app.tasks.transcription.cloud_asr._get_user_language_settings",
            return_value={"source_language": saved_language, "translate_to_english": False},
        ),
        patch(
            "app.tasks.transcription.cloud_asr._get_user_transcription_settings",
            return_value={"min_speakers": 1, "max_speakers": 20, "num_speakers": None},
        ),
        patch("app.tasks.transcription.cloud_asr.load_vocabulary_terms", return_value=[]),
        patch(f"{_CORE}.get_refreshed_object", return_value=None),
        patch(f"{_CORE}._superseded_or_none", return_value=None),
        patch(f"{_CORE}._cloud_asr_provider_or_none", return_value=provider),
        patch("app.services.minio_service.download_temp_audio", _download),
        pytest.raises(_StopError),  # the adapter stops the run once it has been handed its config
    ):
        core.transcribe_gpu_task.__wrapped__(context)


def test_the_forced_language_reaches_a_cloud_provider_adapter():
    provider = _RecordingProvider()
    _run_task(provider, "fr")
    assert provider.config is not None
    assert provider.config.language == "fr"


def test_without_a_forced_language_the_saved_one_reaches_the_adapter():
    provider = _RecordingProvider()
    _run_task(provider, None)
    assert provider.config is not None
    assert provider.config.language == "en"


def test_azure_is_handed_a_locale_for_the_forced_language(monkeypatch):
    seen: dict = {}
    monkeypatch.setitem(sys.modules, "azure", types.ModuleType("azure"))
    monkeypatch.setitem(
        sys.modules, "azure.cognitiveservices", types.ModuleType("azure.cognitiveservices")
    )
    monkeypatch.setitem(sys.modules, "azure.cognitiveservices.speech", _fake_azure_sdk(seen))
    provider = AzureASRProvider(api_key="k", region="eastus")
    _run_task(provider, "fr")
    assert seen["config"].speech_recognition_language == "fr-FR"
