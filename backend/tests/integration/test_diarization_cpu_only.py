"""In-process diarization must work on a CPU-only host (issue #1007).

The pinned pyannote fork's ``SpeakerDiarization._gpu_empty_cache()`` fell through to
``torch.mps.empty_cache()`` whenever CUDA was unavailable. With no MPS backend (every
CPU-only Linux host) that raises ``RuntimeError: Cannot execute emptyCache() without MPS
backend``, and ``apply()`` calls it twice, so every in-process diarization on a CPU worker
failed with ``Speaker diarization failed: Cannot execute emptyCache() ...``. Issue #1003
only guarded the model downloader; the worker path was still broken. The fix lives in the
fork, so these tests exercise the INSTALLED fork: against the old pin both fail.

``torch.cuda.is_available`` is forced False rather than trusting the ambient device: the
suite's CUDA guard already hides GPUs, but its ``OT_TEST_CUDA_VISIBLE_DEVICES`` escape
hatch would otherwise send ``_gpu_empty_cache()`` down the CUDA branch and past the bug.

Needs ``pyannote.audio`` (in ``backend/venv`` and the backend images; deliberately absent
from ``requirements-ci.txt``). The end-to-end test also needs the cached
``speaker-diarization-community-1`` weights, hence ``models``.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

SAMPLE_WAV = Path(__file__).resolve().parent.parent / "fixtures" / "media" / "sample_short.wav"


@pytest.fixture
def cpu_only_torch(monkeypatch):
    """torch as a CPU-only Linux worker sees it: no CUDA and no MPS backend."""
    torch = pytest.importorskip("torch")
    pytest.importorskip("pyannote.audio", reason="pyannote.audio fork not installed")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    if torch.backends.mps.is_available():
        pytest.skip("host has an MPS backend; the CPU-only crash needs a host without one")
    return torch


def test_fork_cache_release_is_a_noop_on_cpu_only_host(cpu_only_torch) -> None:
    """The fork's cache-release hook must not touch the (absent) MPS backend."""
    from pyannote.audio.pipelines.speaker_diarization import SpeakerDiarization

    with pytest.raises(RuntimeError, match="without MPS backend"):
        cpu_only_torch.mps.empty_cache()

    SpeakerDiarization._gpu_empty_cache()


def _load_sample() -> np.ndarray:
    with wave.open(str(SAMPLE_WAV)) as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


@pytest.mark.models
def test_in_process_diarization_runs_on_cpu(cpu_only_torch, monkeypatch) -> None:
    """The CPU worker's real path: SpeakerDiarizer on ``diarization_device='cpu'``."""
    from huggingface_hub import try_to_load_from_cache

    from app.transcription.config import TranscriptionConfig
    from app.transcription.diarizer import PYANNOTE_V4_MODEL
    from app.transcription.diarizer import SpeakerDiarizer

    if not isinstance(try_to_load_from_cache(PYANNOTE_V4_MODEL, "config.yaml"), str):
        pytest.skip(f"{PYANNOTE_V4_MODEL} weights are not in the local Hugging Face cache")

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    diarizer = SpeakerDiarizer(
        TranscriptionConfig(device="cpu", diarization_device="cpu", compute_type="int8")
    )
    diarizer.load_model()
    assert diarizer.last_model == PYANNOTE_V4_MODEL

    audio = _load_sample()
    duration = len(audio) / 16000.0
    result, _, _ = diarizer.diarize(audio)

    assert len(result.start) > 0, "CPU diarization produced no segments"
    assert len(result.start) == len(result.end) == len(result.speaker)
    assert float(result.start.min()) >= 0.0
    assert float(result.end.max()) <= duration + 0.1
    assert all(str(s).startswith("SPEAKER_") for s in result.speaker)
