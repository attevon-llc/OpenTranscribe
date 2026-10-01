"""Custom vocabulary reaches the local Whisper decode on every local path (issue #1117).

The vocabulary used to be loaded for cloud ASR providers only; the local pipelines built
their config without it, so a user's (or their organization's) terms never reached
faster-whisper. Each builder must resolve the file's vocabulary per task and carry it to the
decode, through the cross-stage snapshot where the GPU stage rebuilds its config.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import pytest

from app.tasks.transcription import cpu_task
from app.tasks.transcription import pipelines
from app.transcription import config as tconfig
from app.transcription.config import TranscriptionConfig
from app.transcription.engine.config import EngineConfig

USER_SETTINGS = {
    "min_speakers": 1,
    "max_speakers": 5,
    "vad_threshold": 0.4,
    "vad_min_silence_ms": 900,
    "vad_min_speech_ms": 200,
    "vad_speech_pad_ms": 300,
    "hallucination_silence_threshold": 2.5,
    "repetition_penalty": 1.1,
}


class _StopError(Exception):
    """Raised by the fakes once the config under test has been captured."""


def _build(**overrides) -> TranscriptionConfig:
    tc = TranscriptionConfig(device="cpu")
    for key, value in overrides.items():
        if hasattr(tc, key):
            setattr(tc, key, value)
    return tc


@pytest.fixture
def ctx():
    return MagicMock(task_id="task-1117", file_id=7, user_id=3)


@pytest.fixture
def vocab_calls(monkeypatch):
    calls: list[tuple[int, int]] = []

    def _load(db, user_id, file_id):
        calls.append((user_id, file_id))
        return [f"term-for-file-{file_id}", "  ", "Kubernetes"]

    @contextlib.contextmanager
    def _scope():
        yield MagicMock()

    for mod in (pipelines, cpu_task):
        monkeypatch.setattr(mod, "session_scope", _scope)
        monkeypatch.setattr(mod, "_get_user_transcription_settings", lambda db, uid: USER_SETTINGS)
        monkeypatch.setattr(mod, "_resolve_language_settings", lambda c, s, t: ("es", True))
        monkeypatch.setattr(mod, "update_task_status", lambda *a, **k: None)
        monkeypatch.setattr(mod, "send_progress_notification", lambda *a, **k: None)
    monkeypatch.setattr(pipelines, "load_vocabulary_terms", _load)
    monkeypatch.setattr(tconfig.TranscriptionConfig, "from_environment", classmethod(_from_env))
    monkeypatch.setattr(tconfig.TranscriptionConfig, "for_cpu_lightweight", classmethod(_from_env))
    return calls


def _from_env(cls, **overrides):
    return _build(**overrides)


def _capture_pipeline(monkeypatch) -> list[TranscriptionConfig]:
    seen: list[TranscriptionConfig] = []

    class _Pipeline:
        def __init__(self, config):
            seen.append(config)
            raise _StopError

    monkeypatch.setattr("app.transcription.TranscriptionPipeline", _Pipeline)
    return seen


def _capture_engine(monkeypatch) -> list:
    seen: list = []

    class _Engine:
        def __init__(self, config):
            pass

        def run_gpu_stage(self, pre, progress_callback=None):
            seen.append(pre)
            raise _StopError

        def run_transcribe_only(self, pre, progress_callback=None):
            seen.append(pre)
            raise _StopError

    monkeypatch.setattr("app.transcription.Engine", _Engine)
    monkeypatch.setattr(
        EngineConfig, "from_db_with_env_fallback", classmethod(lambda cls, db: EngineConfig())
    )

    def _engine_from_env(cls, **overrides):
        engine = EngineConfig()
        engine._transcription_config = _build(**overrides)
        return engine

    monkeypatch.setattr(EngineConfig, "from_environment", classmethod(_engine_from_env))
    return seen


def _assert_task_settings(tc: TranscriptionConfig) -> None:
    assert tc.vocabulary == ("term-for-file-7", "Kubernetes")
    assert tc.source_language == "es"
    assert tc.translate_to_english is True


def test_monolithic_local_pipeline_carries_the_files_vocabulary(ctx, vocab_calls, monkeypatch):
    seen = _capture_pipeline(monkeypatch)
    with pytest.raises(_StopError):
        pipelines._run_transcription_pipeline(ctx, "/scratch/a.wav", None, None, None)
    _assert_task_settings(seen[0])
    assert vocab_calls == [(3, 7)]


def test_cpu_pipeline_carries_the_files_vocabulary(ctx, vocab_calls, monkeypatch):
    seen = _capture_pipeline(monkeypatch)
    with pytest.raises(_StopError):
        cpu_task._run_cpu_transcription(ctx, "/scratch/a.wav")
    _assert_task_settings(seen[0])
    assert vocab_calls == [(3, 7)]


@pytest.mark.parametrize("builder", ["_run_engine_pipeline", "_run_transcribe_only_stage"])
def test_engine_paths_carry_the_files_settings_through_the_snapshot(
    ctx, vocab_calls, monkeypatch, builder
):
    seen = _capture_engine(monkeypatch)
    with pytest.raises(_StopError):
        getattr(pipelines, builder)(ctx, "/scratch/a.wav", {})

    # The GPU stage rebuilds its config from this snapshot, so this is what it decodes with.
    restored = EngineConfig.from_snapshot(seen[0].config_snapshot).transcription_config
    _assert_task_settings(restored)
    assert restored.vad_threshold == 0.4
    assert restored.vad_min_silence_ms == 900
    assert restored.hallucination_silence_threshold == 2.5
    assert restored.repetition_penalty == 1.1
    assert vocab_calls == [(3, 7)]
