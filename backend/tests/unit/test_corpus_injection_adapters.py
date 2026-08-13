# mypy: disable-error-code="operator,index,type-var,arg-type,return-value"
# These suites assert directly on ``Turn.start``/``Turn.end`` (typed
# ``float | None`` because an adapter leaves them unset until timings are
# resolved) and on ``TimingInfo.params`` (``dict | None``). Every such
# assertion runs *after* the call that populates the field — that is the
# thing being tested — so narrowing each one would bury the assertion in
# ``assert x is not None`` noise. Declared once here rather than widening a
# production signature to suit a test.
"""Corpus adapters and the QMSum -> AMI/ICSI timing alignment (issue #403).

The alignment is the part most likely to be silently wrong. QMSum redistributes
AMI and ICSI transcripts with the timings stripped, and the obvious way to put
them back — zip QMSum's turn list against the reference's segment list — is
**wrong**: ES2004a has 320 QMSum turns against 283 AMI segments, IS1003b has 407
against 454. Index alignment would assign every turn after the first divergence
a timestamp from an unrelated part of the meeting, and no assertion downstream
would notice.

So every alignment test here carries an explicit **index-alignment negative
control**: it computes what the naive approach would have produced and asserts
the real answer differs. A test that only checked "times are plausible" would
pass under the bug it exists to catch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.scripts.corpus_injection.adapters import build_adapter
from app.scripts.corpus_injection.adapters.generic_json import GenericJsonAdapter
from app.scripts.corpus_injection.adapters.qmsum import QMSumAdapter
from app.scripts.corpus_injection.model import TIMING_REAL
from app.scripts.corpus_injection.model import TIMING_SYNTHETIC
from app.scripts.corpus_injection.nxt import align_turns_to_channels
from app.scripts.corpus_injection.nxt import read_channel_words
from app.scripts.corpus_injection.timings import resolve_timings

NAS_ROOT = Path(os.environ.get("RAG_EVAL_DATA_DIR", "/mnt/nas/opentranscribe-benchmarks"))

# ---------------------------------------------------------------- fixtures


def _words_xml(meeting: str, channel: str, words: list[tuple[str, float, float]]) -> str:
    body = "\n".join(
        f'   <w nite:id="{meeting}.{channel}.words{i}" starttime="{s}" endtime="{e}">{w}</w>'
        for i, (w, s, e) in enumerate(words)
    )
    return (
        '<?xml version="1.0" encoding="ISO-8859-1" standalone="yes"?>\n'
        f'<nite:root nite:id="{meeting}.{channel}.words" '
        'xmlns:nite="http://nite.sourceforge.net/">\n'
        f"{body}\n</nite:root>\n"
    )


@pytest.fixture
def ami_tree(tmp_path: Path) -> Path:
    """A minimal AMI bundle: two timed channels plus the role->channel table."""
    root = tmp_path / "ami" / "ami_public_manual_1.6.2"
    (root / "words").mkdir(parents=True)
    (root / "corpusResources").mkdir(parents=True)
    (root / "corpusResources" / "meetings.xml").write_text(
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        '<corpus xmlns:nite="http://nite.sourceforge.net/">\n'
        '  <meeting nite:id="m1" observation="TEST001">\n'
        '    <speaker nite:id="s1" nxt_agent="A" role="PM" />\n'
        '    <speaker nite:id="s2" nxt_agent="B" role="ID" />\n'
        "  </meeting>\n</corpus>\n",
        encoding="utf-8",
    )
    (root / "words" / "TEST001.A.words.xml").write_text(
        _words_xml(
            "TEST001",
            "A",
            [
                ("Hello", 0.0, 0.5),
                ("world", 0.5, 1.0),
                ("This", 4.0, 4.3),
                ("is", 4.3, 4.5),
                ("me", 4.5, 5.0),
            ],
        ),
        encoding="ISO-8859-1",
    )
    (root / "words" / "TEST001.B.words.xml").write_text(
        _words_xml("TEST001", "B", [("Okay", 2.0, 2.4), ("sure", 2.4, 3.0)]),
        encoding="ISO-8859-1",
    )
    return tmp_path / "ami"


@pytest.fixture
def icsi_tree(tmp_path: Path) -> Path:
    root = tmp_path / "icsi" / "ICSI_plus_NXT" / "ICSIplus" / "Words"
    root.mkdir(parents=True)
    (root / "Bxx001.C.words.xml").write_text(
        _words_xml("Bxx001", "C", [("Right", 1.0, 1.4), ("okay", 1.4, 1.8)]),
        encoding="ISO-8859-1",
    )
    (root / "Bxx001.D.words.xml").write_text(
        _words_xml("Bxx001", "D", [("Mm", 3.0, 3.2), ("hmm", 3.2, 3.5)]),
        encoding="ISO-8859-1",
    )
    return tmp_path / "icsi"


def _qmsum_tree(tmp_path: Path, domain: str, meeting_id: str, turns: list[dict]) -> Path:
    root = tmp_path / "qmsum" / "QMSum-83d7768c1f2b4dfeb091385d3dc7e239b8e5bb7e"
    target = root / "data" / domain / "all"
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{meeting_id}.json").write_text(
        json.dumps(
            {
                "meeting_transcripts": turns,
                "specific_query_list": [{"query": "q", "relevant_text_span": [["0", "1"]]}],
                "general_query_list": [],
                "topic_list": [],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path / "qmsum"


# ------------------------------------------------------------ NXT parsing


class TestNxtWordReader:
    def test_reads_timed_tokens_in_order(self, ami_tree: Path):
        tokens = read_channel_words(
            ami_tree / "ami_public_manual_1.6.2" / "words" / "TEST001.A.words.xml"
        )
        assert [t.token for t in tokens] == ["hello", "world", "this", "is", "me"]
        assert (tokens[0].start, tokens[0].end) == (0.0, 0.5)

    def test_punctuation_and_untimed_nodes_are_skipped(self, tmp_path: Path):
        path = tmp_path / "X.A.words.xml"
        path.write_text(
            '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
            '<nite:root nite:id="X.A.words" xmlns:nite="http://nite.sourceforge.net/">\n'
            '  <vocalsound nite:id="X.v.1" starttime="" endtime="" description="laugh"/>\n'
            '  <w nite:id="X.A.words0" starttime="1.0" endtime="1.2">Yes</w>\n'
            '  <w nite:id="X.A.words1" starttime="1.2" endtime="1.2" punc="true">.</w>\n'
            "</nite:root>\n",
            encoding="ISO-8859-1",
        )
        assert [t.token for t in read_channel_words(path)] == ["yes"]

    def test_clitics_are_folded_back_onto_their_host(self, tmp_path: Path):
        """NXT splits ``I've``; QMSum keeps it whole. Unfolded, the diff drifts."""
        path = tmp_path / "X.A.words.xml"
        path.write_text(
            _words_xml("X", "A", [("I", 1.0, 1.1), ("'ve", 1.1, 1.3), ("gone", 1.3, 1.6)]),
            encoding="ISO-8859-1",
        )
        tokens = read_channel_words(path)
        assert [t.token for t in tokens] == ["i've", "gone"]
        assert (tokens[0].start, tokens[0].end) == (1.0, 1.3)


# ------------------------------------------------- the non-1:1 alignment


class TestNonOneToOneAlignment:
    """The case that makes index alignment wrong, built explicitly.

    Three QMSum turns over two channels, where turn boundaries deliberately do
    not follow the reference's natural utterance boundaries: turn 0 merges two
    A-channel utterances separated by a 3 s pause AND leaves that utterance's
    last word to turn 2.
    """

    @pytest.fixture
    def aligned(self, ami_tree: Path, tmp_path: Path):
        turns = [
            {"speaker": "Project Manager", "content": "Hello world . {disfmarker} This is"},
            {"speaker": "Industrial Designer", "content": "Okay , sure ."},
            {"speaker": "Project Manager", "content": "me ."},
        ]
        qmsum_root = _qmsum_tree(tmp_path, "Product", "TEST001", turns)
        adapter = QMSumAdapter(qmsum_root, ami_root=ami_tree)
        return adapter.load("TEST001")

    def test_every_turn_gets_the_span_of_its_own_words(self, aligned):
        spans = [(t.start, t.end) for t in aligned.turns]
        assert spans == [(0.0, 4.5), (2.0, 3.0), (4.5, 5.0)]

    def test_index_alignment_would_have_produced_different_answers(self, aligned):
        """The negative control.

        Reference utterances in start order are A[0.0-1.0], B[2.0-3.0],
        A[4.0-5.0]. Zipping those onto turns 0/1/2 gives turn 0 -> (0.0, 1.0)
        and turn 2 -> (4.0, 5.0) — both wrong, and both plausible enough that a
        "times look sane" assertion would accept them.
        """
        naive = [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0)]
        actual = [(t.start, t.end) for t in aligned.turns]
        assert actual != naive
        assert actual[0] != naive[0]
        assert actual[2] != naive[2]

    def test_aligned_turns_overlap_in_time(self, aligned):
        """Turn 1 begins inside turn 0's span — real overlapping speech.

        Index alignment cannot produce this at all: it assigns each turn a
        disjoint reference segment, so overlap disappears and the transcript
        reads as strictly sequential.
        """
        turn0, turn1 = aligned.turns[0], aligned.turns[1]
        assert turn0.start < turn1.start < turn0.end

    def test_word_timings_are_carried_through(self, aligned):
        first = aligned.turns[0]
        assert [w.word for w in first.words] == ["Hello", "world", "This", "is"]
        assert first.words[0].start == 0.0

    def test_provenance_is_real_and_names_the_reference(self, aligned):
        assert aligned.timing.source == TIMING_REAL
        assert aligned.timing.reference == "ami:TEST001"
        assert aligned.extra["token_match_rate"] == 1.0

    def test_disfluency_markers_do_not_consume_reference_words(self, aligned):
        """``{disfmarker}`` is QMSum annotation, not speech.

        Left in, it would fail to match and drag the diff out of step.
        """
        assert aligned.turns[0].end == 4.5


class TestChannelResolution:
    def test_ami_roles_map_to_nxt_agents(self, ami_tree: Path, tmp_path: Path):
        turns = [
            {"speaker": "Project Manager", "content": "Hello world"},
            {"speaker": "Industrial Designer", "content": "Okay sure"},
        ]
        doc = QMSumAdapter(
            _qmsum_tree(tmp_path, "Product", "TEST001", turns), ami_root=ami_tree
        ).load("TEST001")
        assert doc.turns[0].start == 0.0  # channel A
        assert doc.turns[1].start == 2.0  # channel B

    def test_icsi_channel_comes_from_the_speaker_labels_trailing_letter(
        self, icsi_tree: Path, tmp_path: Path
    ):
        turns = [
            {"speaker": "Grad C", "content": "Right okay"},
            {"speaker": "PhD D", "content": "Mm hmm"},
        ]
        doc = QMSumAdapter(
            _qmsum_tree(tmp_path, "Academic", "Bxx001", turns), icsi_root=icsi_tree
        ).load("Bxx001")
        assert (doc.turns[0].start, doc.turns[0].end) == (1.0, 1.8)
        assert (doc.turns[1].start, doc.turns[1].end) == (3.0, 3.5)

    def test_committee_has_no_timed_source_and_stays_untimed(self, tmp_path: Path):
        turns = [{"speaker": "The Chair", "content": "Order , please ."}]
        doc = QMSumAdapter(_qmsum_tree(tmp_path, "Committee", "covid_9", turns)).load("covid_9")
        assert doc.timing.source == TIMING_SYNTHETIC
        assert doc.turns[0].start is None

    def test_a_missing_reference_bundle_degrades_to_synthetic_not_an_error(self, tmp_path: Path):
        turns = [{"speaker": "Project Manager", "content": "Hello world"}]
        doc = QMSumAdapter(_qmsum_tree(tmp_path, "Product", "TEST001", turns)).load("TEST001")
        resolve_timings(doc)
        assert doc.timing.source == TIMING_SYNTHETIC


class TestQMSumAdapterMetadata:
    def test_version_is_the_pinned_upstream_commit(self, tmp_path: Path):
        root = _qmsum_tree(tmp_path, "Product", "TEST001", [{"speaker": "A", "content": "x"}])
        info = QMSumAdapter(root).describe()
        assert info.version == "83d7768c1f2b4dfeb091385d3dc7e239b8e5bb7e"
        assert info.license_tier == "A"

    def test_meeting_ids_are_sorted_across_domains(self, tmp_path: Path):
        _qmsum_tree(tmp_path, "Product", "ES2004a", [{"speaker": "A", "content": "x"}])
        root = _qmsum_tree(tmp_path, "Academic", "Bdb001", [{"speaker": "Grad C", "content": "y"}])
        assert QMSumAdapter(root).meeting_ids() == ["Bdb001", "ES2004a"]

    def test_query_counts_are_carried_into_the_manifest_extras(self, tmp_path: Path):
        root = _qmsum_tree(tmp_path, "Product", "TEST001", [{"speaker": "A", "content": "x"}])
        assert QMSumAdapter(root).load("TEST001").extra["specific_query_count"] == 1

    def test_turn_index_is_preserved_verbatim(self, tmp_path: Path):
        """Gold relevance spans address turns by index — this is load-bearing."""
        turns = [{"speaker": "A", "content": f"turn {i}"} for i in range(5)]
        root = _qmsum_tree(tmp_path, "Product", "TEST001", turns)
        doc = QMSumAdapter(root).load("TEST001")
        assert [t.turn_index for t in doc.turns] == [0, 1, 2, 3, 4]


class TestGenericJsonAdapter:
    def _write(self, tmp_path: Path, records: list[dict]) -> Path:
        root = tmp_path / "synthetic"
        root.mkdir()
        (root / "meetings.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records), encoding="utf-8"
        )
        return root

    def test_reads_jsonl(self, tmp_path: Path):
        root = self._write(
            tmp_path,
            [{"meeting_id": "s1", "turns": [{"speaker": "A", "text": "hi", "start": 0, "end": 1}]}],
        )
        doc = GenericJsonAdapter(root).load("s1")
        assert doc.turns[0].text == "hi"
        assert doc.turns[0].start == 0.0

    def test_reads_one_object_per_file(self, tmp_path: Path):
        root = tmp_path / "synthetic"
        root.mkdir()
        (root / "s2.json").write_text(json.dumps({"turns": [{"speaker": "A", "text": "yo"}]}))
        assert GenericJsonAdapter(root).meeting_ids() == ["s2"]

    def test_generated_timings_are_never_promoted_to_real(self, tmp_path: Path):
        """A generator's timestamps are output, not measurement.

        They are kept (the pacing is deliberate) but the provenance stays
        synthetic, so the guard still refuses to compute a timing metric.
        """
        root = self._write(
            tmp_path,
            [
                {
                    "meeting_id": "s1",
                    "turns": [
                        {"speaker": "A", "text": "hi", "start": 0, "end": 1},
                        {"speaker": "B", "text": "yo", "start": 2, "end": 3},
                    ],
                }
            ],
        )
        doc = GenericJsonAdapter(root).load("s1")
        resolve_timings(doc)
        assert doc.timing.source == TIMING_SYNTHETIC
        assert doc.timing.params["generator"] == "corpus_supplied_v1"
        assert doc.turns[1].start == 2.0

    def test_version_file_is_recorded_when_present(self, tmp_path: Path):
        root = self._write(tmp_path, [{"meeting_id": "s1", "turns": []}])
        (root / "VERSION").write_text("gen-2026-08-12-a\n")
        assert GenericJsonAdapter(root).describe().version == "gen-2026-08-12-a"

    def test_missing_version_still_records_something_identifying(self, tmp_path: Path):
        """A truthy version is not enough — the manifest has to say *what* ran.

        The fallback describes the content, so two corpora of different sizes
        cannot record the same version string. Asserting only truthiness would
        pass on a hardcoded ``"unknown"``.
        """
        root = self._write(tmp_path, [{"meeting_id": "s1", "turns": []}])
        assert GenericJsonAdapter(root).describe().version == "unversioned-1-meetings"

        second = tmp_path / "two"
        second.mkdir()
        bigger = self._write(
            second, [{"meeting_id": "s1", "turns": []}, {"meeting_id": "s2", "turns": []}]
        )
        assert GenericJsonAdapter(bigger).describe().version == "unversioned-2-meetings"


class TestRegistry:
    def test_unknown_corpus_is_rejected_by_name(self, tmp_path: Path):
        with pytest.raises(KeyError, match="nosuchcorpus"):
            build_adapter("nosuchcorpus", tmp_path)

    def test_missing_root_is_reported_with_the_path(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="qmsum"):
            build_adapter("qmsum", tmp_path)


# ------------------------------------------------------ real-corpus check


@pytest.mark.skipif(
    not (NAS_ROOT / "qmsum").is_dir() or not (NAS_ROOT / "ami").is_dir(),
    reason=(
        "QMSum + AMI not present under $RAG_EVAL_DATA_DIR. The fixture tests above "
        "prove the algorithm; this one proves it survives the real corpus. Fetch with "
        "scripts/fetch-rag-eval-data.sh --accept-licenses."
    ),
)
class TestAgainstTheRealCorpus:
    """The fixtures can't prove the real files parse. These do."""

    @pytest.fixture(scope="class")
    def adapter(self) -> QMSumAdapter:
        return QMSumAdapter(
            NAS_ROOT / "qmsum", ami_root=NAS_ROOT / "ami", icsi_root=NAS_ROOT / "icsi"
        )

    def test_es2004a_turn_count_really_does_not_match_ami_segment_count(self, adapter):
        """The measurement this whole design rests on: 320 turns, 283 segments."""
        doc = adapter.load("ES2004a")
        segments = _count_ami_segments("ES2004a")
        assert len(doc.turns) == 320
        assert segments == 283
        assert len(doc.turns) != segments

    def test_is1003b_mismatches_in_the_other_direction(self, adapter):
        """407 turns against 454 segments — index alignment fails both ways."""
        doc = adapter.load("IS1003b")
        assert len(doc.turns) == 407
        assert _count_ami_segments("IS1003b") == 454

    def test_content_alignment_still_recovers_real_times_for_es2004a(self, adapter):
        doc = adapter.load("ES2004a")
        assert doc.extra["token_match_rate"] > 0.95
        resolve_timings(doc)
        assert doc.timing.source == TIMING_REAL
        assert doc.duration == pytest.approx(1049.04, abs=1.0)

    def test_icsi_side_aligns_too(self, adapter):
        doc = adapter.load("Bdb001")
        assert doc.extra["token_match_rate"] > 0.85
        resolve_timings(doc)
        assert doc.timing.source == TIMING_REAL


def _count_ami_segments(meeting_id: str) -> int:
    import xml.etree.ElementTree as ET  # noqa: S405  # nosec B405 — local corpus fixture

    base = NAS_ROOT / "ami" / "ami_public_manual_1.6.2" / "segments"
    total = 0
    for path in sorted(base.glob(f"{meeting_id}.*.segments.xml")):
        root = ET.parse(path).getroot()  # noqa: S314  # nosec B314 — local corpus fixture
        total += sum(1 for el in root if el.tag.endswith("segment"))
    return total


def test_turn_order_and_time_order_can_genuinely_diverge(tmp_path: Path):
    """A later-listed turn can start earlier once real times are attached.

    QMSum lists turns per its own transcript order; the reference clock does not
    have to agree. The segment writer therefore sorts by time (that is the order
    the indexer reads segments in) and keeps ``turn_index`` alongside — this test
    is why both are needed.
    """
    from app.scripts.corpus_injection.model import Turn

    channel_a = tmp_path / "M.A.words.xml"
    channel_b = tmp_path / "M.B.words.xml"
    channel_a.write_text(_words_xml("M", "A", [("late", 9.0, 9.5)]), encoding="ISO-8859-1")
    channel_b.write_text(_words_xml("M", "B", [("early", 1.0, 1.5)]), encoding="ISO-8859-1")

    turns = [Turn(0, "A", "late"), Turn(1, "B", "early")]
    align_turns_to_channels(turns, {"A": channel_a, "B": channel_b})

    assert [t.turn_index for t in turns] == [0, 1]
    assert sorted(range(2), key=lambda i: turns[i].start) == [1, 0]


def test_align_reports_zero_when_no_channel_file_exists(tmp_path: Path):
    """A missing reference must degrade, never raise or silently claim success."""
    from app.scripts.corpus_injection.model import Turn

    turns = [Turn(0, "A", "hello world")]
    timed, matched, total = align_turns_to_channels(turns, {"A": tmp_path / "absent.xml"})
    assert (timed, matched, total) == (0, 0, 0)
    assert turns[0].start is None
