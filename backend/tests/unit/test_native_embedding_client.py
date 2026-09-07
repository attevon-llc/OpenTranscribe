"""diar-native speaker-embedding client — window fitting and failure semantics (issue #571).

``fit_to_window`` is the part that has to be right and the part whose bug is INVISIBLE:
the sidecar embeds a fixed 160,000-sample window and applies an all-ones frame mask over
it, so a short clip is zero-padded and the silence is pooled with full weight. The result
is still a plausible unit-norm 256-vector — nothing raises, nothing logs, and the measured
cosine against the in-process model drops from 1.000 (at exactly 10 s) to +0.012 (at 0.8 s).
Every assertion below exists because a plausible-looking wrong answer is the failure mode.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.native_embedding_client import NATIVE_EMBEDDING_MAX_TILES
from app.services.native_embedding_client import NATIVE_EMBEDDING_WINDOW_SAMPLES
from app.services.native_embedding_client import embed_waveform
from app.services.native_embedding_client import fit_to_window

W = NATIVE_EMBEDDING_WINDOW_SAMPLES


def _ramp(n: int) -> np.ndarray:
    """A signal whose every sample is distinguishable, so slicing errors are visible."""
    return np.linspace(-1.0, 1.0, n, dtype=np.float32)


class TestFitToWindow:
    def test_empty_input_yields_no_windows(self) -> None:
        assert fit_to_window(np.zeros(0, dtype=np.float32)) == []

    @pytest.mark.parametrize("length", [1, 400, 12_800, W - 1])
    def test_short_clip_is_filled_to_exactly_the_model_window(self, length: int) -> None:
        """The whole point: NOTHING shorter than the model window may be sent.

        A clip posted raw is zero-padded server-side and pooled against silence.
        """
        windows = fit_to_window(_ramp(length))
        assert len(windows) == 1
        assert windows[0].shape == (W,), (
            f"a {length}-sample clip produced a {windows[0].shape} window; anything other "
            f"than exactly {W} samples is silently zero-padded by the sidecar"
        )

    def test_short_clip_is_filled_by_repeating_not_by_padding(self) -> None:
        """Repeat-fill keeps every pooled frame real speech; zero-fill pools silence."""
        clip = _ramp(W // 4)
        (window,) = fit_to_window(clip)
        assert np.count_nonzero(window == 0.0) <= np.count_nonzero(clip == 0.0), (
            "the filled window introduced zero samples — that is padding, not repetition"
        )
        # Each quarter is the original clip again.
        for i in range(4):
            np.testing.assert_allclose(window[i * clip.size : (i + 1) * clip.size], clip)

    def test_exactly_one_window_is_passed_through_unchanged(self) -> None:
        clip = _ramp(W)
        (window,) = fit_to_window(clip)
        np.testing.assert_array_equal(window, clip)

    def test_long_clip_is_split_into_full_windows(self) -> None:
        windows = fit_to_window(_ramp(3 * W))
        assert len(windows) == 3
        assert all(w.shape == (W,) for w in windows)

    def test_the_final_window_is_the_clips_tail_not_a_short_remainder(self) -> None:
        """A 2.5-window clip must not send a half-length final window.

        Taking the last full window instead means the tail is covered by real audio;
        a short remainder would be zero-padded and poison the mean.
        """
        clip = _ramp(int(2.5 * W))
        windows = fit_to_window(clip)
        assert all(w.shape == (W,) for w in windows)
        np.testing.assert_array_equal(windows[-1], clip[-W:])

    def test_tile_count_is_capped_and_spans_the_whole_clip(self) -> None:
        """Above the cap the windows are a SAMPLE of the clip, never a truncated head."""
        clip = _ramp((NATIVE_EMBEDDING_MAX_TILES + 40) * W)
        windows = fit_to_window(clip)
        assert len(windows) == NATIVE_EMBEDDING_MAX_TILES
        np.testing.assert_array_equal(windows[0], clip[:W])
        np.testing.assert_array_equal(windows[-1], clip[-W:])

    def test_multichannel_input_is_flattened_to_the_window_width(self) -> None:
        windows = fit_to_window(_ramp(W).reshape(1, W))
        assert len(windows) == 1
        assert windows[0].shape == (W,)


class TestEmbedWaveform:
    """Sidecar interaction, with the HTTP call stubbed at the shared `post_json` seam."""

    @staticmethod
    def _stub(monkeypatch, replies, recorder=None):
        import app.services.native_embedding_client as mod

        calls = iter(replies)

        def fake_post_json(url, payload, timeout):
            if recorder is not None:
                recorder.append((url, payload))
            reply = next(calls)
            if isinstance(reply, Exception):
                raise reply
            return {"embedding": list(reply)}

        monkeypatch.setattr(mod, "post_json", fake_post_json)

    def test_returns_an_l2_normalized_vector_of_the_native_dimension(self, monkeypatch) -> None:
        vec = np.arange(1, 257, dtype=np.float32) * 3.0  # deliberately un-normalized
        self._stub(monkeypatch, [vec])
        out = embed_waveform(_ramp(W))
        assert out is not None
        assert out.shape == (256,)
        assert float(np.linalg.norm(out)) == pytest.approx(1.0, abs=1e-5)
        assert float(np.dot(out, vec / np.linalg.norm(vec))) == pytest.approx(1.0, abs=1e-5)

    def test_every_request_carries_exactly_the_model_window(self, monkeypatch) -> None:
        """The regression guard for the defect this module exists to prevent."""
        import base64

        sent: list = []
        self._stub(monkeypatch, [np.ones(256, dtype=np.float32)] * 3, recorder=sent)
        embed_waveform(_ramp(int(0.8 * 16_000)))
        embed_waveform(_ramp(int(2.5 * W)))
        assert len(sent) == 3
        for url, payload in sent:
            assert url.endswith("/embed_window")
            raw = base64.b64decode(payload["samples_b64"])
            assert len(raw) == W * 4, (
                f"posted {len(raw) // 4} samples instead of {W}; the sidecar would "
                "zero-pad the difference and pool silence at full mask weight"
            )

    def test_tiles_are_mean_pooled_over_the_whole_clip(self, monkeypatch) -> None:
        a = np.zeros(256, dtype=np.float32)
        a[0] = 1.0
        b = np.zeros(256, dtype=np.float32)
        b[1] = 1.0
        self._stub(monkeypatch, [a, b])
        out = embed_waveform(_ramp(2 * W))
        assert out is not None
        # Mean of two orthogonal unit vectors, renormalized.
        assert float(out[0]) == pytest.approx(2**-0.5, abs=1e-5)
        assert float(out[1]) == pytest.approx(2**-0.5, abs=1e-5)

    def test_sidecar_failure_returns_none_rather_than_raising(self, monkeypatch) -> None:
        """None means 'use the in-process model', so it must never escape as an exception."""
        self._stub(monkeypatch, [OSError("sidecar gone")])
        assert embed_waveform(_ramp(W)) is None

    def test_a_failure_on_a_later_tile_discards_the_whole_embedding(self, monkeypatch) -> None:
        """A partial mean over the head of a clip is a wrong answer, not a degraded one."""
        good = np.ones(256, dtype=np.float32)
        self._stub(monkeypatch, [good, OSError("sidecar gone mid-clip")])
        assert embed_waveform(_ramp(3 * W)) is None

    def test_empty_audio_returns_none_without_calling_the_sidecar(self, monkeypatch) -> None:
        sent: list = []
        self._stub(monkeypatch, [], recorder=sent)
        assert embed_waveform(np.zeros(0, dtype=np.float32)) is None
        assert sent == []

    def test_a_zero_vector_reply_returns_none_rather_than_a_zero_embedding(
        self, monkeypatch
    ) -> None:
        """An all-zero vector has no direction; every cosine against it is 0."""
        self._stub(monkeypatch, [np.zeros(256, dtype=np.float32)])
        assert embed_waveform(_ramp(W)) is None
