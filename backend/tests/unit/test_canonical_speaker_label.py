"""``canonical_speaker_label`` — the single home for speaker display-name resolution.

Before this helper existed, three planes each resolved a speaker's name differently for
the SAME underlying data: ``transcript_builders.get_speaker_name`` decorated an
unverified-but-confident suggestion with the English literal ``" (suggested)"`` and
ignored an unverified ``display_name`` outright; ``SpeakerStatusService`` used any
``display_name`` unconditionally but ignored suggestions entirely; the chunk-index writers
defaulted an unattributed segment to bare ``"Unknown"`` while ``file_facts`` wrote
``"Unknown Speaker"``. This suite pins the one resolution every plane this change touches
now shares, and that no plane emits the "(suggested)" literal into a label that could reach
an answer.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.ingest_artifacts.digest import build_digest
from app.services.ingest_artifacts.facts import build_facts
from app.services.speaker_status_service import SpeakerStatusService
from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABEL
from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABELS
from app.utils.speaker_labels import canonical_speaker_label
from app.utils.transcript_builders import compute_speaker_stats
from app.utils.transcript_builders import get_speaker_name

# --------------------------------------------------------------------------- priority


def test_a_display_name_wins_even_when_unverified():
    """The union of the two prior behaviours: a human's label, even provisional,
    outranks the raw diarization tag — unlike the old ``get_speaker_name``, which
    silently ignored an unverified ``display_name``."""
    assert canonical_speaker_label("SPEAKER_00", display_name="Alice") == "Alice"


def test_a_confident_suggestion_is_used_when_there_is_no_display_name():
    assert canonical_speaker_label("SPEAKER_00", suggested_name="Bob", confidence=0.9) == "Bob"


def test_a_suggestion_below_threshold_is_ignored():
    assert (
        canonical_speaker_label("SPEAKER_00", suggested_name="Bob", confidence=0.5) == "SPEAKER_00"
    )


def test_a_suggestion_below_threshold_with_no_raw_name_is_unidentified():
    """The exact shape of the 398-chunk drift (``tests/integration/
    test_speaker_label_index_drift.py``): a sub-threshold suggestion next to an
    unresolved raw name must fall all the way through to the canonical unknown
    label, never to the rejected suggestion."""
    assert (
        canonical_speaker_label(None, suggested_name="Joe Rogan", confidence=0.7006)
        == UNKNOWN_SPEAKER_LABEL
    )


def test_a_suggestion_exactly_at_threshold_is_used():
    """The comparison is ``>=``, not ``>`` — pin the boundary itself rather than
    only its interior on each side."""
    assert canonical_speaker_label("SPEAKER_00", suggested_name="Bob", confidence=0.75) == "Bob"


def test_a_suggestion_just_below_threshold_is_ignored():
    """The other side of the boundary, one float ULP-scale step under it."""
    assert (
        canonical_speaker_label("SPEAKER_00", suggested_name="Bob", confidence=0.7499999)
        == "SPEAKER_00"
    )


def test_a_display_name_beats_a_confident_suggestion():
    """A human's label outranks a machine's guess — the deliberate resolution of the
    disagreement between the two prior implementations (see module docstring)."""
    assert (
        canonical_speaker_label(
            "SPEAKER_00", display_name="Alice", suggested_name="Bob", confidence=0.99
        )
        == "Alice"
    )


def test_the_raw_name_is_the_last_resort():
    assert canonical_speaker_label("SPEAKER_00") == "SPEAKER_00"


def test_nothing_at_all_resolves_to_the_unknown_label():
    assert canonical_speaker_label(None) == UNKNOWN_SPEAKER_LABEL


def test_a_confidence_of_none_does_not_crash_the_comparison():
    """``suggested_name`` set with no ``confidence`` must not raise (a bare ``>=`` against
    ``None`` would); it must simply not qualify."""
    assert canonical_speaker_label("SPEAKER_00", suggested_name="Bob") == "SPEAKER_00"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"suggested_name": "Bob", "confidence": 0.9},
        {"display_name": "Alice", "suggested_name": "Bob", "confidence": 0.99},
        {"name": None, "suggested_name": "Bob", "confidence": 1.0},
    ],
)
def test_the_label_never_carries_the_suggested_decoration(kwargs):
    """The exact bug: the old ``get_speaker_name`` returned ``"Bob (suggested)"``, an
    English literal baked into the label that a multilingual pass would have to strip."""
    name = kwargs.pop("name", "SPEAKER_00")
    label = canonical_speaker_label(name, **kwargs)
    assert "(suggested)" not in label


# --------------------------------------------------------------------- the unknown set


def test_unknown_speaker_label_is_a_member_of_the_label_set():
    assert UNKNOWN_SPEAKER_LABEL in UNKNOWN_SPEAKER_LABELS


def test_the_legacy_bare_unknown_spelling_is_also_a_member():
    """The chunk-index writers (out of this change's file set) still default to the bare
    ``"Unknown"`` spelling. A caller filtering an aggregate must exclude both spellings or
    those rows read back in as though they were a real participant named "Unknown"."""
    assert "Unknown" in UNKNOWN_SPEAKER_LABELS


def test_a_real_name_is_not_in_the_unknown_set():
    """Guards the guard: without this, a membership assertion could pass vacuously against
    an accidentally-universal set."""
    assert "Alice" not in UNKNOWN_SPEAKER_LABELS
    assert canonical_speaker_label("Alice") not in UNKNOWN_SPEAKER_LABELS


# ------------------------------------------------------------------------- delegation


def _speaker(**kwargs):
    defaults = dict(name="SPEAKER_00", display_name=None, suggested_name=None, confidence=None)
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _segment(speaker):
    return SimpleNamespace(speaker=speaker)


def test_get_speaker_name_delegates_to_the_canonical_helper():
    speaker = _speaker(name="SPEAKER_00", display_name="Dana")
    segment = _segment(speaker)

    assert get_speaker_name(segment) == canonical_speaker_label(
        speaker.name,
        display_name=speaker.display_name,
        suggested_name=speaker.suggested_name,
        confidence=speaker.confidence,
    )
    assert get_speaker_name(segment) == "Dana"


def test_get_speaker_name_returns_the_canonical_unknown_label_when_there_is_no_speaker():
    assert get_speaker_name(_segment(None)) == UNKNOWN_SPEAKER_LABEL


def test_get_speaker_name_no_longer_decorates_a_confident_suggestion():
    """Regression: this used to return ``"Marcus (suggested)"``."""
    speaker = _speaker(name="SPEAKER_01", suggested_name="Marcus", confidence=0.9)
    assert get_speaker_name(_segment(speaker)) == "Marcus"


def test_speaker_status_service_resolved_display_name_delegates_to_the_canonical_helper():
    """Regression: ``_resolve_display_name`` used to ignore suggestions entirely, so the
    same speaker could read differently here than in ``get_speaker_name``."""
    speaker = _speaker(name="SPEAKER_02", suggested_name="Priya", confidence=0.9)

    resolved = SpeakerStatusService._resolve_display_name(speaker)

    assert resolved == "Priya"
    assert resolved == get_speaker_name(_segment(speaker))


# ------------------------------------------------------ three-plane agreement (issue)


class _StatsSpeaker:
    def __init__(self, name, display_name=None, suggested_name=None, confidence=None):
        self.name = name
        self.display_name = display_name
        self.suggested_name = suggested_name
        self.confidence = confidence


class _StatsSegment:
    def __init__(self, text, start, end, speaker):
        self.text = text
        self.start_time = start
        self.end_time = end
        self.speaker = speaker


def test_facts_and_digest_agree_with_get_speaker_name_on_the_same_speaker():
    """Facts / digest / (the resolver's) get_speaker_name must produce the SAME label for
    the same underlying speaker data. Before ``canonical_speaker_label`` existed, facts
    used ``get_speaker_name``'s output already (so this could not previously drift for
    plain diarized speakers), but a display_name+suggestion combination would have: facts
    (via get_speaker_name) would have shown the raw name, while a status/UI read of the
    same speaker (via the old ``_resolve_display_name``) would have shown the display name.
    Both now resolve to the SAME canonical label.
    """
    text = "Good morning everyone, thanks for joining this call today."
    speaker = _StatsSpeaker(
        "SPEAKER_00", display_name="Priya", suggested_name="Someone Else", confidence=0.99
    )
    resolved = get_speaker_name(_StatsSegment(text, 0.0, 5.0, speaker))
    assert resolved == "Priya"

    segments_for_stats = [_StatsSegment(text, 0.0, 5.0, speaker)]
    stats = compute_speaker_stats(segments_for_stats)
    # facts.py's segment dicts carry the ALREADY-RESOLVED name, exactly as
    # `ingest_artifacts.service.load_ordered_segments` produces them.
    seg_dicts = [{"id": 1, "text": text, "start_time": 0.0, "end_time": 5.0, "speaker": resolved}]
    facts = build_facts(
        seg_dicts, speaker_stats=stats, duration=5.0, language="en", recorded_at=None
    )
    assert facts["roster"] == [resolved]

    digest = build_digest(seg_dicts, language="en")
    section_speakers = {s for section in digest["sections"] for s in section["speakers"]}
    assert section_speakers == {resolved}


# ------------------------------------------------- W2.1: the remaining planes agree too
#
# The module docstring named five planes that disagreed. facts/digest/
# speaker_status_service/transcript_builders were unified above; these tests
# widen the same agreement check to the chunk-index writers
# (search_indexing_task, reindex_task), files/crud.py's segment formatter, and
# formatting_service.py — the "remaining work to close the disagreement
# completely" the module docstring flagged as still open.


def test_the_chunk_index_writers_agree_with_get_speaker_name():
    """Both chunk-index writers now delegate to `canonical_speaker_label`
    through their own resolution helpers — called here directly, not via a
    second call to `canonical_speaker_label`, so a writer that reverted to its
    own ad hoc chain would make THIS comparison fail rather than trivially
    re-proving the helper against itself."""
    from app.tasks.reindex_task import _resolve_reindex_speaker_name
    from app.tasks.search_indexing_task import resolve_chunk_speaker_name

    speaker = _StatsSpeaker(
        "SPEAKER_00", display_name="Priya", suggested_name="Someone Else", confidence=0.99
    )
    resolved = get_speaker_name(_StatsSegment("hi", 0.0, 1.0, speaker))
    assert resolved == "Priya"

    assert resolve_chunk_speaker_name(speaker) == resolved
    assert _resolve_reindex_speaker_name(speaker) == resolved


def test_files_crud_and_formatting_service_agree_with_get_speaker_name():
    from app.api.endpoints.files.crud import _resolve_segment_speaker_name
    from app.services.formatting_service import FormattingService

    # `_speaker()` (defined above), not `_StatsSpeaker`: the functions under
    # test here are typed against the real ORM `Speaker`, and `_speaker()`'s
    # own signature is deliberately untyped (mypy then treats its return as
    # `Any`, same as every other `_speaker(...)` call in this file) — a
    # concrete stub class would fail mypy's `Speaker`-typed parameters.
    speaker = _speaker(
        name="SPEAKER_01", display_name=None, suggested_name="Marcus", confidence=0.9
    )
    resolved = get_speaker_name(_segment(speaker))
    assert resolved == "Marcus"

    assert _resolve_segment_speaker_name(speaker) == resolved
    assert FormattingService.format_speaker_name(speaker) == resolved
    assert FormattingService.create_speaker_summary([speaker])["primary_speakers"] == [resolved]


def test_every_plane_agrees_on_the_canonical_unknown_spelling():
    """Regression: the chunk-index writers defaulted an unattributed segment to
    the bare ``"Unknown"`` and `files/crud.py` to lowercase ``"Unknown
    speaker"`` — a THIRD and FOURTH spelling next to `file_facts`'s
    ``"Unknown Speaker"``. All must now agree on `UNKNOWN_SPEAKER_LABEL`."""
    from app.api.endpoints.files.crud import _resolve_segment_speaker_name
    from app.tasks.search_indexing_task import resolve_chunk_speaker_name

    assert resolve_chunk_speaker_name(None) == UNKNOWN_SPEAKER_LABEL
    assert _resolve_segment_speaker_name(None) == UNKNOWN_SPEAKER_LABEL


def test_a_drifted_plane_would_be_caught_by_the_agreement_tests_above(monkeypatch):
    """Guards the guard, as the review brief asked: deliberately revert ONE
    plane to its old ad hoc chain (in-process, restored by monkeypatch's own
    teardown — no source file is edited) and prove the agreement assertion
    would have failed had the drift been real."""
    import app.tasks.search_indexing_task as sit

    def _old_ad_hoc_resolution(speaker):
        if speaker is None:
            return "Unknown"
        display_name = speaker.display_name if speaker and speaker.display_name else speaker.name
        return display_name or "Unknown"

    monkeypatch.setattr(sit, "resolve_chunk_speaker_name", _old_ad_hoc_resolution)

    speaker = _StatsSpeaker("SPEAKER_00", suggested_name="Marcus", confidence=0.9)
    resolved = get_speaker_name(_StatsSegment("hi", 0.0, 1.0, speaker))

    assert resolved == "Marcus"
    assert sit.resolve_chunk_speaker_name(speaker) == "SPEAKER_00", (
        "the drifted (old) resolution must disagree with the canonical one, "
        "proving the agreement tests above are load-bearing rather than vacuous"
    )
    assert sit.resolve_chunk_speaker_name(speaker) != resolved


# --------------------------------------------------------------- facts coverage/exclusion


def _facts_from_script(script):
    """script: list of (speaker_or_None, text, start, end). ``speaker=None`` means
    undiarized — ``_StatsSegment`` accepts it directly since ``get_speaker_name`` only
    checks truthiness of ``segment.speaker``."""
    segments = [_StatsSegment(t, s, e, sp) for sp, t, s, e in script]
    resolved_names = [get_speaker_name(seg) for seg in segments]
    seg_dicts = [
        {
            "id": i + 1,
            "text": seg.text,
            "start_time": seg.start_time,
            "end_time": seg.end_time,
            "speaker": name,
        }
        for i, (seg, name) in enumerate(zip(segments, resolved_names, strict=True))
    ]
    stats = compute_speaker_stats(segments)
    return build_facts(
        seg_dicts, speaker_stats=stats, duration=None, language="en", recorded_at=None
    )


def test_the_roster_excludes_the_undiarized_bucket():
    dana = _StatsSpeaker("Dana", display_name="Dana")
    facts = _facts_from_script(
        [
            (dana, "hello there friend", 0.0, 5.0),
            (None, "unattributed backchannel here", 5.0, 8.0),
        ]
    )
    assert UNKNOWN_SPEAKER_LABEL not in facts["roster"]
    assert facts["roster"] == ["Dana"]
    assert all(s["name"] != UNKNOWN_SPEAKER_LABEL for s in facts["speakers"])


def test_facts_coverage_reports_the_undiarized_exclusion():
    dana = _StatsSpeaker("Dana", display_name="Dana")
    facts = _facts_from_script(
        [
            (dana, "hello there friend", 0.0, 5.0),
            (None, "unattributed backchannel here", 5.0, 8.0),
        ]
    )
    assert facts["coverage"]["undiarized_files_excluded"] == 1
    assert facts["coverage"]["undiarized_segment_count"] == 1


def test_facts_coverage_is_zero_when_every_segment_is_diarized():
    """Control: guards against a detector that always reports an exclusion."""
    dana = _StatsSpeaker("Dana", display_name="Dana")
    facts = _facts_from_script([(dana, "hello there friend", 0.0, 5.0)])
    assert facts["coverage"]["undiarized_files_excluded"] == 0
    assert facts["coverage"]["undiarized_segment_count"] == 0
