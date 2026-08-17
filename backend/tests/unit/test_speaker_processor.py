"""Tests for ``app.tasks.transcription.speaker_processor``.

The label/segment functions are pure and get exact-value assertions. ``create_or_get_speaker``
and ``create_speaker_mapping`` write real rows through ``db_session`` (the savepoint-isolated
session) and are asserted against those rows — not against mock-call arguments — per the
``test_dispatch.py`` pattern documented in ``backend/tests/CLAUDE.md``. The only patched seam is
``redis_cache.invalidate_speakers``, a genuinely out-of-process call.
"""

from __future__ import annotations

import uuid as uuid_pkg
from unittest.mock import patch

import pytest

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Speaker
from app.tasks.transcription.speaker_processor import create_or_get_speaker
from app.tasks.transcription.speaker_processor import create_speaker_mapping
from app.tasks.transcription.speaker_processor import extract_unique_speakers
from app.tasks.transcription.speaker_processor import mark_overlapping_segments
from app.tasks.transcription.speaker_processor import normalize_speaker_label
from app.tasks.transcription.speaker_processor import process_segments_with_speakers

_INVALIDATE_SPEAKERS = "app.services.redis_cache_service.redis_cache.invalidate_speakers"


@pytest.fixture
def media_file(db_session, normal_user):
    """A real MediaFile row, the FK every Speaker in this module needs."""
    mf = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        filename="speaker_processor_test.mp3",
        storage_path="speaker_processor/test.mp3",
        file_size=1024,
        content_type="audio/mpeg",
        status=FileStatus.PROCESSING,
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


# ---------------------------------------------------------------------------
# normalize_speaker_label — pure
# ---------------------------------------------------------------------------


class TestNormalizeSpeakerLabel:
    def test_none_becomes_fallback(self):
        # speaker_id is typed str, but Python doesn't enforce that at
        # runtime -- a caller reading an untyped dict can still pass None,
        # which is exactly the fallback path this test exists to cover.
        assert normalize_speaker_label(None) == "SPEAKER_00"  # type: ignore[arg-type]

    def test_already_prefixed_label_unchanged(self):
        assert normalize_speaker_label("SPEAKER_01") == "SPEAKER_01"

    def test_bare_numeric_label_gets_prefixed(self):
        assert normalize_speaker_label("01") == "SPEAKER_01"

    def test_arbitrary_label_gets_a_stable_hashed_index(self):
        # Issue #483: this module used to blindly prefix an unrecognized label
        # ("host" -> "SPEAKER_host"), a format no downstream code recognizes as
        # a normalized speaker id. It now delegates to the canonical
        # app.services.asr.base implementation, which falls back to a stable
        # SHA-256 hash into SPEAKER_00..SPEAKER_99 -- deterministic across
        # processes, unlike the builtin (PYTHONHASHSEED-salted) hash().
        assert normalize_speaker_label("host") == "SPEAKER_25"
        # Same input must always produce the same output.
        assert normalize_speaker_label("host") == "SPEAKER_25"


# ---------------------------------------------------------------------------
# extract_unique_speakers — pure, but mutates segments in place
# ---------------------------------------------------------------------------


class TestExtractUniqueSpeakers:
    def test_normalizes_and_collects_unique_labels(self):
        segments = [
            {"speaker": "SPEAKER_00", "text": "hi"},
            {"speaker": "01", "text": "hello"},
            {"speaker": "SPEAKER_00", "text": "again"},
        ]
        result = extract_unique_speakers(segments)
        assert result == {"SPEAKER_00", "SPEAKER_01"}

    def test_none_speaker_gets_fallback_and_mutates_segment_in_place(self):
        segments = [{"speaker": None, "text": "no diarization"}]
        result = extract_unique_speakers(segments)
        assert result == {"SPEAKER_00"}
        # The whole point: a later consumer reading `segment["speaker"]` must see the
        # fallback, not None, or the FK write downstream fails.
        assert segments[0]["speaker"] == "SPEAKER_00"

    def test_missing_speaker_key_gets_fallback(self):
        segments = [{"text": "cloud provider, no diarization"}]
        result = extract_unique_speakers(segments)
        assert result == {"SPEAKER_00"}
        assert segments[0]["speaker"] == "SPEAKER_00"

    def test_empty_segment_list_returns_empty_set(self):
        assert extract_unique_speakers([]) == set()


# ---------------------------------------------------------------------------
# create_or_get_speaker / create_speaker_mapping — real DB rows
# ---------------------------------------------------------------------------


class TestCreateOrGetSpeaker:
    def test_creates_a_real_speaker_row(self, db_session, normal_user, media_file):
        speaker = create_or_get_speaker(db_session, normal_user.id, media_file.id, "SPEAKER_00")

        db_session.flush()
        fetched = db_session.query(Speaker).filter(Speaker.id == speaker.id).one()
        assert fetched.name == "SPEAKER_00"
        assert fetched.user_id == normal_user.id
        assert fetched.media_file_id == media_file.id
        assert fetched.verified is False
        assert fetched.uuid is not None

    def test_second_call_with_same_label_reuses_the_row(self, db_session, normal_user, media_file):
        first = create_or_get_speaker(db_session, normal_user.id, media_file.id, "SPEAKER_00")
        second = create_or_get_speaker(db_session, normal_user.id, media_file.id, "SPEAKER_00")

        assert first.id == second.id
        count = (
            db_session.query(Speaker)
            .filter(Speaker.media_file_id == media_file.id, Speaker.name == "SPEAKER_00")
            .count()
        )
        assert count == 1

    def test_different_labels_create_distinct_rows(self, db_session, normal_user, media_file):
        first = create_or_get_speaker(db_session, normal_user.id, media_file.id, "SPEAKER_00")
        second = create_or_get_speaker(db_session, normal_user.id, media_file.id, "SPEAKER_01")

        assert first.id != second.id


class TestCreateSpeakerMapping:
    def test_maps_every_label_to_a_real_speaker_id(self, db_session, normal_user, media_file):
        with patch(_INVALIDATE_SPEAKERS) as invalidate:
            mapping = create_speaker_mapping(
                db_session, normal_user.id, media_file.id, {"SPEAKER_00", "SPEAKER_01"}
            )

        assert set(mapping.keys()) == {"SPEAKER_00", "SPEAKER_01"}
        for label, speaker_id in mapping.items():
            row = db_session.query(Speaker).filter(Speaker.id == speaker_id).one()
            assert row.name == label
            assert row.media_file_id == media_file.id
        invalidate.assert_called_once_with(normal_user.id)

    def test_empty_speaker_set_skips_cache_invalidation(self, db_session, normal_user, media_file):
        with patch(_INVALIDATE_SPEAKERS) as invalidate:
            mapping = create_speaker_mapping(db_session, normal_user.id, media_file.id, set())

        assert mapping == {}
        invalidate.assert_not_called()

    def test_repeated_call_does_not_duplicate_rows(self, db_session, normal_user, media_file):
        with patch(_INVALIDATE_SPEAKERS):
            first_mapping = create_speaker_mapping(
                db_session, normal_user.id, media_file.id, {"SPEAKER_00"}
            )
            second_mapping = create_speaker_mapping(
                db_session, normal_user.id, media_file.id, {"SPEAKER_00"}
            )

        assert first_mapping == second_mapping
        count = db_session.query(Speaker).filter(Speaker.media_file_id == media_file.id).count()
        assert count == 1


# ---------------------------------------------------------------------------
# process_segments_with_speakers — pure
# ---------------------------------------------------------------------------


class TestProcessSegmentsWithSpeakers:
    def test_maps_speaker_label_to_db_id(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": "SPEAKER_00"}]
        mapping = {"SPEAKER_00": 42}

        result = process_segments_with_speakers(segments, mapping)

        assert len(result) == 1
        assert result[0]["speaker"] == "SPEAKER_00"
        assert result[0]["speaker_id"] == 42

    def test_speaker_not_in_mapping_gets_null_db_id(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": "SPEAKER_99"}]
        result = process_segments_with_speakers(segments, {"SPEAKER_00": 1})
        assert result[0]["speaker_id"] is None

    def test_missing_speaker_falls_back_to_speaker_00(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "hi"}]
        result = process_segments_with_speakers(segments, {"SPEAKER_00": 7})
        assert result[0]["speaker"] == "SPEAKER_00"
        assert result[0]["speaker_id"] == 7

    def test_words_with_start_and_end_are_kept(self):
        segments = [
            {
                "start": 0.0,
                "end": 1.0,
                "text": "hi there",
                "speaker": "SPEAKER_00",
                "words": [
                    {"word": "hi", "start": 0.0, "end": 0.3, "score": 0.9},
                    {"word": "there", "start": 0.3, "end": 1.0},  # no score -> default 1.0
                ],
            }
        ]
        result = process_segments_with_speakers(segments, {"SPEAKER_00": 1})
        words = result[0]["words"]
        assert len(words) == 2
        assert words[0] == {"word": "hi", "start": 0.0, "end": 0.3, "score": 0.9}
        assert words[1] == {"word": "there", "start": 0.3, "end": 1.0, "score": 1.0}

    def test_word_missing_start_or_end_is_dropped(self):
        segments = [
            {
                "start": 0.0,
                "end": 1.0,
                "text": "hi",
                "speaker": "SPEAKER_00",
                "words": [{"word": "hi", "start": 0.0}],  # no "end" -> dropped
            }
        ]
        result = process_segments_with_speakers(segments, {"SPEAKER_00": 1})
        assert result[0]["words"] == []

    def test_missing_words_key_yields_empty_list(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": "SPEAKER_00"}]
        result = process_segments_with_speakers(segments, {"SPEAKER_00": 1})
        assert result[0]["words"] == []

    def test_missing_confidence_defaults_to_one(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": "SPEAKER_00"}]
        result = process_segments_with_speakers(segments, {"SPEAKER_00": 1})
        assert result[0]["confidence"] == 1.0

    def test_empty_segment_list_returns_empty(self):
        assert process_segments_with_speakers([], {}) == []


# ---------------------------------------------------------------------------
# mark_overlapping_segments — pure, exact-value math
# ---------------------------------------------------------------------------


class TestMarkOverlappingSegments:
    def test_no_overlap_regions_returns_segments_unchanged(self):
        segments = [{"start": 0.0, "end": 1.0}]
        result = mark_overlapping_segments(segments, [])
        assert result == segments
        assert "is_overlap" not in result[0]

    def test_no_segments_returns_empty_unchanged(self):
        result = mark_overlapping_segments([], [{"start": 0.0, "end": 1.0}])
        assert result == []

    def test_region_touching_only_one_segment_marks_nothing(self):
        """mark_overlapping_segments requires 2+ segments per region to form a group."""
        segments = [{"start": 0.0, "end": 1.0}, {"start": 5.0, "end": 6.0}]
        overlap_regions = [{"start": 0.2, "end": 0.5}]  # only touches segment 0
        result = mark_overlapping_segments(segments, overlap_regions)
        assert "is_overlap" not in result[0]
        assert "is_overlap" not in result[1]

    def test_two_segments_sharing_a_region_get_exact_confidence(self):
        segments = [
            {"start": 0.0, "end": 2.0},  # duration 2.0
            {"start": 1.5, "end": 3.0},  # duration 1.5
        ]
        overlap_regions = [{"start": 1.0, "end": 2.5}]

        result = mark_overlapping_segments(segments, overlap_regions)

        assert result[0]["is_overlap"] is True
        assert result[1]["is_overlap"] is True
        assert result[0]["overlap_group_id"] == result[1]["overlap_group_id"]
        # seg0: overlap [1.0, 2.0] = 1.0s of 2.0s duration -> 0.5
        assert result[0]["overlap_confidence"] == pytest.approx(0.5)
        # seg1: overlap [1.5, 2.5] = 1.0s of 1.5s duration -> 0.6667
        assert result[1]["overlap_confidence"] == pytest.approx(2.0 / 3.0)

    def test_a_segment_spanning_two_regions_deterministically_keeps_the_higher_confidence_one(
        self,
    ):
        """Issue #482: when one long segment genuinely overlaps two distinct
        regions, the assignment loop used to silently overwrite with whichever
        region was processed last — order-dependent and arbitrary. The winner
        must now be deterministic: the region this segment overlaps MOST.
        """
        # Region processing order matters for this to be a real regression proof:
        # region 0 (the high-confidence one, processed FIRST) must be the one a
        # naive "last assignment wins" would lose. If a fix regresses back to
        # last-write-wins, this pins the WRONG (lower-confidence) group and fails.
        segments = [
            {"start": 0.0, "end": 10.0},  # A: spans both regions, duration 10.0
            {"start": 3.0, "end": 10.0},  # B: only in region 0 (the high one)
            {"start": 0.0, "end": 1.0},  # C: only in region 1 (the low one)
        ]
        overlap_regions = [
            {"start": 3.0, "end": 10.0},  # region 0: A overlaps 7.0/10.0 = 0.7 (high)
            {"start": 0.0, "end": 1.0},  # region 1: A overlaps 1.0/10.0 = 0.1 (low)
        ]

        result = mark_overlapping_segments(segments, overlap_regions)

        seg_a, seg_b, seg_c = result
        assert seg_a["is_overlap"] is True
        # A must win the higher-confidence region (region 0, shared with B) —
        # never region 1 (shared with C), regardless of loop iteration order.
        assert seg_a["overlap_group_id"] == seg_b["overlap_group_id"]
        assert seg_a["overlap_group_id"] != seg_c["overlap_group_id"]
        assert seg_a["overlap_confidence"] == pytest.approx(0.7)

    def test_distinct_regions_get_distinct_group_ids(self):
        segments = [
            {"start": 0.0, "end": 1.0},
            {"start": 0.5, "end": 1.5},
            {"start": 10.0, "end": 11.0},
            {"start": 10.5, "end": 11.5},
        ]
        overlap_regions = [
            {"start": 0.2, "end": 0.8},
            {"start": 10.2, "end": 10.8},
        ]

        result = mark_overlapping_segments(segments, overlap_regions)

        group_ids = {s["overlap_group_id"] for s in result}
        assert len(group_ids) == 2
        assert result[0]["overlap_group_id"] == result[1]["overlap_group_id"]
        assert result[2]["overlap_group_id"] == result[3]["overlap_group_id"]
        assert result[0]["overlap_group_id"] != result[2]["overlap_group_id"]
