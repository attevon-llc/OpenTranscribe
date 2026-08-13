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

    A class with no queries is omitted rather than reported as zero: an empty
    class is missing data, and a 0.000 in a table reads as a measured failure.
    """
    by_corpus: dict[str, list[EvalQuery]] = {}
    for query in queries:
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


def build_results(
    *,
    control_name: str,
    corpora: list[dict[str, Any]],
    retrieval: dict[str, Any],
    policy: dict[str, Any],
    index_state: dict[str, Any],
    qrels_stats: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """The committed results document. Deterministic by construction."""
    from tests.eval.harness.metrics import measure_provenance

    return {
        "schema_version": 1,
        "control_name": control_name,
        "metric_engine": measure_provenance(),
        "relevance_policy": policy,
        "retrieval": retrieval,
        "index": index_state,
        "qrels": qrels_stats,
        "corpora": corpora,
        "rows": rows,
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
