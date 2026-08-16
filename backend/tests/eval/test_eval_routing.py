"""Guarding the routing instrument (#403 Stage 4, unit 1).

The router has its own tests; these are about the thing that *scores* it. An
instrument that cannot report a failure is worse than no instrument, so the
central test here is a **must-fire** one: feed the scorer a case set whose
expectations are deliberately wrong and require it to produce non-zero leakage.
Without that, every "0% leakage" result in this epic would be unfalsifiable.

Nothing here reaches a stack, an index or a model. Routing is a pure function of
the query string, which is exactly why it can be gated in the fast unit suite.
"""

from __future__ import annotations

import pytest

from tests.eval.harness.routing import BY_CONSTRUCTION
from tests.eval.harness.routing import EXPECTED_INTENT_BY_CLASS
from tests.eval.harness.routing import SURFACE_RULE
from tests.eval.harness.routing import UNGROUNDED_LABELS
from tests.eval.harness.routing import RoutingCase
from tests.eval.harness.routing import build_routing_report
from tests.eval.harness.routing import evaluate_routing
from tests.eval.harness.routing import render_routing_table

pytestmark = pytest.mark.unit


def _case(query_id: str, text: str, expected: str, **kwargs) -> RoutingCase:
    return RoutingCase(
        query_id=query_id,
        text=text,
        expected=expected,
        query_class=kwargs.pop("query_class", expected),
        corpus=kwargs.pop("corpus", "synthetic"),
        label_provenance=kwargs.pop("label_provenance", BY_CONSTRUCTION),
        **kwargs,
    )


CORRECTLY_LABELLED = [
    _case("a-1", "How many meetings discussed the compliance audit?", "aggregate"),
    _case("a-2", "Which meetings mention the Slate Viaduct exercise? List them.", "aggregate"),
    _case("l-1", "What was the supplier we selected?", "lookup"),
    _case("l-2", "Which meeting recorded 10,000 requests per second?", "lookup"),
    _case("s-1", "Summarise what the architecture forum covered.", "summarize"),
]


def test_an_empty_case_set_is_refused():
    """An empty confusion matrix reports 100% accuracy on every label."""
    with pytest.raises(ValueError, match="empty case set"):
        evaluate_routing([])


def test_a_correctly_labelled_set_scores_clean_and_is_fully_covered():
    result = evaluate_routing(CORRECTLY_LABELLED)
    assert result.case_count == len(CORRECTLY_LABELLED)
    assert len(result.predicted) == len(CORRECTLY_LABELLED)
    assert result.accuracy() == 1.0
    assert result.leakage("lookup")["misrouted"] == 0


def test_the_scorer_reports_a_failure_when_there_is_one():
    """MUST-FIRE. The guard that makes every clean result mean something.

    Two aggregation questions are labelled ``lookup``. The router will route
    them to ``aggregate`` — correctly — and the scorer must call that leakage,
    because from its point of view the expectation is what it was given.
    """
    mislabelled = [
        _case("x-1", "How many meetings discussed the compliance audit?", "lookup"),
        _case("x-2", "Which meetings mention the audit? List them.", "lookup"),
        _case("l-1", "What was the supplier we selected?", "lookup"),
    ]
    result = evaluate_routing(mislabelled)
    leakage = result.leakage("lookup")

    assert leakage["n"] == 3
    assert leakage["misrouted"] == 2
    assert leakage["to"] == {"aggregate": 2}
    assert leakage["rate"] == pytest.approx(2 / 3)
    assert result.accuracy() == pytest.approx(1 / 3)


def test_accuracy_can_be_read_per_expected_label():
    result = evaluate_routing(
        [
            *CORRECTLY_LABELLED,
            _case("x-1", "What was the peak throughput we measured?", "aggregate"),
        ]
    )
    assert result.accuracy("lookup") == 1.0
    assert result.accuracy("aggregate") == pytest.approx(2 / 3)
    assert result.accuracy("summarize") == 1.0


def test_accuracy_of_a_label_with_no_cases_is_zero_not_one():
    """An absent label must not read as a perfect score."""
    result = evaluate_routing(CORRECTLY_LABELLED)
    assert result.accuracy("temporal") == 0.0


def test_the_temporal_slot_is_only_scored_where_a_case_claims_one():
    cases = [
        _case(
            "t-1",
            "How many meetings in March 2025 discussed the audit?",
            "aggregate",
            expected_temporal=(2025, 3),
        ),
        _case("t-2", "How many meetings discussed the audit?", "aggregate"),
    ]
    result = evaluate_routing(cases)
    assert result.temporal_recovered == {"t-1": True}


def test_a_wrong_temporal_slot_is_recorded_as_not_recovered():
    cases = [
        _case(
            "t-1",
            "How many meetings in March 2025 discussed the audit?",
            "aggregate",
            expected_temporal=(2025, 7),
        )
    ]
    result = evaluate_routing(cases)
    assert result.temporal_recovered == {"t-1": False}


def test_results_do_not_depend_on_case_ordering():
    forward = evaluate_routing(CORRECTLY_LABELLED)
    reverse = evaluate_routing(list(reversed(CORRECTLY_LABELLED)))
    assert forward.predicted == reverse.predicted
    assert {k: dict(v) for k, v in forward.confusion.items()} == {
        k: dict(v) for k, v in reverse.confusion.items()
    }


def test_the_report_carries_its_own_caveats_and_provenance():
    cases = [
        *CORRECTLY_LABELLED,
        _case(
            "q-1",
            "Summarize the discussion about the remote.",
            "summarize",
            corpus="qmsum",
            label_provenance=SURFACE_RULE,
        ),
    ]
    report = build_routing_report(cases, evaluate_routing(cases))

    assert report["llm_required"] is False
    assert report["cases"] == len(cases)
    assert report["label_provenance"] == {BY_CONSTRUCTION: 5, SURFACE_RULE: 1}
    assert set(report["cases_by_corpus"]) == {"qmsum", "synthetic"}
    assert report["ungrounded_labels"] == list(UNGROUNDED_LABELS)
    assert report["caveats"], "a number without its caveats is how a tautology gets quoted"
    assert "why_it_matters" in report["lookup_leakage"]


def test_every_scoreable_query_class_maps_to_a_router_label():
    """The four #403 query classes and the four router labels must connect."""
    from app.services.chat.router import INTENTS
    from tests.eval.harness.corpora import CLASSES

    assert set(EXPECTED_INTENT_BY_CLASS) == set(CLASSES)
    unmapped = sorted(set(EXPECTED_INTENT_BY_CLASS.values()) - set(INTENTS))
    assert unmapped == [], f"expectation names a label the router cannot produce: {unmapped}"


def test_the_rendered_table_has_a_row_per_expected_label():
    table = render_routing_table(evaluate_routing(CORRECTLY_LABELLED))
    lines = [line for line in table.splitlines() if line.startswith("|")]
    assert lines[0].startswith("| expected |")
    assert len(lines) == 2 + len({case.expected for case in CORRECTLY_LABELLED})
    assert "| lookup | 2 | 2 | 1.0000 | — |" in lines
