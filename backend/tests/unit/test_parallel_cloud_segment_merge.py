"""Parallel cloud ASR + a cloud detector honours the plan's merge granularity (#1236 item 2).

For a segment-timed transcription model (``whisper-1``, Azure) the plan sets
``merge == "segment"`` (C7): each segment takes the speaker whose turns overlap it MOST, and
its words follow. The parallel path merged by the segment MIDPOINT instead (no words to vote
with), so a short turn that happened to straddle the middle of a long segment won it, and a
midpoint falling in a silence between two turns left the segment with no speaker at all.

These run ``_run_cloud_asr_pipeline`` (the stage's own entry) with a fake ASR vendor and a
fake detector; only the database, notification and settings seams are replaced. The
word-timed control keeps per-word attribution, so a merge that ignored granularity in the
other direction fails too.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.asr.types import ASRResult
from app.services.asr.types import ASRSegment
from app.services.asr.types import ASRWord
from app.services.diarization.types import DiarizeResult
from app.services.diarization.types import DiarizeSegment
from tests.unit._plan_contexts import make_plan

_CLOUD = "app.tasks.transcription.cloud_asr"


class _Vendor:
    """A transcription vendor returning fixed segments (text only)."""

    provider_name = "openai"

    def __init__(self, segments: list[ASRSegment]) -> None:
        self.segments = segments

    def supports_translation(self) -> bool:
        return False

    def supports_diarization(self) -> bool:
        return False

    def transcribe(self, audio_path, config, progress_callback=None) -> ASRResult:
        return ASRResult(
            segments=self.segments,
            language="en",
            has_speakers=False,
            provider_name="openai",
            model_name="whisper-1",
        )


class _Detector:
    provider_name = "pyannote.ai"

    def __init__(self, turns: list[tuple[float, float, str]]) -> None:
        self.turns = turns

    def diarize(self, audio_path, config, progress_callback=None) -> DiarizeResult:
        return DiarizeResult(
            segments=[DiarizeSegment(start=s, end=e, speaker=spk) for s, e, spk in self.turns],
            num_speakers=len({spk for _, _, spk in self.turns}),
            provider_name="pyannote.ai",
            model_name="precision-3",
        )


_RANGE = SimpleNamespace(min_speakers=1, max_speakers=20, num_speakers=None)


@contextmanager
def _no_session():
    yield None


def _run(plan, vendor: _Vendor, detector: _Detector) -> dict:
    from app.tasks.transcription.cloud_asr import _run_cloud_asr_pipeline
    from app.tasks.transcription.context import TranscriptionContext

    ctx = TranscriptionContext(
        task_id="task-merge",
        file_id=1,
        file_uuid="11111111-1111-1111-1111-111111111111",
        user_id=1,
        file_path="x.wav",
        file_name="x.wav",
        content_type="audio/wav",
    )
    settings = {"source_language": None, "translate_to_english": False}
    with (
        patch(f"{_CLOUD}.session_scope", _no_session),
        patch(f"{_CLOUD}.update_task_status"),
        patch(f"{_CLOUD}.send_progress_notification"),
        patch(f"{_CLOUD}._get_user_language_settings", return_value=settings),
        patch(f"{_CLOUD}.load_vocabulary_terms", return_value=[]),
        patch(f"{_CLOUD}._get_user_transcription_settings", return_value={}),
        patch(f"{_CLOUD}.resolve_speaker_range", return_value=_RANGE),
        patch(
            "app.services.diarization.factory.DiarizationProviderFactory.create_for_engine",
            lambda *a, **k: detector,
        ),
    ):
        return _run_cloud_asr_pipeline(ctx, "x.wav", None, None, None, vendor, plan=plan)


SEGMENT_PLAN = make_plan("cloud_text_segment", "pyannote")


def test_the_segment_timed_plan_is_a_parallel_segment_merge():
    """Guard the fixture: the cell under test really is parallel_cloud + segment merge."""
    assert SEGMENT_PLAN.diarization.execution == "parallel_cloud"
    assert SEGMENT_PLAN.diarization.merge == "segment"


@pytest.mark.parametrize(
    ("turns", "expected"),
    [
        pytest.param(
            [(0.0, 4.5, "SPEAKER_00"), (4.5, 5.5, "SPEAKER_01"), (5.5, 10.0, "SPEAKER_00")],
            "SPEAKER_00",
            id="short-turn-on-the-midpoint",
        ),
        pytest.param(
            [(0.0, 3.0, "SPEAKER_00"), (6.0, 10.0, "SPEAKER_01")],
            "SPEAKER_01",
            id="midpoint-in-a-silence",
        ),
    ],
)
def test_a_segment_takes_the_speaker_who_talks_most_in_it(turns, expected):
    vendor = _Vendor([ASRSegment(text="one long segment", start=0.0, end=10.0, words=[])])

    segments = _run(SEGMENT_PLAN, vendor, _Detector(turns))["segments"]

    assert [s["speaker"] for s in segments] == [expected]


def test_words_a_segment_timed_vendor_returns_do_not_outvote_the_segment():
    """Segment timing: words are not evidence of who spoke, the segment interval is."""
    words = [ASRWord(word="hi", start=0.0, end=1.0), ASRWord(word="there", start=1.0, end=2.0)]
    vendor = _Vendor([ASRSegment(text="hi there", start=0.0, end=10.0, words=words)])
    turns = [(0.0, 2.0, "SPEAKER_01"), (2.0, 10.0, "SPEAKER_00")]

    segments = _run(SEGMENT_PLAN, vendor, _Detector(turns))["segments"]

    assert [s["speaker"] for s in segments] == ["SPEAKER_00"]


def test_a_word_timed_plan_keeps_per_word_attribution():
    """Control: with word timing the words decide (here: both in SPEAKER_01's turn)."""
    plan = make_plan("cloud_text_word", "pyannote")
    assert plan.diarization.merge == "word"
    words = [ASRWord(word="hi", start=0.0, end=1.0), ASRWord(word="there", start=1.0, end=2.0)]
    vendor = _Vendor([ASRSegment(text="hi there", start=0.0, end=10.0, words=words)])
    turns = [(0.0, 2.0, "SPEAKER_01"), (2.0, 10.0, "SPEAKER_00")]

    segments = _run(plan, vendor, _Detector(turns))["segments"]

    assert [s["speaker"] for s in segments] == ["SPEAKER_01"]


def test_the_segment_merge_still_records_the_detector_that_ran():
    vendor = _Vendor([ASRSegment(text="x", start=0.0, end=4.0, words=[])])

    result = _run(SEGMENT_PLAN, vendor, _Detector([(0.0, 4.0, "SPEAKER_00")]))

    assert [s["speaker"] for s in result["segments"]] == ["SPEAKER_00"]
    assert (result["diarization_provider"], result["diarization_model"]) == (
        "pyannote.ai",
        "precision-3",
    )
