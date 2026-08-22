"""Tests for the #461 W2.E1 additions to ``harness.report``: ``build_scored_rows``,
``render_scored_table``, and ``build_results``'s new ``wave2`` block.
"""

from __future__ import annotations

import pytest

from tests.eval.harness.answers import Answer
from tests.eval.harness.attribution import SubmittedAttribution
from tests.eval.harness.attribution import evaluate_attribution
from tests.eval.harness.corpora import EvalQuery
from tests.eval.harness.report import build_results
from tests.eval.harness.report import build_scored_rows
from tests.eval.harness.report import render_scored_table

ATTR_MEASURES = ("answer_names_gold_speaker", "citation_speaker_match", "answered")


def _attr_query(query_id: str, corpus: str, query_class: str = "speaker_attr") -> EvalQuery:
    return EvalQuery(
        query_id=query_id,
        text="text",
        query_class=query_class,
        corpus=corpus,
        license_tier="A",
        spans=(),
        scored_on="attribution",
        gold_answer=Answer.speaker("Alice"),
    )


def _gold_map(queries: list[EvalQuery]) -> dict[str, Answer]:
    """Every fixture query above sets a real ``gold_answer`` — assert that rather
    than carry the field's ``Answer | None`` type into every call site below."""
    out: dict[str, Answer] = {}
    for query in queries:
        assert query.gold_answer is not None
        out[query.query_id] = query.gold_answer
    return out


class TestBuildScoredRows:
    def test_one_row_per_corpus_and_class(self) -> None:
        queries = [
            _attr_query("q1", "qmsum"),
            _attr_query("q2", "qmsum"),
            _attr_query("q3", "synthetic"),
        ]
        gold = _gold_map(queries)
        submitted = {
            "q1": SubmittedAttribution(speaker="Alice", citation_speakers=("Alice",)),
            "q2": SubmittedAttribution(speaker="Wrong"),
            "q3": SubmittedAttribution(speaker="Alice", citation_speakers=("Alice",)),
        }
        result = evaluate_attribution(gold, submitted)
        rows = build_scored_rows(queries, result, scored_on="attribution", measures=ATTR_MEASURES)

        by_corpus = {row["corpus"]: row for row in rows}
        assert set(by_corpus) == {"qmsum", "synthetic"}
        assert by_corpus["qmsum"]["queries"] == 2
        assert by_corpus["qmsum"]["metrics"]["answer_names_gold_speaker"] == 0.5
        assert by_corpus["synthetic"]["metrics"]["answer_names_gold_speaker"] == 1.0
        assert by_corpus["qmsum"]["scored_on"] == "attribution"

    def test_other_scored_on_values_are_excluded(self) -> None:
        """A retrieval-scored query in the same list must never leak into an
        attribution table — the exact confusion `build_rows`'s own docstring
        warns about for the answer/retrieval split."""
        attr_query = _attr_query("q1", "qmsum")
        retrieval_query = EvalQuery(
            query_id="q2",
            text="text",
            query_class="lookup",
            corpus="qmsum",
            license_tier="A",
            spans=(),
            scored_on="retrieval",
        )
        gold = _gold_map([attr_query])
        submitted = {"q1": SubmittedAttribution(speaker="Alice", citation_speakers=("Alice",))}
        result = evaluate_attribution(gold, submitted)
        rows = build_scored_rows(
            [attr_query, retrieval_query], result, scored_on="attribution", measures=ATTR_MEASURES
        )
        assert len(rows) == 1
        assert rows[0]["queries"] == 1

    def test_no_matching_queries_produces_no_rows(self) -> None:
        queries = [_attr_query("q1", "qmsum", query_class="lookup")]
        gold = _gold_map(queries)
        result = evaluate_attribution(gold, {})
        rows = build_scored_rows(
            queries, result, scored_on="speaker_summary", measures=ATTR_MEASURES
        )
        assert rows == []


class TestRenderScoredTable:
    def test_renders_a_markdown_table_with_every_measure_column(self) -> None:
        rows = [
            {
                "corpus": "qmsum",
                "license_tier": "A",
                "query_class": "speaker_attr",
                "queries": 2,
                "unanswered": 0,
                "metrics": {
                    "answer_names_gold_speaker": 0.5,
                    "citation_speaker_match": 0.25,
                    "answered": 1.0,
                },
            }
        ]
        table = render_scored_table(rows, ATTR_MEASURES)
        assert "answer_names_gold_speaker" in table
        assert "0.5000" in table
        assert "qmsum" in table

    def test_empty_rows_renders_header_only(self) -> None:
        table = render_scored_table([], ATTR_MEASURES)
        lines = table.strip().splitlines()
        assert len(lines) == 2  # header + separator, no data rows


class TestBuildResultsWave2Block:
    @pytest.fixture(autouse=True)
    def _metric_engine(self):
        # build_results records measure provenance (metrics.measure_provenance),
        # which imports the eval-only metric engine at call time. That engine is
        # licence-gated into requirements-eval.txt and never installed in CI —
        # see backend/tests/CLAUDE.md — so these tests skip without it.
        pytest.importorskip("ir_measures")

    def _minimal_kwargs(self) -> dict:
        return {
            "control_name": "test",
            "corpora": [],
            "retrieval": {},
            "policy": {},
            "index_state": {},
            "qrels_stats": {},
            "rows": [],
            "retrieval_per_query": [],
        }

    def test_wave2_defaults_to_an_explicit_not_scored_marker(self) -> None:
        results = build_results(**self._minimal_kwargs())
        assert results["wave2"] == {"scored": 0, "note": "no #461 W2.E1 classes scored in this run"}

    def test_wave2_block_is_carried_through_verbatim_when_provided(self) -> None:
        wave2 = {"attribution_rows": [{"corpus": "qmsum"}], "instrumentation": {"llm_calls": {}}}
        results = build_results(**self._minimal_kwargs(), wave2=wave2)
        assert results["wave2"] == wave2

    def test_existing_answers_default_is_unaffected_by_the_new_field(self) -> None:
        """Regression guard: adding `wave2` must not disturb the pre-existing
        `answers` default-marker behaviour."""
        results = build_results(**self._minimal_kwargs())
        assert results["answers"] == {"scored": 0, "note": "no answer-scored queries in this run"}
