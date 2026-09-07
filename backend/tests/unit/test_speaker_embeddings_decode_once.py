"""extract_embeddings_for_segments must decode the audio file ONCE, not per-segment
(issue #661 E3.2).

Before this fix, `extract_embeddings_for_segments` called `extract_embedding_from_file`
for every selected segment, and that method does a full-file decode (`_load_audio`) on
every call — up to 30 full decodes per job (5 segments x 6 speakers). The fix decodes the
file once via `_load_audio` and slices each segment out of that single in-memory waveform
via `extract_embedding_from_waveform`.

⚠️ This is the speaker-embedding path: speaker indexing MUST NOT change. Both the old and
new code paths apply the identical start/end-sample slicing and call the identical
`_embed`, so per the docstring, the only thing that should differ is the number of
`_load_audio` calls, and the resulting embeddings must be numerically IDENTICAL. That
equality is exactly what this test asserts, using a synthetic (non-model) `_embed` stub
so it runs with no GPU/model weights and no ffmpeg — real end-to-end numerical parity
against genuine pyannote weights is out of scope for a fast unit test and is covered by
the `models`-marked integration suite (tests/integration/test_speaker_embedding_cpu.py).
"""

from __future__ import annotations

import numpy as np
import pytest
import torch

from app.services.speaker_embedding_service import SpeakerEmbeddingService


def _stub_service(monkeypatch) -> tuple[SpeakerEmbeddingService, list[str]]:
    """Build a service with no real model, torch device, or sidecar involved."""
    monkeypatch.setattr(SpeakerEmbeddingService, "_initialize_model", lambda self: None)
    monkeypatch.setattr(
        "app.utils.hardware_detection.detect_hardware",
        lambda: type(
            "HW", (), {"get_pyannote_config": lambda self: {"device": "cpu"}, "__init__": None}
        )(),
    )
    monkeypatch.setenv("USE_NATIVE_SPEAKER_EMBEDDINGS", "false")
    service = SpeakerEmbeddingService(mode="v3")  # v3 always routes to the in-process path
    service.device = torch.device("cpu")

    load_calls: list[str] = []

    # A deterministic synthetic "audio file": each sample equals its own index, so a
    # slice's mean uniquely encodes (start_sample, end_sample) and any mis-slicing
    # (wrong offset, wrong length, decoding a different "file") shows up as a numeric
    # mismatch rather than silently passing.
    sample_rate = 16000
    duration_s = 20.0
    n = int(sample_rate * duration_s)
    fake_waveform = torch.arange(n, dtype=torch.float32).unsqueeze(0)

    def fake_load_audio(audio_path: str, target_sr: int = 16000):
        load_calls.append(audio_path)
        return fake_waveform.clone(), sample_rate

    monkeypatch.setattr(SpeakerEmbeddingService, "_load_audio", staticmethod(fake_load_audio))

    # Deterministic, non-model "embedding": a small vector derived from the waveform's
    # own content (mean + std), L2-normalized like a real embedding.
    def fake_embed(self, waveform: torch.Tensor, sample_rate: int):
        arr = waveform.numpy().astype(np.float64).ravel()
        if arr.size == 0:
            return None
        vec = np.array([arr.mean(), arr.std(), float(arr.size)], dtype=np.float32)
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec

    monkeypatch.setattr(SpeakerEmbeddingService, "_embed", fake_embed)

    return service, load_calls


@pytest.fixture
def stubbed(monkeypatch):
    return _stub_service(monkeypatch)


class TestDecodesOnce:
    def test_decodes_the_file_exactly_once_regardless_of_segment_count(self, stubbed) -> None:
        service, load_calls = stubbed
        segments = [
            {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
            {"start": 3.0, "end": 6.0, "speaker": "SPEAKER_00"},
            {"start": 7.0, "end": 9.0, "speaker": "SPEAKER_01"},
            {"start": 10.0, "end": 15.0, "speaker": "SPEAKER_01"},
        ]
        speaker_mapping = {"SPEAKER_00": 1, "SPEAKER_01": 2}

        result = service.extract_embeddings_for_segments(
            "fake_audio.wav", segments, speaker_mapping
        )

        assert load_calls == ["fake_audio.wav"], (
            f"expected exactly one decode of the audio file, got {len(load_calls)}: {load_calls}"
        )
        assert set(result.keys()) == {1, 2}
        assert all(len(v) >= 1 for v in result.values())

    def test_no_segments_selected_still_decodes_at_most_once(self, stubbed) -> None:
        """Below the min-duration floor: no embeddings, but we must not decode per attempt."""
        service, load_calls = stubbed
        segments = [{"start": 0.0, "end": 0.1, "speaker": "SPEAKER_00"}]
        result = service.extract_embeddings_for_segments(
            "fake_audio.wav", segments, {"SPEAKER_00": 1}
        )
        assert result == {}
        assert len(load_calls) <= 1

    def test_empty_speaker_mapping_decodes_zero_times(self, stubbed) -> None:
        """No matching segments at all: short-circuit before ever touching the file."""
        service, load_calls = stubbed
        result = service.extract_embeddings_for_segments(
            "fake_audio.wav", [{"start": 0.0, "end": 5.0, "speaker": "UNKNOWN"}], {}
        )
        assert result == {}
        assert load_calls == []


class TestNumericalEquivalenceWithTheOldPerSegmentPath:
    """The core safety gate: same vectors in, same vectors out (issue #661 E3.2).

    `extract_embedding_from_file` is UNCHANGED — it still does its own
    `_load_audio` + slice + `_embed` per call, which is exactly what
    `extract_embeddings_for_segments` used to call per segment. So calling it
    directly, once per segment, is a faithful stand-in for "the old code path",
    without needing a `git archive` checkout for a pure-logic comparison.
    """

    def test_new_path_matches_the_old_per_segment_call_exactly(self, stubbed) -> None:
        service, load_calls = stubbed
        segments = [
            {"start": 1.0, "end": 4.5, "speaker": "SPEAKER_00"},
            {"start": 5.0, "end": 12.0, "speaker": "SPEAKER_00"},
            {"start": 12.5, "end": 16.0, "speaker": "SPEAKER_01"},
        ]
        speaker_mapping = {"SPEAKER_00": 1, "SPEAKER_01": 2}

        # New path (decodes once, slices from the shared in-memory waveform).
        new_result = service.extract_embeddings_for_segments(
            "fake_audio.wav", segments, speaker_mapping
        )
        load_calls.clear()

        # Old path equivalent: call extract_embedding_from_file per selected segment.
        # Reproduce the same merge/select logic the method itself applies.
        from app.core.constants import SPEAKER_SHORT_SEGMENT_MIN_DURATION
        from app.services.audio_segment_utils import group_segments_by_speaker
        from app.services.audio_segment_utils import merge_adjacent_segments
        from app.services.audio_segment_utils import select_top_segments

        grouped = group_segments_by_speaker(segments, speaker_mapping)
        assert grouped, (
            "no speaker groups were built from the fixture segments — the equality "
            "loop below would iterate zero times and pass vacuously"
        )
        old_result: dict[int, list[np.ndarray]] = {}
        for speaker_id, speaker_segs in grouped.items():
            merged = merge_adjacent_segments(speaker_segs)
            selected = select_top_segments(
                merged, min_duration=SPEAKER_SHORT_SEGMENT_MIN_DURATION, max_segments=5
            )
            embeddings = []
            for seg in selected:
                emb = service.extract_embedding_from_file(
                    "fake_audio.wav", {"start": seg["start"], "end": seg["end"]}
                )
                if emb is not None:
                    embeddings.append(emb)
            if embeddings:
                old_result[speaker_id] = embeddings

        assert new_result, (
            "extract_embeddings_for_segments returned no speakers at all — the "
            "equality comparison below would iterate zero times and pass vacuously"
        )
        assert set(new_result.keys()) == set(old_result.keys())
        for speaker_id in new_result:
            assert len(new_result[speaker_id]) == len(old_result[speaker_id])
            for new_vec, old_vec in zip(
                new_result[speaker_id], old_result[speaker_id], strict=False
            ):
                np.testing.assert_array_equal(
                    new_vec,
                    old_vec,
                    err_msg=(
                        f"embedding mismatch for speaker {speaker_id} — decode-once "
                        "changed the resulting vector, not just the decode count"
                    ),
                )
        # And the old path paid one full decode per selected segment, unlike the new one.
        assert len(load_calls) == sum(len(v) for v in old_result.values())
