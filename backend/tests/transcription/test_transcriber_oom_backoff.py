"""OOM backoff in the Whisper stage (issue #1081).

A CUDA OOM during transcription used to fail the attempt outright. It now frees the cached
allocator memory, halves the batch size and retries the stage, at most
``GPU_OOM_MAX_HALVINGS`` (default 2) times, before failing. The stage also reserves its VRAM
estimate from the admission budget, sized by the batch it is about to run.
"""

from __future__ import annotations

import contextlib
import logging
from unittest.mock import MagicMock

import pytest

from app.transcription import transcriber as transcriber_mod
from app.transcription.config import TranscriptionConfig
from app.transcription.transcriber import Transcriber

CT2_OOM = "CUDA failed with error out of memory"


def _segment(start: float, end: float):
    seg = MagicMock()
    seg.start, seg.end, seg.words, seg.text = start, end, [], " hi"
    return seg


class _Pipeline:
    """Records the batch size of every call; raises OOM for the sizes it is told to."""

    def __init__(self, oom_at: set[int], error: Exception | None = None):
        self.oom_at = oom_at
        self.error = error
        self.batch_sizes: list[int] = []

    def transcribe(self, audio, **kwargs):
        batch = kwargs["batch_size"]
        self.batch_sizes.append(batch)

        def _gen():
            if self.error is not None:
                raise self.error
            if batch in self.oom_at:
                # CTranslate2 raises from inside the generator, mid-decode.
                raise RuntimeError(CT2_OOM)
            yield _segment(0.0, 1.0)

        return _gen(), MagicMock(language="en")


@pytest.fixture
def cuda_transcriber(monkeypatch):
    monkeypatch.delenv("GPU_OOM_MAX_HALVINGS", raising=False)
    config = TranscriptionConfig(model_name="large-v3-turbo", device="cuda", batch_size=16)
    t = Transcriber.__new__(Transcriber)
    t.config = config
    t._model = MagicMock()

    admitted: list[tuple[str, int]] = []

    @contextlib.contextmanager
    def _fake_admit(stage, *, device, batch_size=None, **_kwargs):
        admitted.append((stage, batch_size))
        yield 0

    freed: list[bool] = []
    monkeypatch.setattr(transcriber_mod.vram_budget, "admit", _fake_admit)
    monkeypatch.setattr(transcriber_mod.cuda_health, "free_cached_vram", lambda: freed.append(True))
    monkeypatch.setattr(
        transcriber_mod, "_resolve_task_and_language", lambda *a, **k: ("transcribe", None)
    )
    return t, admitted, freed


def test_an_oom_halves_the_batch_and_retries(cuda_transcriber, caplog):
    t, admitted, freed = cuda_transcriber
    t._pipeline = _Pipeline(oom_at={16})

    with caplog.at_level(logging.WARNING, logger="app.transcription.transcriber"):
        result = t.transcribe([0.0] * 16000)

    assert t._pipeline.batch_sizes == [16, 8]
    assert len(result["segments"]) == 1
    assert freed == [True], "cached VRAM must be released before the retry"
    assert admitted == [("asr", 16), ("asr", 8)], "each attempt reserves for its own batch"
    assert "batch_size 16 -> 8" in caplog.text
    # The shared, cached transcriber's configured batch size is not mutated for later tasks.
    assert t.config.batch_size == 16


def test_it_gives_up_after_two_halvings(cuda_transcriber):
    t, _, freed = cuda_transcriber
    t._pipeline = _Pipeline(oom_at={16, 8, 4, 2, 1})

    with pytest.raises(RuntimeError, match="out of memory"):
        t.transcribe([0.0] * 16000)

    assert t._pipeline.batch_sizes == [16, 8, 4]
    assert len(freed) == 2


def test_the_halving_limit_is_env_tunable(cuda_transcriber, monkeypatch):
    t, _, _ = cuda_transcriber
    monkeypatch.setenv("GPU_OOM_MAX_HALVINGS", "0")
    t._pipeline = _Pipeline(oom_at={16})
    with pytest.raises(RuntimeError, match="out of memory"):
        t.transcribe([0.0] * 16000)
    assert t._pipeline.batch_sizes == [16]


def test_a_non_oom_error_is_not_retried(cuda_transcriber):
    t, _, freed = cuda_transcriber
    t._pipeline = _Pipeline(oom_at=set(), error=RuntimeError("cudaErrorInvalidDevice"))
    with pytest.raises(RuntimeError, match="cudaErrorInvalidDevice"):
        t.transcribe([0.0] * 16000)
    assert t._pipeline.batch_sizes == [16]
    assert freed == []


def test_an_oom_on_the_cpu_is_not_retried(cuda_transcriber):
    t, admitted, _ = cuda_transcriber
    t.config.device = "cpu"
    t._pipeline = _Pipeline(oom_at={16})
    with pytest.raises(RuntimeError, match="out of memory"):
        t.transcribe([0.0] * 16000)
    assert t._pipeline.batch_sizes == [16]
