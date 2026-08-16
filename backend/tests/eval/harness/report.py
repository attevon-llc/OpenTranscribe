"""Results assembly and the metric table.

Two rules the gate depends on:

* **Everything written here is deterministic.** No wall clock, no elapsed
  milliseconds, no host name, no dict-insertion-order dependence. Two runs over
  an unchanged corpus produce byte-identical ``metrics.json`` and ``metrics.md``;
  anything that cannot make that promise (durations, timestamps) goes in a
  separate ``runinfo.json`` that is explicitly outside the claim.
* **Licence tier is a column.** A number's tier decides whether it may be
  published, and rediscovering that at writing time is how an unpublishable
  figure gets into a paper.
"""

from __future__ import annotations

import json
from typing import Any

from tests.eval.harness.answers import MEASURES as ANSWER_MEASURES
from tests.eval.harness.answers import AnswerResult
from tests.eval.harness.answers import subset_answers
from tests.eval.harness.corpora import CLASSES
from tests.eval.harness.corpora import EvalQuery
from tests.eval.harness.metrics import MEASURES
from tests.eval.harness.metrics import EvalResult
from tests.eval.harness.metrics import subset

_PRECISION = 6


def _round(values: dict[str, float]) -> dict[str, float]:
    return {name: round(value, _PRECISION) for name, value in values.items()}


def build_rows(queries: list[EvalQuery], result: EvalResult) -> list[dict[str, Any]]:
    """One row per (corpus, query class), plus an ``all`` row per corpus.

    **Retrieval-scored queries only.** An answer-scored query (the aggregation
    class) has its own table with its own measures; putting the two in one table
    is how "aggregation" came to sit in the metric table with an nDCG beside it
    and nothing scoring the count it actually asks for.

    A class with no queries is omitted rather than reported as zero: an empty
    class is missing data, and a 0.000 in a table reads as a measured failure.
    """
    by_corpus: dict[str, list[EvalQuery]] = {}
    for query in queries:
        if query.scored_on != "retrieval":
            continue
        by_corpus.setdefault(query.corpus, []).append(query)

    rows: list[dict[str, Any]] = []
    for corpus in sorted(by_corpus):
        members = by_corpus[corpus]
        tier = members[0].license_tier
        for query_class in (*CLASSES, "all"):
            selected = (
                members
                if query_class == "all"
                else [q for q in members if q.query_class == query_class]
            )
            if not selected:
                continue
            ids = {q.query_id for q in selected}
            scoped = subset(result, ids)
            rows.append(
                {
                    "corpus": corpus,
                    "license_tier": tier,
                    "query_class": query_class,
                    "queries": scoped.query_count,
                    "unanswered": len(scoped.unanswered),
                    "scored_on": sorted({q.scored_on for q in selected}),
                    "metrics": _round(scoped.aggregate),
                }
            )
    return rows


def render_table(rows: list[dict[str, Any]]) -> str:
    """The metric table, as GitHub-flavoured Markdown."""
    names = list(MEASURES)
    header = ["corpus", "tier", "class", "n", "unans."] + names
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * len(header)) + "|",
    ]
    for row in rows:
        cells = [
            row["corpus"],
            row["license_tier"],
            row["query_class"],
            str(row["queries"]),
            str(row["unanswered"]),
        ]
        cells += [f"{row['metrics'].get(name, 0.0):.4f}" for name in names]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def build_answer_rows(queries: list[EvalQuery], result: AnswerResult) -> list[dict[str, Any]]:
    """Answer scores per (corpus, class) and per (corpus, class, rule).

    The per-rule breakdown is not decoration: "aggregation" is five different
    questions (count files, list files, count events, top speaker, count within
    a month) answered by three different mechanisms, and a single class mean
    hides which one is failing.
    """
    by_corpus: dict[str, list[EvalQuery]] = {}
    for query in queries:
        if query.scored_on != "answer":
            continue
        by_corpus.setdefault(query.corpus, []).append(query)

    rows: list[dict[str, Any]] = []
    for corpus in sorted(by_corpus):
        members = by_corpus[corpus]
        tier = members[0].license_tier
        for query_class in sorted({q.query_class for q in members}):
            in_class = [q for q in members if q.query_class == query_class]
            groups: list[tuple[str, list[EvalQuery]]] = [("all", in_class)]
            groups += [
                (rule, [q for q in in_class if q.rule == rule])
                for rule in sorted({q.rule for q in in_class if q.rule})
            ]
            for rule, selected in groups:
                scoped = subset_answers(result, {q.query_id for q in selected})
                rows.append(
                    {
                        "corpus": corpus,
                        "license_tier": tier,
                        "query_class": query_class,
                        "rule": rule,
                        "queries": scoped.query_count,
                        "unanswered": len(scoped.unanswered),
                        "scored_on": ["answer"],
                        "metrics": _round(scoped.aggregate),
                    }
                )
    return rows


def render_answer_table(rows: list[dict[str, Any]]) -> str:
    """The answer table. No column name it prints appears in the retrieval
    table, so the two can never be read as the same measurement."""
    names = list(ANSWER_MEASURES)
    header = ["corpus", "tier", "class", "rule", "n", "unans."] + names
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * len(header)) + "|",
    ]
    for row in rows:
        cells = [
            row["corpus"],
            row["license_tier"],
            row["query_class"],
            row["rule"],
            str(row["queries"]),
            str(row["unanswered"]),
        ]
        cells += [f"{row['metrics'].get(name, 0.0):.4f}" for name in names]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def build_answer_details(queries: list[EvalQuery], result: AnswerResult) -> list[dict[str, Any]]:
    """Per-query gold vs submitted, sorted by query id.

    Twenty rows of "gold 12, submitted 3" is the difference between a failing
    number and a diagnosable one, and it is what lets a reader who did not run
    the harness check the exactness claim rather than take it.
    """
    by_id = {query.query_id: query for query in queries if query.scored_on == "answer"}
    details: list[dict[str, Any]] = []
    for query_id in sorted(result.per_query):
        query = by_id.get(query_id)
        if query is None or query.gold_answer is None:
            continue
        submitted = result.submitted.get(query_id)
        details.append(
            {
                "query_id": query_id,
                "license_tier": query.license_tier,
                "rule": query.rule,
                "kind": query.gold_answer.kind,
                "gold": query.gold_answer.as_json(),
                "submitted": None if submitted is None else submitted.as_json(),
                "scores": _round(result.per_query[query_id]),
            }
        )
    return details


def build_results(
    *,
    control_name: str,
    corpora: list[dict[str, Any]],
    retrieval: dict[str, Any],
    policy: dict[str, Any],
    index_state: dict[str, Any],
    qrels_stats: dict[str, Any],
    rows: list[dict[str, Any]],
    answers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The committed results document. Deterministic by construction.

    ``answers`` is a self-contained block — its own scoring provenance, its own
    answerer identity, its own rows — so no reader can arrive at an EM value
    without also reading what produced it.
    """
    from tests.eval.harness.metrics import measure_provenance

    return {
        "schema_version": 2,
        "control_name": control_name,
        "metric_engine": measure_provenance(),
        "relevance_policy": policy,
        "retrieval": retrieval,
        "index": index_state,
        "qrels": qrels_stats,
        "corpora": corpora,
        "rows": rows,
        "answers": answers or {"scored": 0, "note": "no answer-scored queries in this run"},
    }


def dumps(results: dict[str, Any]) -> str:
    """Canonical JSON: sorted keys, fixed separators, trailing newline."""
    return json.dumps(results, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def render_comparison(baseline_rows: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    """Delta table against a named control, for the D5 per-stage PR table.

    A row present in one side and not the other is reported as such rather than
    compared against zero.
    """
    keyed = {(row["corpus"], row["query_class"]): row for row in rows}
    names = list(MEASURES)
    header = ["corpus", "class"] + [f"Δ {name}" for name in names]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * len(header)) + "|",
    ]
    for base in baseline_rows:
        key = (base["corpus"], base["query_class"])
        current = keyed.pop(key, None)
        if current is None:
            lines.append(f"| {key[0]} | {key[1]} | " + " | ".join(["absent"] * len(names)) + " |")
            continue
        cells = [key[0], key[1]]
        for name in names:
            delta = current["metrics"].get(name, 0.0) - base["metrics"].get(name, 0.0)
            cells.append(f"{delta:+.4f}")
        lines.append("| " + " | ".join(cells) + " |")
    for key in keyed:
        lines.append(f"| {key[0]} | {key[1]} | " + " | ".join(["new"] * len(names)) + " |")
    return "\n".join(lines) + "\n"
