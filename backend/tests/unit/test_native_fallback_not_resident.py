"""With diar-native selected, the in-process PyAnnote fallback is never resident beside it.

PyAnnote is the fallback for a sidecar outage, loaded lazily. Two paths used to leave it in
VRAM for the worker's whole life, next to the sidecar's own models: the worker-start preload
(when the sidecar was not ready yet) and a single mid-run sidecar failure (the fallback was
cached on the shared native diarizer and never released, and every later job's
``embed_window`` kept routing to it). On a GPU packed with concurrent jobs that VRAM is what
the next Whisper decode needs. ``FakeSpeakerDiarizer`` substitutes only the weights load.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.transcription import diarizer as diarizer_mod
from app.transcription import diarizer_native
from app.transcription.config import TranscriptionConfig
from app.transcription.diarize_result import DiarizeResult
from app.transcription.diarizer_native import NativeSpeakerDiarizer
from app.transcription.model_manager import ModelManager

pytestmark = pytest.mark.xdist_group("diar_native_state")


class FakeSpeakerDiarizer:
    instances: list[FakeSpeakerDiarizer] = []

    def __init__(self, config):
        self.is_loaded = False
        FakeSpeakerDiarizer.instances.append(self)

    def load_model(self) -> None:
        self.is_loaded = True

    def unload_model(self) -> None:
        self.is_loaded = False

    def diarize(self, audio):
        return (
            DiarizeResult(
                start=np.array([0.0]),
                end=np.array([1.0]),
                speaker=np.array(["SPEAKER_00"], dtype=object),
            ),
            {"count": 0, "duration": 0.0, "regions": []},
            None,
        )

    def embed_window(self, audio, start, end):
        return np.array([0.1, 0.2], dtype=np.float32)


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch):
    """Sidecar readiness the test controls; PyAnnote replaced by the fake."""
    state = {"ready": True}
    monkeypatch.setattr(diarizer_native, "sidecar_ready", lambda *a, **k: state["ready"])
    monkeypatch.setattr(diarizer_native, "sidecar_healthy", lambda *a, **k: state["ready"])
    monkeypatch.setattr(diarizer_native, "diarizer_require_sidecar", lambda: False)
    monkeypatch.setattr(diarizer_mod, "SpeakerDiarizer", FakeSpeakerDiarizer)
    monkeypatch.setattr("app.transcription.model_manager.SpeakerDiarizer", FakeSpeakerDiarizer)
    monkeypatch.setattr(ModelManager, "get_transcriber", lambda self, config: None)
    monkeypatch.setattr(ModelManager, "_cleanup_gpu", lambda self: None)
    FakeSpeakerDiarizer.instances.clear()
    return state


def _config() -> TranscriptionConfig:
    return TranscriptionConfig(diarizer_backend="native", device="cpu")


def test_preload_does_not_load_pyannote_while_the_sidecar_is_not_ready(sidecar):
    sidecar["ready"] = False
    manager = ModelManager()

    manager.ensure_models_loaded(_config())

    assert FakeSpeakerDiarizer.instances == []
    assert manager._diarizer is None


def test_preload_builds_only_the_native_client_when_the_sidecar_is_ready(sidecar):
    manager = ModelManager()

    manager.ensure_models_loaded(_config())

    assert isinstance(manager._diarizer, NativeSpeakerDiarizer)
    assert FakeSpeakerDiarizer.instances == []


def test_a_fallback_after_a_sidecar_failure_is_released_once_it_serves_again(sidecar):
    manager = ModelManager()
    config = _config()
    native = manager.get_diarizer(config)
    assert isinstance(native, NativeSpeakerDiarizer)

    # One job hits a sidecar failure and falls back in-process.
    native._refuse_or_fallback_impl(RuntimeError("connection reset"), np.zeros(16000), True)
    fallback = native._fallback
    assert fallback is not None and fallback.is_loaded

    # The next job finds the sidecar serving: same native client, fallback unloaded.
    assert manager.get_diarizer(config) is native
    assert native._fallback is None
    assert not fallback.is_loaded


def test_the_fallback_is_not_released_while_a_job_is_using_it(sidecar):
    native = NativeSpeakerDiarizer(_config())
    native._fallback_engine()
    native._fallback_users = 1

    assert native.release_fallback() is False
    assert native._fallback is not None

    native._fallback_users = 0
    assert native.release_fallback() is True
    assert native._fallback is None
