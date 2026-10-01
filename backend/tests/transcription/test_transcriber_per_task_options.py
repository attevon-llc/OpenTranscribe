"""Per-task decode options on a shared, cached transcriber (issue #1117).

A GPU worker preloads one ``Transcriber`` and every task reuses it. The decode options
(language, translate, beam size, VAD, accuracy settings and the custom vocabulary) belong to
the task being processed, not to the config the weights were loaded with, so every decode
call must receive the options of its own task: one after another, and on concurrent threads.
"""

from __future__ import annotations

import contextlib
import threading
from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from unittest.mock import MagicMock
from unittest.mock import patch

import numpy as np
import pytest

from app.transcription import transcriber as transcriber_mod
from app.transcription.config import TranscriptionConfig
from app.transcription.engine.job import PreprocessResult
from app.transcription.model_manager import ModelManager
from app.transcription.transcriber import Transcriber

if TYPE_CHECKING:
    from app.transcription.engine.config import EngineConfig

MODEL = "large-v3"  # supports translation, multilingual


def _segment():
    seg = MagicMock()
    seg.start, seg.end, seg.words, seg.text = 0.0, 1.0, [], " hola"
    return seg


class _RecordingPipeline:
    """Stands in for BatchedInferencePipeline; records each call's kwargs."""

    def __init__(self, barrier: threading.Barrier | None = None):
        self.calls: list[dict] = []
        self.barrier = barrier
        self._lock = threading.Lock()

    def transcribe(self, audio, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
        if self.barrier is not None:
            # Hold every thread inside its decode until all of them are in it, so the
            # decodes genuinely overlap.
            self.barrier.wait(timeout=5)
        detected = kwargs.get("language") or "fr"
        return iter([_segment()]), MagicMock(language=detected)


@pytest.fixture(autouse=True)
def _no_vram_admission(monkeypatch):
    @contextlib.contextmanager
    def _admit(stage, *, device, batch_size=None, **_kwargs):
        yield 0

    monkeypatch.setattr(transcriber_mod.vram_budget, "admit", _admit)


@pytest.fixture
def manager(monkeypatch):
    """A ModelManager whose load_model installs a recording pipeline instead of weights."""
    loads: list[TranscriptionConfig] = []
    pipeline = _RecordingPipeline()

    def _fake_load(self):
        loads.append(self.config)
        self._model = MagicMock()
        self._pipeline = pipeline

    monkeypatch.setattr(Transcriber, "load_model", _fake_load)
    mgr = ModelManager()
    return mgr, pipeline, loads


def _preload() -> TranscriptionConfig:
    # What worker_ready builds: env defaults, no task overrides.
    return TranscriptionConfig(model_name=MODEL, device="cpu", source_language="auto")


def _task(**overrides: Any) -> TranscriptionConfig:
    base: dict[str, Any] = {"model_name": MODEL, "device": "cpu"}
    base.update(overrides)
    return TranscriptionConfig(**base)


def _audio() -> np.ndarray:
    return np.zeros(16000, dtype=np.float32)


def _decode(mgr: ModelManager, tc: TranscriptionConfig) -> dict:
    """What every engine stage does: fetch the cached transcriber, decode this task."""
    return mgr.get_transcriber(tc).transcribe(_audio(), options=tc)


def test_sequential_tasks_each_decode_with_their_own_language_and_task(manager):
    mgr, pipeline, loads = manager
    mgr.ensure_models_loaded = MagicMock()  # diarizer not under test
    mgr.get_transcriber(_preload())

    _decode(mgr, _task(source_language="en"))
    _decode(mgr, _task(source_language="es", translate_to_english=True))
    _decode(mgr, _task(source_language="es", translate_to_english=False))
    _decode(mgr, _task(source_language="auto"))

    assert len(loads) == 1, "per-task options must not reload the model"
    got = [(c["language"], c["task"]) for c in pipeline.calls]
    assert got == [
        ("en", "transcribe"),
        ("es", "translate"),
        ("es", "transcribe"),
        (None, "transcribe"),
    ]


def test_auto_detect_returns_the_detected_language_even_when_preloaded_with_a_language(
    manager,
):
    mgr, pipeline, _ = manager
    mgr.get_transcriber(_task(source_language="en"))  # preload pinned to English

    result = _decode(mgr, _task(source_language="auto"))

    assert pipeline.calls[-1]["language"] is None
    assert result["language"] == "fr"  # what the (fake) model detected


def test_other_per_task_decode_settings_reach_the_decode(manager):
    mgr, pipeline, _ = manager
    mgr.get_transcriber(_preload())

    tc = _task(
        source_language="de",
        beam_size=2,
        vad_threshold=0.3,
        vad_min_silence_ms=500,
        vad_min_speech_ms=100,
        vad_speech_pad_ms=50,
        repetition_penalty=1.3,
        hallucination_silence_threshold=2.0,
    )
    _decode(mgr, tc)

    call = pipeline.calls[-1]
    assert call["beam_size"] == 2
    assert call["vad_parameters"] == {
        "threshold": 0.3,
        "min_silence_duration_ms": 500,
        "min_speech_duration_ms": 100,
        "speech_pad_ms": 50,
    }
    assert call["repetition_penalty"] == 1.3
    assert call["hallucination_silence_threshold"] == 2.0


def test_each_task_decodes_with_its_own_vocabulary(manager):
    mgr, pipeline, _ = manager
    mgr.get_transcriber(_preload())

    _decode(mgr, _task(source_language="en", vocabulary=("Kubernetes", "OpenTranscribe")))
    _decode(mgr, _task(source_language="en", vocabulary=("myocardial infarction",)))
    _decode(mgr, _task(source_language="en", vocabulary=None))

    hotwords = [c.get("hotwords") for c in pipeline.calls]
    assert hotwords == ["Kubernetes, OpenTranscribe", "myocardial infarction", None]


def test_concurrent_threads_never_see_each_others_options(manager):
    mgr, _, _ = manager
    threads_n = 4
    pipeline = _RecordingPipeline(barrier=threading.Barrier(threads_n))
    transcriber = mgr.get_transcriber(_preload())
    transcriber._pipeline = pipeline

    tasks = {
        "en": _task(source_language="en", vocabulary=("alpha",)),
        "es": _task(source_language="es", translate_to_english=True, vocabulary=("beta",)),
        "de": _task(source_language="de", vocabulary=("gamma",)),
        "auto": _task(source_language="auto", vocabulary=None),
    }
    results: dict[str, dict] = {}
    errors: list[BaseException] = []

    def _run(key: str) -> None:
        try:
            results[key] = _decode(mgr, tasks[key])
        except BaseException as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    workers = [threading.Thread(target=_run, args=(k,)) for k in tasks]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=10)

    assert not errors
    assert len(pipeline.calls) == threads_n
    seen = {(c["language"], c["task"], c.get("hotwords")) for c in pipeline.calls}
    assert seen == {
        ("en", "transcribe", "alpha"),
        ("es", "translate", "beta"),
        ("de", "transcribe", "gamma"),
        (None, "transcribe", None),
    }
    assert {k: r["language"] for k, r in results.items()} == {
        "en": "en",
        "es": "es",
        "de": "de",
        "auto": "fr",
    }


def test_without_options_the_load_config_still_drives_the_decode(manager):
    """Single-shot callers (probe scripts) that build one transcriber per run still work."""
    mgr, pipeline, _ = manager
    t = mgr.get_transcriber(_task(source_language="it"))
    t.transcribe(_audio())
    assert pipeline.calls[-1]["language"] == "it"


# --------------------------------------------------------------------------- #
# The engine stages hand the task's config to the decode                     #
# --------------------------------------------------------------------------- #


def _pre() -> PreprocessResult:
    return PreprocessResult(
        task_id="task-1117",
        file_id=1,
        user_id=1,
        local_wav_path="/scratch/task-1117.wav",
        minio_temp_object="",
        audio_duration_s=1.0,
        audio_sample_rate=16000,
        audio_channels=1,
        audio_size_bytes=32000,
        vad_regions=None,
        config_snapshot={},
        stage1_timings={},
    )


class _StubEngineConfig:
    boundary_acoustic_recheck_enabled = False

    def __init__(self, tc: TranscriptionConfig):
        self.transcription_config = tc


@pytest.mark.parametrize("stage_name", ["_GpuRawStage", "_TranscribeOnlyStage", "_GpuStage"])
def test_engine_stages_pass_the_task_config_to_the_decode(stage_name):
    from app.transcription.engine import stages as stages_mod
    from app.transcription.engine.job import JobSpec

    tc = _task(source_language="es", translate_to_english=True, enable_diarization=False)
    decoded_with: list[TranscriptionConfig | None] = []

    class _SharedTranscriber:
        def transcribe(self, audio, options=None):
            decoded_with.append(options)
            return {"segments": [], "language": "es"}

    mgr = MagicMock()
    mgr.get_transcriber.return_value = _SharedTranscriber()

    with (
        patch("app.transcription.model_manager.ModelManager.get_instance", return_value=mgr),
        patch(
            "app.transcription.engine.audio_loader.load_from_shared_volume",
            return_value=_audio(),
        ),
        patch("app.transcription.audio.load_audio", return_value=_audio()),
        patch("app.utils.hardware_detection.detect_hardware", return_value=MagicMock()),
    ):
        stage = getattr(stages_mod, stage_name)()
        config = cast("EngineConfig", _StubEngineConfig(tc))
        if stage_name == "_GpuStage":
            stage.run(JobSpec(audio_path="/scratch/x.wav", task_id="task-1117"), config)
        else:
            stage.run(_pre(), config)

    assert decoded_with == [tc]
    assert decoded_with[0] is tc


# --------------------------------------------------------------------------- #
# The per-file settings survive the cross-stage snapshot                      #
# --------------------------------------------------------------------------- #


def test_snapshot_round_trip_keeps_every_per_file_decode_setting():
    from app.transcription.engine.config import EngineConfig

    tc = _task(
        source_language="es",
        translate_to_english=True,
        beam_size=3,
        vad_threshold=0.42,
        vad_min_silence_ms=700,
        vad_min_speech_ms=120,
        vad_speech_pad_ms=90,
        hallucination_silence_threshold=1.5,
        repetition_penalty=1.2,
        vocabulary=("Kubernetes", "OpenTranscribe"),
    )
    engine = EngineConfig()
    engine._transcription_config = tc

    restored = EngineConfig.from_snapshot(engine.to_snapshot()).transcription_config

    for field in (
        "source_language",
        "translate_to_english",
        "beam_size",
        "vad_threshold",
        "vad_min_silence_ms",
        "vad_min_speech_ms",
        "vad_speech_pad_ms",
        "hallucination_silence_threshold",
        "repetition_penalty",
        "vocabulary",
    ):
        assert getattr(restored, field) == getattr(tc, field), field


def test_vocabulary_does_not_change_the_model_cache_key():
    a = _task(vocabulary=("alpha",), source_language="en")
    b = _task(vocabulary=None, source_language="es", translate_to_english=True)
    assert a.config_hash() == b.config_hash()
