"""Numerical-equality gate for the lazy/chunked scaled read (issue #661 E4).

`load_from_shared_volume` used to do `data.astype(np.float32) / 32767.0` in one shot —
mmap-opened then immediately fully materialized into a float32 copy, doubling peak
memory over the final ~691 MB (3h @ 16kHz mono) array. The fix reads/scales in fixed-size
chunks straight into a preallocated output array. Floating point division is elementwise
independent (no cross-chunk reduction), so chunking must not change a single output value
— this test is the falsifiable claim, not "a transcript came back".

⚠️ The dangerous failure mode this guards against is silent, not a crash: faster_whisper's
feature extractor only checks `dtype != float32` and casts WITHOUT dividing
(`feature_extractor.py:207-208`), so an accidentally-unscaled int16-derived array produces
amplitudes ~32767x too large with no exception at all.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.transcription.engine import audio_loader

SAMPLE_WAV = Path(__file__).resolve().parent.parent / "fixtures" / "media" / "sample_short.wav"


def _reference_scaled_read(wav_path: str) -> np.ndarray:
    """The OLD, one-shot implementation — the ground truth this must match exactly."""
    import scipy.io.wavfile as wavfile

    _, data = wavfile.read(wav_path, mmap=True)
    return data.astype(np.float32) / 32767.0  # type: ignore[no-any-return]


class TestScaledReadNumericalEquality:
    def test_chunked_read_is_bit_identical_to_the_one_shot_reference(self) -> None:
        assert SAMPLE_WAV.exists(), f"fixture missing: {SAMPLE_WAV}"

        expected = _reference_scaled_read(str(SAMPLE_WAV))
        actual = audio_loader.load_from_shared_volume(str(SAMPLE_WAV))

        assert actual is not None
        assert actual.dtype == np.float32
        assert actual.shape == expected.shape
        np.testing.assert_array_equal(
            actual,
            expected,
            err_msg="chunked scaled read diverges from the one-shot reference — this would "
            "silently corrupt every downstream consumer (Whisper features, diarizer input)",
        )

    def test_chunk_boundaries_do_not_perturb_values(self, tmp_path) -> None:
        """Force multiple chunk boundaries with a tiny synthetic WAV, independent of
        whatever length the real fixture happens to be (which may fit in one chunk).
        """
        import scipy.io.wavfile as wavfile

        rng = np.random.default_rng(20260904)
        # Full int16 range, including the extremes, well past several artificially
        # small chunk boundaries.
        n_samples = 10_003
        data = rng.integers(-32768, 32767, size=n_samples, dtype=np.int16)
        wav_path = tmp_path / "synthetic.wav"
        wavfile.write(str(wav_path), 16000, data)

        expected = data.astype(np.float32) / 32767.0

        # Use a small chunk size to force many boundaries within this short file.
        original_chunk = audio_loader._SCALED_READ_CHUNK_SAMPLES
        audio_loader._SCALED_READ_CHUNK_SAMPLES = 777
        try:
            actual = audio_loader.load_from_shared_volume(str(wav_path))
        finally:
            audio_loader._SCALED_READ_CHUNK_SAMPLES = original_chunk

        assert actual is not None
        np.testing.assert_array_equal(actual, expected)

    def test_result_is_normalized_to_unit_range_not_raw_int16_magnitude(self) -> None:
        """Guards the exact silent-corruption failure mode the gist calls out: an
        unscaled int16-derived array passes a dtype check but has amplitudes ~32767x
        too large, with no exception anywhere in the chain.
        """
        assert SAMPLE_WAV.exists(), f"fixture missing: {SAMPLE_WAV}"
        actual = audio_loader.load_from_shared_volume(str(SAMPLE_WAV))
        assert actual is not None
        assert actual.dtype == np.float32
        # A real (non-silent) recording must have some signal, and every sample must
        # be within the normalized range — an unscaled reader would blow past this.
        assert np.abs(actual).max() <= 1.0 + 1e-6
        assert np.abs(actual).max() > 1e-6, "audio reads back as all-silence — fixture or path bug"

    @pytest.mark.parametrize("missing_path", ["", "/nonexistent/path/does-not-exist.wav"])
    def test_missing_or_empty_path_returns_none(self, missing_path: str) -> None:
        assert audio_loader.load_from_shared_volume(missing_path) is None
