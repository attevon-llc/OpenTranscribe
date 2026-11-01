"""A recording with no speech transcribes to nothing; it does not crash (v0.6.0 audit).

The model downloader failed in the published v0.6.0 image because whisperx's
``FasterWhisperPipeline.transcribe`` hands an empty VAD result to transformers 5.x, which
raises ``IndexError`` on the empty batch. The backend does not use whisperx:
``Transcriber`` decodes through faster-whisper's ``BatchedInferencePipeline``, which is not a
transformers pipeline and handles an empty VAD result itself (an empty chunk list, and a
dummy feature for language detection). That is a property of a pinned third-party package,
so it is pinned here, through the production ``Transcriber``, on real weights: silence and
low noise with an explicit language and with auto-detect, the two branches the empty path
takes.

Needs the ``tiny`` faster-whisper weights (fetched to the Hugging Face cache on first run,
~75 MB) and ``faster-whisper``, which every backend image and the venv install.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from app.transcription.config import TranscriptionConfig
from app.transcription.transcriber import Transcriber

pytestmark = [pytest.mark.integration, pytest.mark.models]

SAMPLE_RATE = 16000
SAMPLE_WAV = Path(__file__).resolve().parent.parent / "fixtures" / "media" / "sample_short.wav"


@pytest.fixture(scope="module")
def tiny_transcriber():
    pytest.importorskip("faster_whisper", reason="faster-whisper is in every backend image")
    config = TranscriptionConfig(
        model_name="tiny", device="cpu", compute_type="int8", batch_size=4, source_language="en"
    )
    transcriber = Transcriber(config)
    transcriber.load_model()
    try:
        yield transcriber
    finally:
        transcriber.unload_model()


def _no_speech(kind: str) -> np.ndarray:
    seconds = 20
    if kind == "silence":
        return np.zeros(SAMPLE_RATE * seconds, dtype=np.float32)
    rng = np.random.default_rng(1234)
    return (0.001 * rng.standard_normal(SAMPLE_RATE * seconds)).astype(np.float32)


@pytest.mark.parametrize("kind", ["silence", "low-noise"])
@pytest.mark.parametrize("language", ["en", "auto"])
def test_audio_without_speech_returns_no_segments(tiny_transcriber, kind, language):
    options = TranscriptionConfig(
        model_name="tiny",
        device="cpu",
        compute_type="int8",
        batch_size=4,
        source_language=language,
    )

    result = tiny_transcriber.transcribe(_no_speech(kind), options=options)

    assert result["segments"] == []
    assert isinstance(result["language"], str) and result["language"]


def test_control_real_speech_does_produce_segments(tiny_transcriber):
    """The same transcriber on 10 s of real speech: an empty result above means no speech."""
    with wave.open(str(SAMPLE_WAV), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)

    result = tiny_transcriber.transcribe(pcm.astype(np.float32) / 32768.0)

    assert len(result["segments"]) >= 1
    assert any(seg["words"] for seg in result["segments"])
