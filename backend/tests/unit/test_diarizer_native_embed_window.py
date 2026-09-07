"""`NativeSpeakerDiarizer.embed_window` must send the sidecar's full model window (#571).

The acoustic boundary re-check embeds sub-second disputed words. Until issue #571 the
raw slice was posted straight to `/embed_window`, and diar-core zero-pads whatever it
receives up to 160,000 samples while applying an **all-ones** 589-frame mask — so a
0.8 s window came back as an embedding of 92% silence. It still had the right shape and
unit-ish norm, so nothing raised and nothing logged. Measured cosine against the
in-process model on the identical clip, by clip length:

    0.8s +0.012 | 1s +0.028 | 2s +0.007 | 3s +0.081 | 5s +0.239 | 8s +0.901 | 10s +1.000

The re-check compares that vector against speaker centroids by cosine, so the feature was
scoring noise. These tests pin the fix at the wire.
"""

from __future__ import annotations

import base64

import numpy as np
import pytest

from app.services.native_embedding_client import NATIVE_EMBEDDING_WINDOW_SAMPLES
from app.transcription.diarizer_native import NativeSpeakerDiarizer

W = NATIVE_EMBEDDING_WINDOW_SAMPLES


class _Config:
    num_speakers = None
    enable_overlap_detection = False
    enable_native_embeddings = True


@pytest.fixture
def diarizer_and_requests(monkeypatch):
    """A loaded native diarizer whose sidecar POSTs are captured, not sent."""
    sent: list[dict] = []

    def fake_post_json(url, payload, timeout):
        sent.append({"url": url, "payload": payload})
        vec = np.zeros(256, dtype=np.float32)
        vec[0] = 1.0
        return {"embedding": vec.tolist()}

    monkeypatch.setattr("app.services.native_embedding_client.post_json", fake_post_json)
    diarizer = NativeSpeakerDiarizer(_Config(), base_url="http://diar-native:8701")
    diarizer.is_loaded = True
    return diarizer, sent


def _posted_sample_count(request: dict) -> int:
    return len(base64.b64decode(request["payload"]["samples_b64"])) // 4


@pytest.mark.parametrize("word_duration", [0.05, 0.2, 0.8, 3.0])
def test_a_short_word_is_sent_as_a_full_model_window(
    diarizer_and_requests, word_duration: float
) -> None:
    """The regression this file exists for: never post fewer than 160,000 samples."""
    diarizer, sent = diarizer_and_requests
    audio = np.linspace(-1.0, 1.0, 30 * 16_000, dtype=np.float32)

    result = diarizer.embed_window(audio, 5.0, 5.0 + word_duration)

    assert result is not None
    assert len(sent) == 1
    assert _posted_sample_count(sent[0]) == W, (
        f"a {word_duration}s re-check window posted "
        f"{_posted_sample_count(sent[0])} samples instead of {W}; the sidecar zero-pads "
        "the difference and pools the silence at full mask weight"
    )
    assert sent[0]["url"].endswith("/embed_window")


def test_the_returned_vector_is_the_native_dimension_and_normalized(
    diarizer_and_requests,
) -> None:
    diarizer, _ = diarizer_and_requests
    audio = np.linspace(-1.0, 1.0, 30 * 16_000, dtype=np.float32)
    result = diarizer.embed_window(audio, 1.0, 1.4)
    assert result is not None
    assert result.shape == (256,)
    assert float(np.linalg.norm(result)) == pytest.approx(1.0, abs=1e-5)


def test_a_window_past_the_end_of_the_audio_is_still_a_full_window(
    diarizer_and_requests,
) -> None:
    """Clamping to the audio length must not produce a short, silence-padded request."""
    diarizer, sent = diarizer_and_requests
    audio = np.linspace(-1.0, 1.0, 2 * 16_000, dtype=np.float32)  # 2 s of audio total
    # Runs past the end: the slice clamps to 0.7 s of real audio, above the 0.4 s floor.
    result = diarizer.embed_window(audio, 1.3, 2.5)
    assert result is not None
    assert _posted_sample_count(sent[0]) == W


def test_a_sidecar_failure_returns_none_and_never_raises(monkeypatch) -> None:
    """A re-check embed must never break diarization."""

    def boom(url, payload, timeout):
        raise OSError("sidecar gone")

    monkeypatch.setattr("app.services.native_embedding_client.post_json", boom)
    diarizer = NativeSpeakerDiarizer(_Config(), base_url="http://diar-native:8701")
    diarizer.is_loaded = True
    audio = np.zeros(16_000, dtype=np.float32)
    assert diarizer.embed_window(audio, 0.1, 0.5) is None


def test_a_degenerate_window_is_refused_without_contacting_the_sidecar(
    diarizer_and_requests,
) -> None:
    """Less than half the 0.8 s floor of real audio is not embeddable."""
    diarizer, sent = diarizer_and_requests
    audio = np.zeros(1000, dtype=np.float32)
    assert diarizer.embed_window(audio, 0.0, 0.01) is None
    assert sent == []
