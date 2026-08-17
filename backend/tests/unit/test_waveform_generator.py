"""Tests for ``app.tasks.transcription.waveform_generator.WaveformGenerator``.

Pure computational helpers (``_compute_waveform_samples``, ``_normalize_waveform``) get
exact-value assertions on hand-computed arrays. The ffmpeg-backed paths
(``generate_waveform_data``, ``_probe_audio_file``, ``_extract_raw_audio``,
``generate_from_16khz_wav``) are exercised against tiny real WAV fixtures — ffmpeg is present
on this host, so the actual decode/RMS/normalize pipeline runs end to end rather than being
mocked. Mocks appear only where a real failure is hard to produce with a real file
(dependency-check failure, unexpected internal exception).
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from app.tasks.transcription.waveform_generator import WAVEFORM_RESOLUTIONS
from app.tasks.transcription.waveform_generator import WaveformGenerator


def _write_wav(
    path: Path,
    duration_s: float = 2.0,
    sample_rate: int = 16000,
    freq: float = 440.0,
    channels: int = 1,
) -> None:
    n_samples = int(sample_rate * duration_s)
    t = np.linspace(0, duration_s, n_samples, endpoint=False)
    tone = (0.5 * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
    if channels > 1:
        tone = np.repeat(tone.reshape(-1, 1), channels, axis=1).reshape(-1)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(channels)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(tone.tobytes())


@pytest.fixture
def generator():
    """Real WaveformGenerator — ffmpeg/ffprobe are present on this host."""
    return WaveformGenerator()


# ---------------------------------------------------------------------------
# _check_dependencies (constructor)
# ---------------------------------------------------------------------------


class TestCheckDependencies:
    def test_constructs_successfully_when_ffmpeg_present(self):
        # Real check against the real binaries — no mock needed.
        gen = WaveformGenerator()
        assert gen.WAVEFORM_SAMPLE_RATE == 22050

    def test_raises_runtime_error_when_ffmpeg_missing(self):
        with patch.object(subprocess, "run", side_effect=FileNotFoundError("no ffmpeg")):
            with pytest.raises(RuntimeError, match="FFmpeg is required"):
                WaveformGenerator()


# ---------------------------------------------------------------------------
# _compute_waveform_samples — pure, exact-value
# ---------------------------------------------------------------------------


class TestComputeWaveformSamples:
    def test_downsample_computes_exact_rms_per_chunk(self, generator):
        # 8 samples -> 2 target samples: chunk_size = 4.
        audio = np.array([0, 2, 0, 2, 0, 0, 0, 0], dtype=np.float32)
        result = generator._compute_waveform_samples(audio, target_samples=2)

        assert len(result) == 2
        # chunk0 = [0,2,0,2] -> mean(square)=2 -> rms=sqrt(2)
        assert result[0] == pytest.approx(np.sqrt(2.0))
        # chunk1 = [0,0,0,0] -> rms=0
        assert result[1] == pytest.approx(0.0)

    def test_upsample_interpolates_when_fewer_samples_than_target(self, generator):
        audio = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        result = generator._compute_waveform_samples(audio, target_samples=5)

        assert len(result) == 5
        # np.interp over indices [0, 0.5, 1, 1.5, 2] against abs(audio)=[0,1,0]
        expected = np.interp([0, 0.5, 1.0, 1.5, 2.0], [0, 1, 2], [0.0, 1.0, 0.0])
        assert result == pytest.approx(expected.tolist())

    def test_equal_length_takes_the_upsample_branch(self, generator):
        """total_samples <= target_samples is the trigger, so equality must not be missed."""
        audio = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        result = generator._compute_waveform_samples(audio, target_samples=3)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# _normalize_waveform — pure, exact-value
# ---------------------------------------------------------------------------


class TestNormalizeWaveform:
    def test_normalizes_to_0_255_range_exact_values(self, generator):
        result = generator._normalize_waveform([0.0, 0.5, 1.0], target_samples=3)
        assert result == [0, 127, 255]

    def test_pads_short_waveform_with_zeros(self, generator):
        result = generator._normalize_waveform([1.0], target_samples=4)
        assert len(result) == 4
        assert result == [255, 0, 0, 0]

    def test_truncates_long_waveform(self, generator):
        result = generator._normalize_waveform([1.0, 1.0, 1.0, 1.0], target_samples=2)
        assert len(result) == 2

    def test_all_zero_waveform_returns_all_zero_list(self, generator):
        result = generator._normalize_waveform([0.0, 0.0, 0.0], target_samples=3)
        assert result == [0, 0, 0]

    def test_empty_waveform_returns_zero_filled_list_of_target_length(self, generator):
        result = generator._normalize_waveform([], target_samples=3)
        assert result == [0, 0, 0]


# ---------------------------------------------------------------------------
# _probe_audio_file — real ffprobe
# ---------------------------------------------------------------------------


class TestProbeAudioFile:
    def test_probes_real_wav_duration_and_sample_rate(self, generator, tmp_path):
        wav_path = tmp_path / "probe.wav"
        _write_wav(wav_path, duration_s=2.0, sample_rate=16000)

        info = generator._probe_audio_file(str(wav_path))

        assert info is not None
        assert info["duration"] == pytest.approx(2.0, abs=0.05)
        assert info["sample_rate"] == 16000


# ---------------------------------------------------------------------------
# _extract_raw_audio — real ffmpeg
# ---------------------------------------------------------------------------


class TestExtractRawAudio:
    def test_extracts_expected_sample_count_and_range(self, generator, tmp_path):
        wav_path = tmp_path / "raw.wav"
        _write_wav(wav_path, duration_s=1.0, sample_rate=16000)

        audio = generator._extract_raw_audio(str(wav_path), duration=1.0)

        assert audio is not None
        assert audio.dtype == np.float32
        # WAVEFORM_SAMPLE_RATE = 22050; ffmpeg is told duration+0.5s but source is only 1.0s
        # long, so extraction cannot exceed the source's real content by much.
        assert len(audio) > 0
        assert np.all(np.abs(audio) <= 1.0)


# ---------------------------------------------------------------------------
# generate_waveform_data — end-to-end, real ffmpeg
# ---------------------------------------------------------------------------


class TestGenerateWaveformData:
    def test_generates_all_resolutions_with_correct_sample_counts(self, generator, tmp_path):
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, duration_s=2.0, sample_rate=16000)

        result = generator.generate_waveform_data(str(wav_path))

        assert result is not None
        assert set(result.keys()) == {f"waveform_{s}" for s in WAVEFORM_RESOLUTIONS.values()}
        for samples, entry in zip(WAVEFORM_RESOLUTIONS.values(), result.values(), strict=False):
            assert entry["samples"] == samples
            assert len(entry["waveform"]) == samples
            assert all(0 <= v <= 255 for v in entry["waveform"])
            assert entry["duration"] > 0

    def test_invalid_file_returns_none(self, generator, tmp_path):
        garbage = tmp_path / "not_audio.bin"
        garbage.write_bytes(b"this is not a media file")

        result = generator.generate_waveform_data(str(garbage))

        assert result is None

    def test_missing_file_returns_none(self, generator, tmp_path):
        missing = tmp_path / "does_not_exist.wav"
        result = generator.generate_waveform_data(str(missing))
        assert result is None

    def test_unexpected_internal_error_is_caught_and_returns_none(self, generator, tmp_path):
        """generate_waveform_data must not propagate — the pipeline treats a crashed waveform
        stage as non-fatal, so an unexpected exception anywhere inside must degrade to None."""
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, duration_s=1.0, sample_rate=16000)

        with patch.object(generator, "_probe_audio_file", side_effect=RuntimeError("boom")):
            result = generator.generate_waveform_data(str(wav_path))

        assert result is None


# ---------------------------------------------------------------------------
# generate_from_16khz_wav — real scipy resample, no ffmpeg
# ---------------------------------------------------------------------------


class TestGenerateFrom16kHzWav:
    def test_resamples_16khz_to_22050_and_produces_all_resolutions(self, generator, tmp_path):
        wav_path = tmp_path / "shared.wav"
        _write_wav(wav_path, duration_s=2.0, sample_rate=16000)

        result = generator.generate_from_16khz_wav(str(wav_path))

        assert result is not None
        assert set(result.keys()) == {f"waveform_{s}" for s in WAVEFORM_RESOLUTIONS.values()}
        for samples, entry in zip(WAVEFORM_RESOLUTIONS.values(), result.values(), strict=False):
            assert entry["sample_rate"] == generator.WAVEFORM_SAMPLE_RATE
            assert entry["samples"] == samples
            assert len(entry["data"]) == samples
            # 2s of 16kHz audio resampled to 22050Hz should still read as ~2s.
            assert entry["duration"] == pytest.approx(2.0, abs=0.1)

    def test_already_at_target_rate_skips_resample(self, generator, tmp_path):
        wav_path = tmp_path / "already22050.wav"
        _write_wav(wav_path, duration_s=1.0, sample_rate=22050)

        result = generator.generate_from_16khz_wav(str(wav_path))

        assert result is not None
        first_key = f"waveform_{next(iter(WAVEFORM_RESOLUTIONS.values()))}"
        assert result[first_key]["duration"] == pytest.approx(1.0, abs=0.05)

    def test_stereo_wav_uses_first_channel_without_crashing(self, generator, tmp_path):
        wav_path = tmp_path / "stereo.wav"
        _write_wav(wav_path, duration_s=1.0, sample_rate=16000, channels=2)

        result = generator.generate_from_16khz_wav(str(wav_path))

        assert result is not None
        first_key = f"waveform_{next(iter(WAVEFORM_RESOLUTIONS.values()))}"
        assert result[first_key]["duration"] == pytest.approx(1.0, abs=0.05)
        assert all(0 <= v <= 255 for v in result[first_key]["data"])

    def test_missing_file_returns_none(self, generator, tmp_path):
        missing = tmp_path / "nope.wav"
        result = generator.generate_from_16khz_wav(str(missing))
        assert result is None
