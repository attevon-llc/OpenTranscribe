"""Tests for the #461 W2.E1 speaker-attribution carving in ``harness.corpora``.

``load_qmsum_queries`` is exercised end-to-end against a small fixture shaped exactly
like the real QMSum schema (verified against
``/mnt/nas/opentranscribe-benchmarks/qmsum/.../data/Committee/train/education_21.json``
while this module was written: top-level ``meeting_transcripts`` — a list of
``{speaker, content}`` in turn order — and ``specific_query_list`` — a list of
``{query, answer, relevant_text_span}`` with ``relevant_text_span`` as
``[[start, end]]`` DECIMAL-STRING, INCLUSIVE turn-index pairs). A fixture with the
wrong shape would pass every unit test here while the real loader silently produced
zero SPEAKER_ATTR queries against real data — the exact failure mode this repo's
CLAUDE.md warns "worse than no harness" about for the RECURRENCE prompt shape, and
the same discipline applies here.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.eval.harness.answers import ATTRIBUTION_PROBE_KIND
from tests.eval.harness.answers import SPEAKER
from tests.eval.harness.corpora import ATTRIBUTION_PROBE
from tests.eval.harness.corpora import LOOKUP
from tests.eval.harness.corpora import SPEAKER_ATTR
from tests.eval.harness.corpora import SPEAKER_SUMMARY
from tests.eval.harness.corpora import SUMMARIZE
from tests.eval.harness.corpora import InjectedCorpus
from tests.eval.harness.corpora import _carve_attribution
from tests.eval.harness.corpora import _pick_decoy_speaker
from tests.eval.harness.corpora import _resolve_single_speaker
from tests.eval.harness.corpora import load_qmsum_queries

FILE_UUID = "3f2a9c10-0000-0000-0000-000000000000"


def _transcripts(*speakers: str) -> list[dict]:
    return [{"speaker": speaker, "content": f"turn {i}"} for i, speaker in enumerate(speakers)]


class TestResolveSingleSpeaker:
    def test_single_speaker_span_resolves(self) -> None:
        transcripts = _transcripts("Alice", "Alice", "Bob")
        assert _resolve_single_speaker(transcripts, [["0", "1"]]) == "Alice"

    def test_span_crossing_two_speakers_is_unresolved(self) -> None:
        transcripts = _transcripts("Alice", "Bob")
        assert _resolve_single_speaker(transcripts, [["0", "1"]]) is None

    def test_out_of_range_index_is_unresolved(self) -> None:
        transcripts = _transcripts("Alice")
        assert _resolve_single_speaker(transcripts, [["0", "5"]]) is None

    def test_empty_span_is_unresolved(self) -> None:
        assert _resolve_single_speaker(_transcripts("Alice"), []) is None

    def test_end_before_start_is_unresolved(self) -> None:
        assert _resolve_single_speaker(_transcripts("Alice", "Bob"), [["1", "0"]]) is None

    def test_multiple_spans_all_by_the_same_speaker_resolve(self) -> None:
        transcripts = _transcripts("Alice", "Bob", "Alice", "Alice")
        assert _resolve_single_speaker(transcripts, [["0", "0"], ["2", "3"]]) == "Alice"

    def test_multiple_spans_by_different_speakers_are_unresolved(self) -> None:
        transcripts = _transcripts("Alice", "Bob")
        assert _resolve_single_speaker(transcripts, [["0", "0"], ["1", "1"]]) is None


class TestPickDecoySpeaker:
    def test_picks_the_lexicographically_first_other_speaker(self) -> None:
        transcripts = _transcripts("Zed", "Alice", "Mona")
        assert _pick_decoy_speaker(transcripts, "Zed") == "Alice"

    def test_excludes_the_true_speaker_even_if_alphabetically_first(self) -> None:
        transcripts = _transcripts("Alice", "Bob")
        assert _pick_decoy_speaker(transcripts, "Alice") == "Bob"

    def test_no_other_speaker_returns_none(self) -> None:
        transcripts = _transcripts("Alice", "Alice")
        assert _pick_decoy_speaker(transcripts, "Alice") is None

    def test_is_deterministic_across_calls(self) -> None:
        transcripts = _transcripts("Carol", "Alice", "Bob")
        first = _pick_decoy_speaker(transcripts, "Carol")
        second = _pick_decoy_speaker(transcripts, "Carol")
        assert first == second == "Alice"


class TestCarveAttribution:
    def test_lookup_with_attribution_text_and_resolvable_speaker_becomes_speaker_attr(
        self,
    ) -> None:
        transcripts = _transcripts("Philip Blaker")
        query_class, speaker = _carve_attribution(
            LOOKUP, "According to Philip Blaker, what happened?", transcripts, [["0", "0"]]
        )
        assert query_class == SPEAKER_ATTR
        assert speaker == "Philip Blaker"

    def test_summarize_with_attribution_text_becomes_speaker_summary(self) -> None:
        transcripts = _transcripts("Philip Blaker")
        query_class, speaker = _carve_attribution(
            SUMMARIZE,
            "Summarize the role of Qualification Wales, according to Philip Blaker.",
            transcripts,
            [["0", "0"]],
        )
        assert query_class == SPEAKER_SUMMARY
        assert speaker == "Philip Blaker"

    def test_non_attribution_text_is_unchanged(self) -> None:
        transcripts = _transcripts("Alice")
        query_class, speaker = _carve_attribution(
            LOOKUP, "What was discussed about the budget?", transcripts, [["0", "0"]]
        )
        assert (query_class, speaker) == (LOOKUP, None)

    def test_attribution_text_with_unresolvable_speaker_is_unchanged(self) -> None:
        transcripts = _transcripts("Alice", "Bob")
        query_class, speaker = _carve_attribution(
            LOOKUP, "According to someone, what happened?", transcripts, [["0", "1"]]
        )
        assert (query_class, speaker) == (LOOKUP, None)

    def test_aggregation_class_is_never_reclassified(self) -> None:
        transcripts = _transcripts("Alice")
        query_class, speaker = _carve_attribution(
            "aggregation", "According to Alice, how many?", transcripts, [["0", "0"]]
        )
        assert (query_class, speaker) == ("aggregation", None)


def _write_meeting(root: Path, domain: str, meeting_id: str, payload: dict) -> None:
    directory = root / "data" / domain / "all"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{meeting_id}.json").write_text(json.dumps(payload), encoding="utf-8")


class TestLoadQmsumQueriesCarvesAttribution:
    """End-to-end: a fixture in the REAL QMSum on-disk shape, verified against a real
    NAS file's schema (module docstring)."""

    def _corpus(self, tmp_path: Path) -> InjectedCorpus:
        return InjectedCorpus(
            key="qmsum",
            name="QMSum",
            version="test",
            license_tier="A",
            root=tmp_path,
            file_uuid_by_meeting={"education_21": FILE_UUID},
            extra_by_meeting={"education_21": {"domain": "Committee"}},
        )

    def test_an_attributable_query_yields_speaker_attr_plus_a_probe(self, tmp_path: Path) -> None:
        payload = {
            "meeting_transcripts": [
                {"speaker": "Philip Blaker", "content": "We regulate qualifications."},
                {"speaker": "Philip Blaker", "content": "Standards matter."},
                {"speaker": "Gareth Pierce", "content": "I agree."},
            ],
            "general_query_list": [],
            "specific_query_list": [
                {
                    "query": "According to Philip Blaker, who regulates qualifications?",
                    "answer": "Qualification Wales.",
                    "relevant_text_span": [["0", "1"]],
                }
            ],
        }
        _write_meeting(tmp_path, "Committee", "education_21", payload)
        queries = load_qmsum_queries(self._corpus(tmp_path))

        attr = [q for q in queries if q.query_class == SPEAKER_ATTR]
        probes = [q for q in queries if q.query_class == ATTRIBUTION_PROBE]
        assert len(attr) == 1
        assert attr[0].scored_on == "attribution"
        attr_gold = attr[0].gold_answer
        assert attr_gold is not None
        assert attr_gold.kind == SPEAKER
        assert attr_gold.value == "Philip Blaker"
        # spans are retained even though nothing scores retrieval on this class today.
        assert attr[0].spans

        assert len(probes) == 1
        assert probes[0].scored_on == "attribution_probe"
        probe_gold = probes[0].gold_answer
        assert probe_gold is not None
        assert probe_gold.kind == ATTRIBUTION_PROBE_KIND
        assert probe_gold.value == ("Philip Blaker", "Gareth Pierce")

    def test_a_non_attributable_query_stays_lookup_and_plants_no_probe(
        self, tmp_path: Path
    ) -> None:
        payload = {
            "meeting_transcripts": [
                {"speaker": "Philip Blaker", "content": "We regulate qualifications."},
            ],
            "general_query_list": [],
            "specific_query_list": [
                {
                    "query": "What topics were discussed?",
                    "answer": "Regulation.",
                    "relevant_text_span": [["0", "0"]],
                }
            ],
        }
        _write_meeting(tmp_path, "Committee", "education_21", payload)
        queries = load_qmsum_queries(self._corpus(tmp_path))

        assert len(queries) == 1
        assert queries[0].query_class == LOOKUP
        assert queries[0].scored_on == "retrieval"
        assert queries[0].gold_answer is None

    def test_a_single_speaker_meeting_plants_the_attr_query_but_no_probe(
        self, tmp_path: Path
    ) -> None:
        """No OTHER speaker exists to serve as a decoy — the probe must not be
        fabricated against a speaker who was never in the meeting."""
        payload = {
            "meeting_transcripts": [
                {"speaker": "Philip Blaker", "content": "We regulate qualifications."},
            ],
            "general_query_list": [],
            "specific_query_list": [
                {
                    "query": "According to Philip Blaker, what do they regulate?",
                    "answer": "Qualifications.",
                    "relevant_text_span": [["0", "0"]],
                }
            ],
        }
        _write_meeting(tmp_path, "Committee", "education_21", payload)
        queries = load_qmsum_queries(self._corpus(tmp_path))

        assert len(queries) == 1
        assert queries[0].query_class == SPEAKER_ATTR
        assert not any(q.query_class == ATTRIBUTION_PROBE for q in queries)
