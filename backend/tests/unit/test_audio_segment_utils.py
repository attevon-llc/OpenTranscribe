"""Tests for ``app.services.audio_segment_utils`` — pure segment-math helpers plus the
two real ffmpeg subprocess wrappers.

The merge/select/group functions are pure (no I/O), so they get exact-value assertions on
hand-computed boundary cases rather than mocks. ``extract_audio_segment_np`` and
``load_full_audio_np`` shell out to ffmpeg; ffmpeg is present on this host (and inside the
CI image), so those are exercised against tiny real WAV fixtures generated with the stdlib
``wave`` module — that proves the actual subprocess plumbing (seek flags, sample-rate
conversion, byte layout) rather than a mocked stand-in for it.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.services.audio_segment_utils import extract_audio_segment_np
from app.services.audio_segment_utils import group_segments_by_speaker
from app.services.audio_segment_utils import load_full_audio_np
from app.services.audio_segment_utils import merge_adjacent_segments
from app.services.audio_segment_utils import select_top_segments


def _write_wav(
    path: Path,
    duration_s: float = 2.0,
    sample_rate: int = 16000,
    freq: float = 440.0,
    channels: int = 1,
) -> None:
    """Write a real (non-silent) PCM16 WAV file — no ffmpeg needed to create it."""
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


# ---------------------------------------------------------------------------
# merge_adjacent_segments — pure logic, exact-value boundary cases
# ---------------------------------------------------------------------------


class TestMergeAdjacentSegments:
    def test_empty_list_returns_empty(self):
        assert merge_adjacent_segments([]) == []

    def test_single_segment_returns_equivalent_copy(self):
        segs = [{"start": 1.0, "end": 2.0}]
        result = merge_adjacent_segments(segs)
        assert result == [{"start": 1.0, "end": 2.0}]
        assert result[0] is not segs[0]  # copied, not aliased

    def test_gap_exactly_at_threshold_merges(self):
        """max_gap uses <=, so a gap exactly equal to it must still merge."""
        segs = [{"start": 0.0, "end": 1.0}, {"start": 1.5, "end": 2.0}]
        result = merge_adjacent_segments(segs, max_gap=0.5)
        assert result == [{"start": 0.0, "end": 2.0}]

    def test_gap_just_beyond_threshold_does_not_merge(self):
        segs = [{"start": 0.0, "end": 1.0}, {"start": 1.51, "end": 2.0}]
        result = merge_adjacent_segments(segs, max_gap=0.5)
        assert result == [
            {"start": 0.0, "end": 1.0},
            {"start": 1.51, "end": 2.0},
        ]

    def test_overlapping_segments_merge_to_max_end(self):
        """A negative gap (overlap) must still merge, and the end must be the larger one."""
        segs = [{"start": 0.0, "end": 5.0}, {"start": 2.0, "end": 4.0}]
        result = merge_adjacent_segments(segs, max_gap=0.5)
        assert result == [{"start": 0.0, "end": 5.0}]

    def test_unsorted_input_is_sorted_before_merging(self):
        segs = [{"start": 5.0, "end": 6.0}, {"start": 0.0, "end": 1.0}]
        result = merge_adjacent_segments(segs, max_gap=0.5)
        assert result == [
            {"start": 0.0, "end": 1.0},
            {"start": 5.0, "end": 6.0},
        ]

    def test_chain_merge_across_three_segments(self):
        segs = [
            {"start": 0.0, "end": 1.0},
            {"start": 1.2, "end": 2.0},
            {"start": 2.3, "end": 3.0},
        ]
        result = merge_adjacent_segments(segs, max_gap=0.5)
        assert result == [{"start": 0.0, "end": 3.0}]

    def test_does_not_mutate_original_input(self):
        segs = [{"start": 0.0, "end": 1.0}, {"start": 1.1, "end": 2.0}]
        merge_adjacent_segments(segs, max_gap=0.5)
        assert segs == [{"start": 0.0, "end": 1.0}, {"start": 1.1, "end": 2.0}]


# ---------------------------------------------------------------------------
# select_top_segments — pure logic, exact-value boundary cases
# ---------------------------------------------------------------------------


class TestSelectTopSegments:
    def test_empty_list_returns_empty(self):
        assert select_top_segments([]) == []

    def test_selects_longest_first(self):
        segs = [
            {"start": 0.0, "end": 2.0},  # 2s
            {"start": 10.0, "end": 15.0},  # 5s
            {"start": 20.0, "end": 23.0},  # 3s
        ]
        result = select_top_segments(segs, min_duration=1.0, max_segments=3)
        assert [s["end"] - s["start"] for s in result] == [5.0, 3.0, 2.0]

    def test_filters_segments_below_min_duration(self):
        segs = [
            {"start": 0.0, "end": 0.5},  # 0.5s — below threshold
            {"start": 10.0, "end": 12.0},  # 2s
        ]
        result = select_top_segments(segs, min_duration=1.0, max_segments=5)
        assert result == [{"start": 10.0, "end": 12.0}]

    def test_respects_max_segments_limit(self):
        segs = [{"start": float(i), "end": float(i) + 2.0} for i in range(10)]
        result = select_top_segments(segs, min_duration=0.0, max_segments=3)
        assert len(result) == 3

    def test_all_below_min_duration_returns_empty(self):
        segs = [{"start": 0.0, "end": 0.1}, {"start": 1.0, "end": 1.2}]
        result = select_top_segments(segs, min_duration=5.0, max_segments=5)
        assert result == []

    def test_fewer_segments_than_max_returns_all_qualifying(self):
        segs = [{"start": 0.0, "end": 3.0}, {"start": 10.0, "end": 11.0}]
        result = select_top_segments(segs, min_duration=0.5, max_segments=10)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# group_segments_by_speaker — pure logic
# ---------------------------------------------------------------------------


class TestGroupSegmentsBySpeaker:
    def test_empty_segments_returns_empty_dict(self):
        assert group_segments_by_speaker([], {"SPEAKER_00": 1}) == {}

    def test_groups_multiple_segments_under_same_speaker_id(self):
        segments = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0},
            {"speaker": "SPEAKER_01", "start": 1.0, "end": 2.0},
            {"speaker": "SPEAKER_00", "start": 2.0, "end": 3.0},
        ]
        mapping = {"SPEAKER_00": 10, "SPEAKER_01": 20}
        result = group_segments_by_speaker(segments, mapping)
        assert set(result.keys()) == {10, 20}
        assert len(result[10]) == 2
        assert len(result[20]) == 1
        # order preserved within a speaker's group
        assert [s["start"] for s in result[10]] == [0.0, 2.0]

    def test_segment_with_missing_speaker_key_is_skipped(self):
        segments = [{"start": 0.0, "end": 1.0}]
        result = group_segments_by_speaker(segments, {"SPEAKER_00": 1})
        assert result == {}

    def test_segment_with_speaker_absent_from_mapping_is_skipped(self):
        segments = [{"speaker": "SPEAKER_99", "start": 0.0, "end": 1.0}]
        result = group_segments_by_speaker(segments, {"SPEAKER_00": 1})
        assert result == {}

    def test_only_qualifying_segments_end_up_in_the_result(self):
        """Non-emptiness guard for the loop above: a real speaker segment must appear."""
        segments: list[dict[str, Any]] = [
            {"start": 0.0, "end": 1.0},  # skipped: no speaker key
            {"speaker": "SPEAKER_00", "start": 1.0, "end": 2.0},
        ]
        result = group_segments_by_speaker(segments, {"SPEAKER_00": 5})
        assert result != {}
        assert result[5][0]["start"] == 1.0


# ---------------------------------------------------------------------------
# extract_audio_segment_np — real ffmpeg subprocess
# ---------------------------------------------------------------------------


class TestExtractAudioSegmentNp:
    def test_non_positive_duration_returns_none_without_invoking_ffmpeg(self, monkeypatch):
        mock_run = MagicMock()
        monkeypatch.setattr(subprocess, "run", mock_run)

        assert extract_audio_segment_np("irrelevant.wav", start=0.0, duration=0.0) is None
        assert extract_audio_segment_np("irrelevant.wav", start=0.0, duration=-1.0) is None
        mock_run.assert_not_called()

    def test_extracts_expected_sample_count_from_real_wav(self, tmp_path):
        wav_path = tmp_path / "tone.wav"
        _write_wav(wav_path, duration_s=3.0, sample_rate=16000)

        result = extract_audio_segment_np(str(wav_path), start=1.0, duration=1.0, target_sr=8000)

        assert result is not None
        assert result.dtype == np.float32
        # ffmpeg's exact frame count can drift by a handful of samples; the math (1s @ 8kHz)
        # must land within a small tolerance, not just "some array came back".
        assert abs(len(result) - 8000) <= 80
        assert np.all(np.abs(result) <= 1.0)

    def test_missing_source_file_returns_none(self, tmp_path):
        missing = tmp_path / "does_not_exist.wav"
        result = extract_audio_segment_np(str(missing), start=0.0, duration=1.0)
        assert result is None


# ---------------------------------------------------------------------------
# load_full_audio_np — real ffmpeg subprocess
# ---------------------------------------------------------------------------


class TestLoadFullAudioNp:
    def test_loads_expected_length_and_dtype(self, tmp_path):
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, duration_s=2.0, sample_rate=16000)

        result = load_full_audio_np(str(wav_path), target_sr=16000)

        assert result.dtype == np.float32
        assert abs(len(result) - 32000) <= 160  # 2s @ 16kHz, small ffmpeg-boundary tolerance

    def test_resamples_when_target_sr_differs(self, tmp_path):
        wav_path = tmp_path / "full16.wav"
        _write_wav(wav_path, duration_s=2.0, sample_rate=16000)

        result = load_full_audio_np(str(wav_path), target_sr=8000)

        assert abs(len(result) - 16000) <= 80  # 2s @ 8kHz

    def test_missing_file_raises_called_process_error(self, tmp_path):
        missing = tmp_path / "nope.wav"
        with pytest.raises(subprocess.CalledProcessError):
            load_full_audio_np(str(missing))
