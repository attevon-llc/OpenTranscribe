# mypy: disable-error-code="operator,index,type-var,arg-type,return-value,union-attr"
# Same rationale as test_corpus_injection_adapters.py: these assert directly on
# ``Turn.start``/``Turn.end`` (``float | None`` until timings are resolved) and on
# ``doc.timing``/``doc.extra`` fields populated by the call under test.
"""AMI distractor adapter (issue #461 A5) — tiny fixture XML, no AMI download needed.

Verified separately against the REAL corpus and NOT re-asserted here (that assertion lives
in ``TestAgainstTheRealCorpus`` below, gated behind ``$RAG_EVAL_DATA_DIR``): the distractor
set is exactly 34 meetings (EN 16, IN 10, IB 7, TS 1) once QMSum's 137-meeting ``Product``
domain is excluded from AMI's 171. These fixture tests prove the ALGORITHM — exclusion,
channel discovery, segment/word parsing, speaker labelling — with hand-built XML small
enough to read in one sitting, so they run in CI with no NAS mount and no license fetch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.scripts.corpus_injection.adapters import build_adapter
from app.scripts.corpus_injection.adapters.ami import AMIDistractorAdapter
from app.scripts.corpus_injection.adapters.ami import _join_words
from app.scripts.corpus_injection.adapters.ami import _read_words_by_id
from app.scripts.corpus_injection.adapters.ami import _segment_id_range
from app.scripts.corpus_injection.model import TIMING_REAL
from tests.unit.test_corpus_injection_adapters import NAS_ROOT

# ---------------------------------------------------------------- fixtures


def _words_xml(
    meeting: str, channel: str, elements: list[tuple[str, str, float | None, float | None]]
) -> str:
    """``elements``: ``(tag, text, start, end)``. ``tag='w'`` for a real word,
    ``'vocalsound'``/``'disfmarker'``/``'gap'`` for a non-verbal marker."""
    body_lines = []
    for i, (tag, text, start, end) in enumerate(elements):
        attrs = f'nite:id="{meeting}.{channel}.words{i}"'
        if start is not None:
            attrs += f' starttime="{start}"'
        if end is not None:
            attrs += f' endtime="{end}"'
        if text:
            body_lines.append(f"   <{tag} {attrs}>{text}</{tag}>")
        else:
            body_lines.append(f"   <{tag} {attrs}/>")
    body = "\n".join(body_lines)
    return (
        '<?xml version="1.0" encoding="ISO-8859-1" standalone="yes"?>\n'
        f'<nite:root nite:id="{meeting}.{channel}.words" '
        'xmlns:nite="http://nite.sourceforge.net/">\n'
        f"{body}\n</nite:root>\n"
    )


def _segments_xml(
    meeting: str, channel: str, segments: list[tuple[float, float, str, str | None]]
) -> str:
    """``segments``: ``(start, end, first_word_id_suffix, last_word_id_suffix_or_None)``.

    A ``None`` last id produces a single-``id()`` href (no ``..id()`` range) — the other
    real shape (~10% of the real corpus).
    """
    body_lines = []
    for i, (start, end, first, last) in enumerate(segments):
        if last is None:
            href = f"{meeting}.{channel}.words.xml#id({meeting}.{channel}.words{first})"
        else:
            href = (
                f"{meeting}.{channel}.words.xml#id({meeting}.{channel}.words{first})"
                f"..id({meeting}.{channel}.words{last})"
            )
        body_lines.append(
            f'   <segment nite:id="{meeting}.sync.{i}" channel="0" '
            f'transcriber_start="{start}" transcriber_end="{end}">\n'
            f'      <nite:child href="{href}"/>\n'
            "   </segment>"
        )
    body = "\n".join(body_lines)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<nite:root xmlns:nite="http://nite.sourceforge.net/" nite:id="{meeting}.{channel}.segs">\n'
        f"{body}\n</nite:root>\n"
    )


def _meetings_xml(entries: dict[str, list[tuple[str, str, str]]]) -> str:
    """``entries``: ``{observation: [(nxt_agent, role_or_empty, global_name), ...]}``.

    A speaker with an empty ``role`` gets NO ``role=`` attribute at all — matching the real
    corpus, where most meetings' `<speaker>` entries simply omit it (see the module docstring
    on ``ami.py`` for why the adapter must not assume it is present).
    """
    meetings = []
    for observation, speakers in entries.items():
        speaker_lines = []
        for agent, role, global_name in speakers:
            role_attr = f' role="{role}"' if role else ""
            speaker_lines.append(
                f'    <speaker nite:id="{observation}.{agent}" nxt_agent="{agent}"'
                f'{role_attr} global_name="{global_name}" />'
            )
        meetings.append(
            f'  <meeting nite:id="m-{observation}" observation="{observation}">\n'
            + "\n".join(speaker_lines)
            + "\n  </meeting>"
        )
    return (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        '<corpus xmlns:nite="http://nite.sourceforge.net/">\n'
        + "\n".join(meetings)
        + "\n</corpus>\n"
    )


def _qmsum_product_tree(tmp_path: Path, meeting_ids: list[str]) -> Path:
    root = tmp_path / "qmsum" / "QMSum-83d7768c1f2b4dfeb091385d3dc7e239b8e5bb7e"
    target = root / "data" / "Product" / "all"
    target.mkdir(parents=True, exist_ok=True)
    for meeting_id in meeting_ids:
        (target / f"{meeting_id}.json").write_text(
            json.dumps(
                {"meeting_transcripts": [], "specific_query_list": [], "general_query_list": []}
            ),
            encoding="utf-8",
        )
    return tmp_path / "qmsum"


@pytest.fixture
def ami_tree(tmp_path: Path) -> Path:
    """One distractor meeting (``TESTD01``, two channels) and one QMSum-covered meeting
    (``TESTQ01``, present in AMI's own ``meetings.xml`` but excluded via the QMSum tree)."""
    root = tmp_path / "ami" / "ami_public_manual_1.6.2"
    (root / "words").mkdir(parents=True)
    (root / "segments").mkdir(parents=True)
    (root / "corpusResources").mkdir(parents=True)
    (root / "corpusResources" / "meetings.xml").write_text(
        _meetings_xml(
            {
                # Channel A has a role (PM); channel B has NONE — meetings.xml also omits
                # a THIRD channel (C) entirely, matching the real IN1001 under-listing.
                "TESTD01": [("A", "PM", "MEE001"), ("B", "", "FEE002")],
                "TESTQ01": [("A", "PM", "MEE003")],
            }
        ),
        encoding="ISO-8859-1",
    )
    (root / "words" / "TESTD01.A.words.xml").write_text(
        _words_xml(
            "TESTD01",
            "A",
            [
                ("w", "Okay", 0.0, 0.5),
                ("w", ".", 0.5, 0.5),  # attaches with no leading space
                ("w", "I", 2.0, 2.2),
                ("w", "'ve", 2.2, 2.5),  # clitic — attaches with no leading space
                ("w", "arrived", 2.5, 3.0),
                ("vocalsound", "", 5.0, 5.2),  # non-verbal-only segment target
            ],
        ),
        encoding="ISO-8859-1",
    )
    (root / "segments" / "TESTD01.A.segments.xml").write_text(
        _segments_xml(
            "TESTD01",
            "A",
            [
                (0.0, 0.5, "0", "1"),  # "Okay."
                (2.0, 3.0, "2", "4"),  # "I've arrived."
                (5.0, 5.2, "5", None),  # vocalsound only -> must be dropped
            ],
        ),
        encoding="UTF-8",
    )
    # Channel B has no role in meetings.xml -> speaker label must fall back to
    # "Participant B". Single-id href form (no "..id()") exercised here.
    (root / "words" / "TESTD01.B.words.xml").write_text(
        _words_xml("TESTD01", "B", [("w", "Right", 1.0, 1.4)]),
        encoding="ISO-8859-1",
    )
    (root / "segments" / "TESTD01.B.segments.xml").write_text(
        _segments_xml("TESTD01", "B", [(1.0, 1.4, "0", None)]),
        encoding="UTF-8",
    )
    # Channel C: listed nowhere in meetings.xml but DOES have segments/words files on
    # disk — channel discovery must find it anyway (the real IN1001 shape).
    (root / "words" / "TESTD01.C.words.xml").write_text(
        _words_xml("TESTD01", "C", [("w", "Sure", 6.0, 6.3)]),
        encoding="ISO-8859-1",
    )
    (root / "segments" / "TESTD01.C.segments.xml").write_text(
        _segments_xml("TESTD01", "C", [(6.0, 6.3, "0", None)]),
        encoding="UTF-8",
    )
    # TESTQ01 exists in AMI's own meetings.xml but MUST be excluded because QMSum already
    # covers it (see the qmsum fixture built alongside this one in each test).
    (root / "words" / "TESTQ01.A.words.xml").write_text(
        _words_xml("TESTQ01", "A", [("w", "Hi", 0.0, 0.3)]),
        encoding="ISO-8859-1",
    )
    (root / "segments" / "TESTQ01.A.segments.xml").write_text(
        _segments_xml("TESTQ01", "A", [(0.0, 0.3, "0", None)]),
        encoding="UTF-8",
    )
    return tmp_path / "ami"


# ------------------------------------------------------------- unit-level parsing


class TestHrefRangeParsing:
    def test_a_range_href_returns_both_endpoints(self):
        href = "TESTD01.A.words.xml#id(TESTD01.A.words2)..id(TESTD01.A.words13)"
        assert _segment_id_range(href) == (2, 13)

    def test_a_single_id_href_returns_a_one_wide_range(self):
        href = "TESTD01.A.words.xml#id(TESTD01.A.words5)"
        assert _segment_id_range(href) == (5, 5)

    def test_an_unparseable_href_returns_none(self):
        assert _segment_id_range("not a real href") is None


class TestJoinWords:
    def test_punctuation_attaches_with_no_leading_space(self):
        from app.scripts.corpus_injection.adapters.ami import _TimedElement

        elements = [
            _TimedElement("w", "Okay", 0.0, 0.5),
            _TimedElement("w", ".", 0.5, 0.5),
        ]
        assert _join_words(elements) == "Okay."

    def test_a_clitic_folds_back_onto_its_host(self):
        from app.scripts.corpus_injection.adapters.ami import _TimedElement

        elements = [
            _TimedElement("w", "I", 2.0, 2.2),
            _TimedElement("w", "'ve", 2.2, 2.5),
            _TimedElement("w", "arrived", 2.5, 3.0),
        ]
        assert _join_words(elements) == "I've arrived"

    def test_non_verbal_markers_contribute_no_text(self):
        from app.scripts.corpus_injection.adapters.ami import _TimedElement

        elements = [_TimedElement("vocalsound", "", 5.0, 5.2)]
        assert _join_words(elements) == ""


class TestReadWordsById:
    def test_keyed_by_numeric_suffix_not_position(self, ami_tree: Path):
        words = _read_words_by_id(
            ami_tree / "ami_public_manual_1.6.2" / "words" / "TESTD01.A.words.xml"
        )
        assert words[0].text == "Okay"
        assert words[2].text == "I"
        assert words[5].tag == "vocalsound"


# ------------------------------------------------------------------ the adapter


class TestMeetingIds:
    def test_excludes_meetings_qmsum_already_covers(self, ami_tree: Path, tmp_path: Path):
        qmsum_root = _qmsum_product_tree(tmp_path, ["TESTQ01"])
        adapter = AMIDistractorAdapter(ami_tree, qmsum_root=qmsum_root)
        assert adapter.meeting_ids() == ["TESTD01"]

    def test_a_meeting_qmsum_does_not_cover_stays_in(self, ami_tree: Path, tmp_path: Path):
        qmsum_root = _qmsum_product_tree(tmp_path, [])
        adapter = AMIDistractorAdapter(ami_tree, qmsum_root=qmsum_root)
        assert set(adapter.meeting_ids()) == {"TESTD01", "TESTQ01"}

    def test_missing_qmsum_product_tree_raises_rather_than_silently_including_everything(
        self, ami_tree: Path, tmp_path: Path
    ):
        empty_qmsum = tmp_path / "no-qmsum-here"
        empty_qmsum.mkdir()
        adapter = AMIDistractorAdapter(ami_tree, qmsum_root=empty_qmsum)
        with pytest.raises(FileNotFoundError, match="Product"):
            adapter.meeting_ids()


class TestLoad:
    @pytest.fixture
    def adapter(self, ami_tree: Path, tmp_path: Path) -> AMIDistractorAdapter:
        qmsum_root = _qmsum_product_tree(tmp_path, ["TESTQ01"])
        return AMIDistractorAdapter(ami_tree, qmsum_root=qmsum_root)

    def test_channels_come_from_the_directory_listing_not_meetings_xml(self, adapter):
        """TESTD01.C has no `<speaker>` entry in meetings.xml at all — channel PRESENCE
        must still be discovered (the real IN1001 shape: meetings.xml lists 3, disk has 4)."""
        doc = adapter.load("TESTD01")
        assert doc.extra["channels"] == 3

    def test_turns_are_merged_across_channels_in_chronological_order(self, adapter):
        doc = adapter.load("TESTD01")
        starts = [t.start for t in doc.turns]
        assert starts == sorted(starts)
        # A@0.0 "Okay.", B@1.0 "Right", A@2.0 "I've arrived", C@6.0 "Sure" — the vocalsound
        # segment (A@5.0) must be ABSENT, not present with empty text.
        assert [t.text for t in doc.turns] == ["Okay.", "Right", "I've arrived", "Sure"]

    def test_turn_index_is_the_merged_chronological_position(self, adapter):
        doc = adapter.load("TESTD01")
        assert [t.turn_index for t in doc.turns] == list(range(len(doc.turns)))

    def test_speaker_label_uses_the_role_when_meetings_xml_has_one(self, adapter):
        doc = adapter.load("TESTD01")
        okay_turn = next(t for t in doc.turns if t.text == "Okay.")
        assert okay_turn.speaker == "Project Manager"

    def test_speaker_label_falls_back_to_participant_letter_without_a_role(self, adapter):
        doc = adapter.load("TESTD01")
        right_turn = next(t for t in doc.turns if t.text == "Right")
        assert right_turn.speaker == "Participant B"

    def test_a_channel_absent_from_meetings_xml_also_falls_back(self, adapter):
        doc = adapter.load("TESTD01")
        sure_turn = next(t for t in doc.turns if t.text == "Sure")
        assert sure_turn.speaker == "Participant C"

    def test_timing_is_real_and_fully_aligned_by_construction(self, adapter):
        """Unlike qmsum.py's diff-based recovery, every AMI-native turn is 100% timed —
        there is no reference to fall short of aligning against."""
        doc = adapter.load("TESTD01")
        assert doc.timing.source == TIMING_REAL
        assert doc.timing.aligned_turns == doc.timing.total_turns == len(doc.turns)
        assert doc.timing.reference == "ami:TESTD01"

    def test_word_level_timings_are_populated_from_real_w_elements(self, adapter):
        doc = adapter.load("TESTD01")
        arrived_turn = next(t for t in doc.turns if t.text == "I've arrived")
        assert arrived_turn.words is not None
        surfaces = [w.word for w in arrived_turn.words]
        assert surfaces == ["I", "'ve", "arrived"]

    def test_extra_marks_the_meeting_as_a_distractor_not_a_judged_query_source(self, adapter):
        doc = adapter.load("TESTD01")
        assert doc.extra["role"] == "distractor"
        assert doc.extra["license_tier"] == "A"

    def test_describe_reports_tier_a_and_the_version_from_the_extracted_dirname(self, adapter):
        info = adapter.describe()
        assert info.license_tier == "A"
        assert info.version == "1.6.2"
        assert info.key == "ami"


class TestRegistry:
    def test_ami_is_discoverable_by_key(self, ami_tree: Path, tmp_path: Path):
        _qmsum_product_tree(tmp_path, ["TESTQ01"])
        adapter = build_adapter("ami", data_dir=tmp_path)
        assert isinstance(adapter, AMIDistractorAdapter)
        assert adapter.meeting_ids() == ["TESTD01"]

    def test_missing_root_is_reported_with_the_path(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="ami"):
            build_adapter("ami", tmp_path)


# ------------------------------------------------------ real-corpus check


@pytest.mark.skipif(
    not (NAS_ROOT / "qmsum").is_dir() or not (NAS_ROOT / "ami").is_dir(),
    reason=(
        "QMSum + AMI not present under $RAG_EVAL_DATA_DIR. The fixture tests above prove "
        "the algorithm; this one proves the measured distractor count really is 34. Fetch "
        "with scripts/fetch-rag-eval-data.sh --accept-licenses."
    ),
)
class TestAgainstTheRealCorpus:
    """The number the A5 brief was built on, re-measured — not trusted from the brief."""

    @pytest.fixture(scope="class")
    def adapter(self) -> AMIDistractorAdapter:
        return AMIDistractorAdapter(NAS_ROOT / "ami", qmsum_root=NAS_ROOT / "qmsum")

    def test_distractor_count_and_prefix_distribution(self, adapter):
        from collections import Counter

        ids = adapter.meeting_ids()
        assert len(ids) == 34
        assert Counter(m[:2] for m in ids) == {"EN": 16, "IN": 10, "IB": 7, "TS": 1}

    def test_no_distractor_overlaps_qmsums_gold_set(self, adapter):
        assert set(adapter.meeting_ids()).isdisjoint(adapter._qmsum_product_ids)

    def test_a_sample_meeting_loads_with_real_full_timing(self, adapter):
        doc = adapter.load("EN2001a")
        assert doc.timing.source == TIMING_REAL
        assert doc.timing.aligned_turns == doc.timing.total_turns
        assert len(doc.turns) > 0
        assert all(t.start is not None and t.end is not None for t in doc.turns)
