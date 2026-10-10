"""The acoustic boundary re-check reaches the gpu-split pipeline (issue #1205).

Before: the split transcribe stage built its config from env only (admin DB values never
reached the snapshot the diarize worker rebuilds from), and the diarize worker never ran the
re-check at all. These tests drive the real stages and assert the words that come out.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock
from unittest.mock import patch

import numpy as np
import pytest

from app.services import system_settings_service
from app.tasks.transcription import pipelines
from app.transcription.config import TranscriptionConfig
from app.transcription.diarize_result import DiarizeResult
from app.transcription.engine.config import EngineConfig
from app.transcription.engine.job import RawInferenceResult
from app.transcription.engine.job import RawTranscriptResult
from app.transcription.engine.stages import _DiarizerOnlyStage
from app.transcription.engine.stages import _FinalizeStage


class _StopError(Exception):
    pass


# ----------------------------------------------------------------- config builder


@pytest.fixture
def split_stage_env(db_session, monkeypatch):
    """Everything around ``_run_transcribe_only_stage`` faked except the engine config.

    The real DB session serves the system settings, so the admin value travels the same
    path production uses; the engine itself is replaced by one that records the handoff.
    """

    @contextlib.contextmanager
    def _scope():
        yield db_session

    user_settings = {
        "min_speakers": 1,
        "max_speakers": 5,
        "vad_threshold": 0.5,
        "vad_min_silence_ms": 2000,
        "vad_min_speech_ms": 250,
        "vad_speech_pad_ms": 400,
        "hallucination_silence_threshold": None,
        "repetition_penalty": 1.0,
    }
    handoffs: list = []

    class _Engine:
        def __init__(self, config):
            pass

        def run_transcribe_only(self, pre, progress_callback=None):
            handoffs.append(pre)
            raise _StopError

    replacements = {
        (pipelines, "session_scope"): _scope,
        (pipelines, "update_task_status"): lambda *a, **k: None,
        (pipelines, "send_progress_notification"): lambda *a, **k: None,
        (pipelines, "_resolve_language_settings"): lambda c, s, t: ("en", False),
        (pipelines, "load_vocabulary_terms"): lambda *a: [],
        (pipelines, "_get_user_transcription_settings"): lambda db, uid: user_settings,
    }
    for (module, name), value in replacements.items():
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(
        TranscriptionConfig,
        "from_environment",
        classmethod(lambda cls, **o: TranscriptionConfig(device="cpu")),
    )
    monkeypatch.setattr("app.transcription.Engine", _Engine)
    return handoffs


def test_split_transcribe_stage_snapshot_carries_the_admin_db_values(db_session, split_stage_env):
    for key, value in {
        "engine.boundary_acoustic_recheck_enabled": "true",
        "engine.boundary_acoustic_cosine_margin": "0.21",
        "engine.boundary_acoustic_max_word_dur": "0.7",
    }.items():
        system_settings_service.set_setting(db_session, key, value)
    db_session.commit()

    with pytest.raises(_StopError):
        pipelines._run_transcribe_only_stage(
            MagicMock(task_id="t", file_id=1, user_id=1), "/scratch/a.wav", {}
        )

    snapshot = split_stage_env[0].config_snapshot
    assert snapshot["boundary_acoustic_recheck_enabled"] is True
    assert snapshot["boundary_acoustic_cosine_margin"] == pytest.approx(0.21)
    assert snapshot["boundary_acoustic_max_word_dur"] == pytest.approx(0.7)
    restored = EngineConfig.from_snapshot(snapshot)
    assert restored.boundary_acoustic_recheck_enabled is True
    assert restored.boundary_acoustic_cosine_margin == pytest.approx(0.21)


# ----------------------------------------------------------------- diarize stage


def _transcript() -> RawTranscriptResult:
    # "yeah" (1.0-1.4 s) is a backchannel inside the overlap region; diarization turns give
    # that span to SPEAKER_00, but its voiceprint is SPEAKER_01's.
    words = [
        {"word": "so", "start": 0.0, "end": 0.4, "score": 0.9},
        {"word": "yeah", "start": 1.0, "end": 1.4, "score": 0.9},
        {"word": "anyway", "start": 2.0, "end": 2.6, "score": 0.9},
    ]
    return RawTranscriptResult(
        task_id=None,
        audio_path="",
        audio_duration_s=4.0,
        language="en",
        raw_segments=[{"start": 0.0, "end": 2.6, "text": "so yeah anyway", "words": words}],
        local_wav_path="/scratch/a.wav",
        config_snapshot={},
        stage_timings={},
    )


def _diarizer():
    diarize_df = DiarizeResult(
        start=np.array([0.0, 3.0]),
        end=np.array([3.0, 4.0]),
        speaker=np.array(["SPEAKER_00", "SPEAKER_01"], dtype=object),
    )
    native = {"SPEAKER_00": np.array([1.0, 0.0]), "SPEAKER_01": np.array([0.0, 1.0])}
    overlap = {"count": 1, "regions": [{"start": 0.9, "end": 1.5}]}
    diarizer = MagicMock()
    diarizer.diarize.return_value = (diarize_df, overlap, native)
    diarizer.last_provider, diarizer.last_model = "native", "m"
    diarizer.embed_window.return_value = np.array([0.0, 1.0])  # sounds like SPEAKER_01
    return diarizer


def _config(enabled: bool) -> EngineConfig:
    config = EngineConfig(boundary_acoustic_recheck_enabled=enabled)
    config._transcription_config = TranscriptionConfig(device="cpu")
    return config


def _run_split(enabled: bool):
    diarizer = _diarizer()
    manager = MagicMock()
    manager.get_diarizer.return_value = diarizer
    config = _config(enabled)
    with (
        patch("app.transcription.model_manager.ModelManager.get_instance", return_value=manager),
        patch("app.utils.hardware_detection.detect_hardware", return_value=MagicMock()),
        patch("app.utils.vram_profiler.VRAMProfiler", return_value=MagicMock()),
        patch(
            "app.transcription.engine.audio_loader.load_from_shared_volume",
            return_value=np.zeros(16000 * 4, dtype=np.float32),
        ),
    ):
        raw = _DiarizerOnlyStage().run(_transcript(), config)
    return raw, diarizer, config


def _speaker_of(job_result, word: str) -> str:
    speaker: str = next(
        w["speaker"] for s in job_result.segments for w in s["words"] if w["word"] == word
    )
    return speaker


def test_diarize_worker_relabels_the_backchannel_and_finalize_applies_it():
    raw, diarizer, config = _run_split(enabled=True)

    assert diarizer.embed_window.called, "the re-check never embedded a window"
    assert raw.word_speaker_overrides == {"1.000-1.400": "SPEAKER_01"}
    # survives the Celery hand-off
    assert (
        RawInferenceResult.deserialize(raw.serialize()).word_speaker_overrides
        == raw.word_speaker_overrides
    )

    job = _FinalizeStage().run(raw, config)

    assert _speaker_of(job, "yeah") == "SPEAKER_01"
    assert _speaker_of(job, "so") == "SPEAKER_00"
    assert _speaker_of(job, "anyway") == "SPEAKER_00"


def test_diarize_worker_leaves_labels_alone_when_the_recheck_is_off():
    """Control: same audio, same stub; only the admin flag differs."""
    raw, diarizer, config = _run_split(enabled=False)

    assert not diarizer.embed_window.called
    assert raw.word_speaker_overrides is None
    job = _FinalizeStage().run(raw, config)
    assert _speaker_of(job, "yeah") == "SPEAKER_00"


def test_a_failing_recheck_keeps_the_max_overlap_labels():
    diarizer = _diarizer()
    diarizer.embed_window.side_effect = RuntimeError("boom")
    manager = MagicMock()
    manager.get_diarizer.return_value = diarizer
    config = _config(True)
    with (
        patch("app.transcription.model_manager.ModelManager.get_instance", return_value=manager),
        patch("app.utils.hardware_detection.detect_hardware", return_value=MagicMock()),
        patch("app.utils.vram_profiler.VRAMProfiler", return_value=MagicMock()),
        patch(
            "app.transcription.engine.audio_loader.load_from_shared_volume",
            return_value=np.zeros(16000 * 4, dtype=np.float32),
        ),
    ):
        raw = _DiarizerOnlyStage().run(_transcript(), config)

    assert raw.word_speaker_overrides is None
    assert _speaker_of(_FinalizeStage().run(raw, config), "yeah") == "SPEAKER_00"
