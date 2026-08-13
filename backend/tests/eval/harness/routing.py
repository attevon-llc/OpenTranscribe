"""Scoring the query router as the classifier it is (#403 Stage 4).

The router is not a ranker, so neither :mod:`tests.eval.harness.metrics` (nDCG
over qrels) nor :mod:`tests.eval.harness.answers` (exact match over counts) can
say anything about it. What it produces is a **label**, and the honest instrument
for a label is a confusion matrix over queries whose class is already known.

Three properties of this measurement decide whether it means anything, so they
are computed and reported rather than described:

**Lookup leakage is the number that matters.** Every route keeps the chunk tier,
so a misroute cannot make a query unanswerable — what it costs is the reduced
chunk budget the summarize and aggregate branches run with. That cost lands
*only* on queries that should have been ``lookup`` and were routed elsewhere.
**D5 says the lookup class must never regress**, and this is the quantity that
would cause it, measured directly on 1,172 real human questions rather than
inferred from a retrieval delta afterwards.

**Not every label has honest ground truth, and the ones that do not say so.**
QMSum's ``summarize`` class is assigned by a surface rule — the query text starts
with "summarize"/"summarise"/"describe" — which overlaps the router's own
lexicon. Agreement there is therefore *partly tautological* and is reported with
``label_provenance: surface-rule`` so nobody quotes it as an independent result.
The synthetic tier's labels are true **by construction** (the generator built the
query from the rule), and QMSum's ``lookup`` label is the complement of a surface
rule over human-written questions, which makes leakage out of it a real
measurement. The ``temporal`` label has **no** ground truth in either corpus and
is exercised only by unit tests; :data:`UNGROUNDED_LABELS` records that.

**No LLM, no stack, no index.** Routing is a pure function of the query string,
so this runs anywhere and is byte-identical across runs.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from dataclasses import field
from typing import Any

#: Eval query class -> the label the router is expected to produce.
#:
#: ``multi_file`` maps to ``lookup`` deliberately. Those queries ask for specific
#: facts that happen to be spread over several recordings ("across the incident
#: review sessions … what was the supplier we selected, the release it was
#: scheduled into, …"); the chunk plane is where those sentences live, and a
#: digest of each file would answer none of them. Spanning files is a *scope*
#: property, not an intent.
EXPECTED_INTENT_BY_CLASS: dict[str, str] = {
    "lookup": "lookup",
    "multi_file": "lookup",
    "summarize": "summarize",
    "aggregation": "aggregate",
}

#: Labels no corpus in this harness can score. Reported so a reader never mistakes
#: "absent from the matrix" for "never predicted".
UNGROUNDED_LABELS: tuple[str, ...] = ("temporal",)

#: How a case's expected label was arrived at. It travels with every row because
#: the three are not equally strong evidence.
BY_CONSTRUCTION = "by-construction"
SURFACE_RULE = "surface-rule"


@dataclass(frozen=True)
class RoutingCase:
    """One labelled query, with the provenance of its label attached."""

    query_id: str
    text: str
    expected: str
    query_class: str
    corpus: str
    label_provenance: str
    rule: str = ""
    #: ``(year, month)`` the router should recover, for cases whose class turns
    #: on a date. ``None`` means the case makes no claim about the hint.
    expected_temporal: tuple[int, int] | None = None


@dataclass
class RoutingResult:
    """Per-case predictions plus everything derived from them."""

    predicted: dict[str, str] = field(default_factory=dict)
    signals: dict[str, tuple[str, ...]] = field(default_factory=dict)
    confusion: dict[str, Counter] = field(default_factory=dict)
    temporal_recovered: dict[str, bool] = field(default_factory=dict)
    case_count: int = 0

    def accuracy(self, expected: str | None = None) -> float:
        """Share correct — overall, or within one expected label."""
        rows = (
            self.confusion.items()
            if expected is None
            else [(expected, self.confusion.get(expected, Counter()))]
        )
        total = 0
        correct = 0
        for label, counts in rows:
            total += sum(counts.values())
            correct += counts.get(label, 0)
        return correct / total if total else 0.0

    def leakage(self, expected: str) -> dict[str, Any]:
        """Where an expected label's queries went when they went wrong."""
        counts = self.confusion.get(expected, Counter())
        total = sum(counts.values())
        wrong = {label: n for label, n in sorted(counts.items()) if label != expected}
        return {
            "n": total,
            "misrouted": sum(wrong.values()),
            "rate": (sum(wrong.values()) / total) if total else 0.0,
            "to": wrong,
        }


def evaluate_routing(cases: list[RoutingCase]) -> RoutingResult:
    """Run the production router over every case and tabulate.

    Args:
        cases: Labelled queries, in any order (results are keyed by id).

    Returns:
        A :class:`RoutingResult`.

    Raises:
        ValueError: If ``cases`` is empty. An empty confusion matrix reports
            100% accuracy on every label, which is the shape of assertion this
            repo's test auditor exists to reject.
    """
    if not cases:
        raise ValueError("Refusing to evaluate routing over an empty case set")

    from app.services.chat.router import route

    result = RoutingResult(case_count=len(cases))
    for case in sorted(cases, key=lambda c: c.query_id):
        decision = route(case.text)
        result.predicted[case.query_id] = decision.intent
        result.signals[case.query_id] = decision.signals
        result.confusion.setdefault(case.expected, Counter())[decision.intent] += 1
        if case.expected_temporal is not None:
            hint = decision.temporal
            result.temporal_recovered[case.query_id] = bool(
                hint is not None and (hint.year, hint.month) == case.expected_temporal
            )
    return result


def build_routing_report(cases: list[RoutingCase], result: RoutingResult) -> dict[str, Any]:
    """The self-describing block written into the results document."""
    provenance = Counter(case.label_provenance for case in cases)
    by_corpus: dict[str, Counter] = {}
    for case in cases:
        by_corpus.setdefault(case.corpus, Counter())[case.expected] += 1

    temporal_cases = [c for c in cases if c.expected_temporal is not None]
    recovered = sum(1 for c in temporal_cases if result.temporal_recovered.get(c.query_id))

    return {
        "classifier": "app.services.chat.router.route (rules only)",
        "llm_required": False,
        "cases": result.case_count,
        "accuracy": round(result.accuracy(), 4),
        "accuracy_by_expected": {
            label: round(result.accuracy(label), 4) for label in sorted(result.confusion)
        },
        "lookup_leakage": {
            **result.leakage("lookup"),
            "why_it_matters": (
                "The chunk tier is never removed, so a misroute cannot make a query "
                "unanswerable. What it costs is the reduced chunk budget on the "
                "summarize/aggregate branches — and that cost lands only here. This "
                "is the quantity D5's 'lookup must never regress' turns on."
            ),
        },
        "confusion": {
            expected: dict(sorted(counts.items()))
            for expected, counts in sorted(result.confusion.items())
        },
        "label_provenance": dict(sorted(provenance.items())),
        "cases_by_corpus": {
            corpus: dict(sorted(counts.items())) for corpus, counts in sorted(by_corpus.items())
        },
        "temporal_slot": {
            "cases": len(temporal_cases),
            "recovered": recovered,
            "rate": round(recovered / len(temporal_cases), 4) if temporal_cases else 0.0,
        },
        "ungrounded_labels": list(UNGROUNDED_LABELS),
        "caveats": [
            "QMSum's summarize label is a surface rule over the query's opening word, "
            "which overlaps the router's own lexicon — agreement on that class is "
            "partly tautological and is NOT an independent result.",
            "QMSum's lookup label is the complement of that rule over human-written "
            "questions, so leakage OUT of lookup is a real measurement.",
            "The synthetic tier's labels are true by construction: the generator built "
            "each query from the rule the label names.",
            f"No corpus here exercises {', '.join(UNGROUNDED_LABELS)}; unit tests do.",
        ],
    }


def render_routing_table(result: RoutingResult) -> str:
    """One markdown row per expected label: n, correct, and where the rest went."""
    header = "| expected | n | correct | accuracy | misrouted to |\n|---|---|---|---|---|\n"
    lines = []
    for expected in sorted(result.confusion):
        counts = result.confusion[expected]
        total = sum(counts.values())
        correct = counts.get(expected, 0)
        wrong = ", ".join(
            f"{label} {n}" for label, n in sorted(counts.items()) if label != expected
        )
        lines.append(
            f"| {expected} | {total} | {correct} | {correct / total:.4f} | {wrong or '—'} |"
        )
    return header + "\n".join(lines) + "\n"
