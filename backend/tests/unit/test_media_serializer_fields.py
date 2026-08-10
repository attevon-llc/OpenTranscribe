"""Unit tests for additive MediaFileDetail / Speaker serializer fields.

Covers the thin-frontend serializer additions:
- ``resolved_speaker_name`` is always populated (never null) on transcript segments.
- ``Speaker.profile_name`` / ``Speaker.profile_status`` populate when a profile is
  linked and are ``None`` otherwise.
- ``grouped_segments`` grouping matches the frontend ``groupedTranscriptSegments`` logic.

These are pure-Pydantic / pure-Python tests; no DB or live stack required.
"""

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

from app.api.endpoints.files.crud import _build_grouped_segments
from app.api.endpoints.files.crud import _resolve_segment_speaker_name
from app.schemas.media import TranscriptSegment
from app.services.formatting_service import FormattingService
from app.services.speaker_status_service import SpeakerStatusService

# ---------------------------------------------------------------------------
# Task 1: resolved_speaker_name is always non-null
# ---------------------------------------------------------------------------


def _make_segment_model(
    *,
    speaker: SimpleNamespace | None,
    overlap_group_id=None,
    start_time: float = 0.0,
    end_time: float = 1.0,
):
    """Build a duck-typed segment model that ``model_validate`` accepts."""
    return SimpleNamespace(
        uuid=uuid4(),
        media_file_id=uuid4(),
        speaker_id=(speaker.uuid if speaker else None),
        speaker=speaker,
        start_time=start_time,
        end_time=end_time,
        text="hello world",
        is_overlap=overlap_group_id is not None,
        overlap_group_id=overlap_group_id,
        overlap_confidence=None,
        confidence=None,
        words=None,
        redactions=None,
        toxicity=None,
    )


def _make_speaker(
    *, name="SPEAKER_01", display_name=None, resolved_display_name=None, profile=None
):
    return SimpleNamespace(
        uuid=uuid4(),
        user_id=uuid4(),
        media_file_id=uuid4(),
        name=name,
        display_name=display_name,
        resolved_display_name=resolved_display_name,
        suggested_name=None,
        verified=False,
        confidence=None,
        created_at=datetime.now(UTC),
        profile=profile,
        profile_id=(getattr(profile, "id", None) if profile else None),
        computed_status=None,
        status_text=None,
        status_color=None,
        profile_name=None,
        profile_status=None,
        predicted_gender=None,
        predicted_age_range=None,
        attribute_confidence=None,
        attributes_predicted_at=None,
    )


def test_resolved_speaker_name_uses_display_name():
    speaker = _make_speaker(name="SPEAKER_01", display_name="Alice")
    seg = _make_segment_model(speaker=speaker)
    out = FormattingService.format_transcript_segment(seg)
    assert out.resolved_speaker_name == "Alice"


def test_resolved_speaker_name_falls_back_to_label():
    speaker = _make_speaker(name="SPEAKER_02", display_name=None)
    seg = _make_segment_model(speaker=speaker)
    out = FormattingService.format_transcript_segment(seg)
    assert out.resolved_speaker_name == "SPEAKER_02"


def test_resolved_speaker_name_never_null_without_speaker():
    seg = _make_segment_model(speaker=None)
    out = FormattingService.format_transcript_segment(seg)
    assert out.resolved_speaker_name is not None
    assert isinstance(out.resolved_speaker_name, str)
    assert out.resolved_speaker_name != ""


def test_resolve_segment_speaker_name_helper():
    assert _resolve_segment_speaker_name(None) == "Unknown speaker"
    assert _resolve_segment_speaker_name(_make_speaker(display_name="Bob")) == "Bob"
    assert (
        _resolve_segment_speaker_name(_make_speaker(name="SPEAKER_03", display_name=None))
        == "SPEAKER_03"
    )


# ---------------------------------------------------------------------------
# Task 2: Speaker.profile_name / profile_status
# ---------------------------------------------------------------------------


def test_profile_fields_populate_when_linked():
    profile = SimpleNamespace(id=7, name="Jane Doe")
    speaker = _make_speaker(display_name="Jane Doe", profile=profile)
    info = SpeakerStatusService.compute_speaker_status(speaker)
    assert info["profile_name"] == "Jane Doe"
    assert info["profile_status"] == "linked"


def test_profile_fields_none_when_unlinked():
    speaker = _make_speaker(display_name=None, profile=None)
    info = SpeakerStatusService.compute_speaker_status(speaker)
    assert info["profile_name"] is None
    assert info["profile_status"] is None


def test_add_computed_status_sets_profile_fields():
    profile = SimpleNamespace(id=3, name="Carol")
    speaker = _make_speaker(display_name="Carol", profile=profile)
    SpeakerStatusService.add_computed_status(speaker)
    assert speaker.profile_name == "Carol"
    assert speaker.profile_status == "linked"


# ---------------------------------------------------------------------------
# Task 3: grouped_segments matches the frontend grouping
# ---------------------------------------------------------------------------


def _fmt(speaker, overlap_group_id, start, end):
    """Produce a formatted TranscriptSegment schema, as the response builder does."""
    return FormattingService.format_transcript_segment(
        _make_segment_model(
            speaker=speaker,
            overlap_group_id=overlap_group_id,
            start_time=start,
            end_time=end,
        )
    )


def _reference_grouping(segments: list[TranscriptSegment], index_offset: int = 0) -> list[dict]:
    """Independent port of the grouping rules, for comparison.

    Mirrors ``_build_grouped_segments``: an overlap run of >1 member becomes one
    overlap group; every other segment is its own group. ``overlap_group_id`` is
    carried on single-member groups too, so a run split across a pagination boundary
    stays stitchable.
    """
    groups: list[dict] = []
    i = 0
    n = len(segments)
    while i < n:
        seg = segments[i]
        if seg.overlap_group_id:
            gid = seg.overlap_group_id
            members = [seg]
            j = i + 1
            while j < n and segments[j].overlap_group_id == gid:
                members.append(segments[j])
                j += 1
            if len(members) > 1:
                groups.append(
                    {
                        "is_overlap_group": True,
                        "overlap_group_id": gid,
                        "start_time": min(s.start_time for s in members),
                        "end_time": max(s.end_time for s in members),
                        "start_segment_index": index_offset + i,
                        "uuids": [s.uuid for s in members],
                    }
                )
                i = j
                continue
            groups.append(
                {
                    "is_overlap_group": False,
                    "overlap_group_id": gid,
                    "start_time": seg.start_time,
                    "end_time": seg.end_time,
                    "start_segment_index": index_offset + i,
                    "uuids": [seg.uuid],
                }
            )
            i += 1
        else:
            groups.append(
                {
                    "is_overlap_group": False,
                    "overlap_group_id": None,
                    "start_time": seg.start_time,
                    "end_time": seg.end_time,
                    "start_segment_index": index_offset + i,
                    "uuids": [seg.uuid],
                }
            )
            i += 1
    return groups


def _sample_segments(spk, gid_a, gid_b) -> list[TranscriptSegment]:
    """Regular, a 2-member overlap run, a lone overlap-flagged segment, a regular,
    then a 3-member overlap run reusing ``gid_a`` (non-adjacent, so a separate run)."""
    return [
        _fmt(spk, None, 0.0, 1.0),  # 0 regular
        _fmt(spk, gid_a, 1.0, 2.5),  # 1 overlap group A
        _fmt(spk, gid_a, 1.2, 2.0),  # 2 overlap group A
        _fmt(spk, gid_b, 3.0, 4.0),  # 3 lone overlap -> regular
        _fmt(spk, None, 4.0, 5.0),  # 4 regular
        _fmt(spk, gid_a, 5.0, 6.0),  # 5 overlap group A (reused id, new run)
        _fmt(spk, gid_a, 5.1, 6.5),  # 6
        _fmt(spk, gid_a, 5.2, 7.0),  # 7
    ]


def test_grouped_segments_matches_reference_logic():
    spk = _make_speaker(display_name="Alice")
    gid_a = uuid4()
    gid_b = uuid4()
    segments = _sample_segments(spk, gid_a, gid_b)

    groups = _build_grouped_segments(segments)
    reference = _reference_grouping(segments)

    assert len(groups) == len(reference)
    for got, ref in zip(groups, reference, strict=True):
        assert got.is_overlap_group == ref["is_overlap_group"]
        assert got.overlap_group_id == ref["overlap_group_id"]
        assert got.start_time == ref["start_time"]
        assert got.end_time == ref["end_time"]
        assert got.start_segment_index == ref["start_segment_index"]
        assert got.segment_uuids == ref["uuids"]

    # Spot-check shapes: first overlap group spans segments 1-2.
    overlap_a = groups[1]
    assert overlap_a.is_overlap_group is True
    assert overlap_a.start_segment_index == 1
    assert overlap_a.start_time == 1.0
    assert overlap_a.end_time == 2.5
    assert overlap_a.segment_uuids == [segments[1].uuid, segments[2].uuid]

    # The lone overlap-flagged segment (index 3) renders as a regular single group,
    # but keeps its overlap id so a split run stays stitchable.
    lone = groups[2]
    assert lone.is_overlap_group is False
    assert lone.start_segment_index == 3
    assert lone.overlap_group_id == gid_b

    # The trailing 3-member overlap group.
    overlap_tail = groups[-1]
    assert overlap_tail.is_overlap_group is True
    assert len(overlap_tail.segment_uuids) == 3
    assert overlap_tail.start_segment_index == 5


def test_grouped_segments_reference_segments_by_uuid_only():
    """Groups must not embed segment copies — that was the #352 dual-copy bug."""
    spk = _make_speaker(display_name="Alice")
    groups = _build_grouped_segments([_fmt(spk, None, 0.0, 1.0)])

    assert not hasattr(groups[0], "segments")
    dumped = groups[0].model_dump()
    assert "segments" not in dumped
    assert dumped["segment_uuids"]


def test_grouped_segments_index_offset_is_global():
    """``start_segment_index`` must be absolute, or the SPA's reading-progress bar
    (min visible index / total) jumps backwards on every page after the first."""
    spk = _make_speaker(display_name="Alice")
    gid = uuid4()
    page = [
        _fmt(spk, None, 0.0, 1.0),
        _fmt(spk, gid, 1.0, 2.0),
        _fmt(spk, gid, 1.5, 2.5),
    ]

    groups = _build_grouped_segments(page, index_offset=500)

    assert [g.start_segment_index for g in groups] == [500, 501]
    # The default keeps page-one behaviour unchanged.
    assert [g.start_segment_index for g in _build_grouped_segments(page)] == [0, 1]


def test_grouped_segments_split_overlap_run_stays_stitchable():
    """An overlap run straddling a page boundary must expose its id on BOTH sides.

    The client stitches the two halves by that id; without it on the single-member
    tail the halves render as two groups sharing one Svelte key, which throws.
    """
    spk = _make_speaker(display_name="Alice")
    gid = uuid4()
    run = [
        _fmt(spk, gid, 1.0, 2.0),
        _fmt(spk, gid, 1.5, 2.5),
        _fmt(spk, gid, 2.0, 3.0),
    ]

    # Page 1 holds two members, page 2 holds the third.
    page_one = _build_grouped_segments(run[:2], index_offset=0)
    page_two = _build_grouped_segments(run[2:], index_offset=2)

    assert page_one[-1].is_overlap_group is True
    assert page_one[-1].overlap_group_id == gid
    # Run length 1 on this page, so not an overlap group — but the id survives.
    assert page_two[0].is_overlap_group is False
    assert page_two[0].overlap_group_id == gid
    assert page_two[0].start_segment_index == 2

    stitched = page_one[-1].segment_uuids + page_two[0].segment_uuids
    assert stitched == [s.uuid for s in run]


def test_grouped_segments_empty():
    assert _build_grouped_segments([]) == []
