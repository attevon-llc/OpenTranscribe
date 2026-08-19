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


def build_retrieval_per_query(queries: list[EvalQuery], result: EvalResult) -> list[dict[str, Any]]:
    """Every retrieval-scored query's own scores (#461 phase 0).

    **Every retrieval number this harness has ever published is a point estimate
    with no confidence interval** — including the reranker's measured 20-33% nDCG@10
    deficit, which is the one finding with direct product impact and is currently
    unactioned *because* nobody can say whether it is an effect or noise. A mean over
    N queries cannot answer that; the spread over those N queries can.

    Emitting the per-query scores is the cheapest possible fix: they are already
    computed by :func:`metrics.evaluate` and were simply discarded for retrieval
    queries, while the answer table has published them all along.

    Deterministic by construction — sorted by query id, rounded like every other
    number here — so it stays inside the byte-identical ``metrics.json`` promise.

    ⚠️ **This is the instrument, not the analysis.** It does not compute an interval;
    it makes one computable by anyone reading the file. Deciding *which* interval is
    a separate judgement, and hard-coding one here would smuggle that judgement into
    the raw data.
    """
    by_id = {query.query_id: query for query in queries if query.scored_on == "retrieval"}
    details: list[dict[str, Any]] = []
    for query_id in sorted(result.per_query):
        query = by_id.get(query_id)
        if query is None:
            continue
        details.append(
            {
                "query_id": query_id,
                "corpus": query.corpus,
                "query_class": query.query_class,
                "license_tier": query.license_tier,
                # How many documents the qrels judge relevant for this query. A
                # per-query score is unreadable without it: nDCG@10 over 1 gold
                # document and over 40 are different measurements.
                "gold_count": len(query.spans),
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
    retrieval_per_query: list[dict[str, Any]],
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
        # Per-query retrieval scores (#461 phase 0). Without these every number in
        # `rows` is a point estimate nobody can put an interval around.
        "retrieval_per_query": retrieval_per_query,
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
