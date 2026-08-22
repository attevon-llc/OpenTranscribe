"""ELITR-Bench adapter (#521): turn parsing, the licence-tier claim, and the real corpus.

The parsing rules under test are the corpus's own conventions (see the adapter's module
docstring): a ``(PERSONn)`` marker opens a turn, an unmarked line continues the current
turn, and content before the first marker — real in 5 of the 18 files — becomes an
``"Unknown"`` turn rather than being dropped or attributed to an invented person.

The real-corpus tier is gated on the NAS staging directory existing, the same shape the
other adapter suites use — it skips on a machine without the benchmarks mount rather
than failing, and its numbers (18 meetings, 271 questions) are measured claims about
the staged copy, not aspirations.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.scripts.corpus_injection.adapters import build_adapter
from app.scripts.corpus_injection.adapters.elitr_bench import ElitrBenchAdapter

NAS_ROOT = Path(os.environ.get("RAG_EVAL_DATA_DIR", "/mnt/nas/opentranscribe-benchmarks"))
_REAL_ROOT = NAS_ROOT / "elitr-bench"

# ---------------------------------------------------------------- fixtures


def _qa_json(split: str, meetings: dict[str, int]) -> str:
    return json.dumps(
        {
            "split": split,
            "meetings": [
                {
                    "id": meeting_id,
                    "questions": [
                        {
                            "id": str(i),
                            "question-type": "who",
                            "answer-position": "S",
                            "question": f"q{i}?",
                            "groundtruth-answer": f"a{i}",
                        }
                        for i in range(count)
                    ],
                }
                for meeting_id, count in meetings.items()
            ],
        }
    )


@pytest.fixture
def elitr_tree(tmp_path: Path) -> Path:
    root = tmp_path / "elitr-bench"
    (root / "transcripts").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "transcripts" / "meeting_en_dev_001.txt").write_text(
        "(PERSON6) So we are expecting [PERSON10] today?\n"
        "(PERSON3) Yes, he has just written an email.\n"
        "And so he is joining in half a minute.\n"
        "\n"
        "(PERSON6) Okay.\n"
    )
    (root / "transcripts" / "meeting_en_test_014.txt").write_text(
        "Some untranscribed preamble line.\n"
        "A second unattributed line.\n"
        "(PERSON1) First attributed words.\n"
    )
    (root / "data" / "elitr-bench-qa_dev.json").write_text(
        _qa_json("dev", {"meeting_en_dev_001": 3})
    )
    (root / "data" / "elitr-bench-qa_test2.json").write_text(
        _qa_json("test2", {"meeting_en_test_014": 2})
    )
    return root


# ---------------------------------------------------------------- parsing


class TestTurnParsing:
    def test_marker_lines_open_turns_and_bare_lines_continue_them(self, elitr_tree: Path):
        doc = ElitrBenchAdapter(elitr_tree).load("meeting_en_dev_001")
        assert [t.speaker for t in doc.turns] == ["PERSON6", "PERSON3", "PERSON6"]
        # The continuation line belongs to PERSON3's turn, joined with a space.
        assert doc.turns[1].text == (
            "Yes, he has just written an email. And so he is joining in half a minute."
        )

    def test_turn_indices_are_sequential(self, elitr_tree: Path):
        doc = ElitrBenchAdapter(elitr_tree).load("meeting_en_dev_001")
        assert [t.turn_index for t in doc.turns] == [0, 1, 2]

    def test_content_before_the_first_marker_becomes_an_unknown_turn(self, elitr_tree: Path):
        """5 of the 18 real files open with unmarked lines. Dropping them would shrink
        the retrieval haystack; attributing them to PERSON1 would invent attribution
        the file never made. They become one 'Unknown' turn, first."""
        doc = ElitrBenchAdapter(elitr_tree).load("meeting_en_test_014")
        assert doc.turns[0].speaker == "Unknown"
        assert doc.turns[0].text == "Some untranscribed preamble line. A second unattributed line."
        assert doc.turns[1].speaker == "PERSON1"

    def test_timings_are_left_unresolved(self, elitr_tree: Path):
        """The corpus has no timestamps anywhere; the adapter must not invent any
        (timings.resolve_timings stamps synthetic provenance, once, centrally)."""
        doc = ElitrBenchAdapter(elitr_tree).load("meeting_en_dev_001")
        assert all(t.start is None and t.end is None for t in doc.turns)
        assert not doc.timing.is_real

    def test_question_count_is_attached_from_the_matching_split(self, elitr_tree: Path):
        adapter = ElitrBenchAdapter(elitr_tree)
        assert adapter.load("meeting_en_dev_001").extra["question_count"] == 3
        assert adapter.load("meeting_en_test_014").extra["question_count"] == 2


class TestDescribeAndRegistry:
    def test_licence_tier_is_b_because_of_the_transcripts(self, elitr_tree: Path):
        """The QA layer is CC-BY-4.0 but the transcripts are ELITR-minuting
        (CC BY-NC-SA) — the corpus inherits the stricter term. A future edit
        'correcting' this to tier A is the licence mistake #521 documents."""
        info = ElitrBenchAdapter(elitr_tree).describe()
        assert info.license_tier == "B"
        assert info.key == "elitr-bench"

    def test_registry_builds_the_adapter_from_the_data_dir(self, tmp_path: Path, elitr_tree):
        adapter = build_adapter("elitr-bench", data_dir=elitr_tree.parent, root=elitr_tree)
        assert isinstance(adapter, ElitrBenchAdapter)

    def test_meeting_ids_are_sorted_transcript_stems(self, elitr_tree: Path):
        assert ElitrBenchAdapter(elitr_tree).meeting_ids() == [
            "meeting_en_dev_001",
            "meeting_en_test_014",
        ]


# ---------------------------------------------------------- real corpus (NAS)

pytestmark_real = pytest.mark.skipif(
    not _REAL_ROOT.is_dir(),
    reason=f"staged ELITR-Bench not found at {_REAL_ROOT} (see issue #521 staging steps)",
)


class TestRealStagedCorpus:
    @pytestmark_real
    def test_eighteen_meetings_and_271_questions(self):
        adapter = ElitrBenchAdapter(_REAL_ROOT)
        ids = adapter.meeting_ids()
        assert len(ids) == 18
        assert sum(len(v) for v in adapter._qa_by_meeting.values()) == 271

    @pytestmark_real
    def test_seventeen_meetings_have_person_speakers_and_dev_006_has_none(self):
        """Measured on the staged copy: 17 of 18 files carry ``(PERSONn)`` markers;
        ``meeting_en_dev_006``'s ``transcript_MAN2`` source variant is entirely
        unattributed and parses as one 'Unknown' speaker. If this test starts
        failing on the count, the staged copy changed — re-measure, don't relax."""
        adapter = ElitrBenchAdapter(_REAL_ROOT)
        unattributed = []
        for meeting_id in adapter.meeting_ids():
            doc = adapter.load(meeting_id)
            assert doc.turns, meeting_id
            named = {s for s in doc.speakers if s != "Unknown"}
            if not named:
                unattributed.append(meeting_id)
            else:
                assert all(s.startswith("PERSON") for s in named), (
                    meeting_id,
                    sorted(named)[:5],
                )
        assert unattributed == ["meeting_en_dev_006"]

    @pytestmark_real
    def test_word_counts_match_the_staged_copy(self):
        """Floor/ceiling measured at staging time (min 2,601 / max 10,344). A parse
        that silently dropped continuation lines would land far under the floor."""
        adapter = ElitrBenchAdapter(_REAL_ROOT)
        counts = [adapter.load(m).word_count for m in adapter.meeting_ids()]
        assert min(counts) > 2_000
        assert max(counts) < 12_000
