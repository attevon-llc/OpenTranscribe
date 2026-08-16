"""Corpus characterisation: BM25 difficulty and near-duplicate structure.

Produces ``metrics.json``. Two numbers matter and both are reported honestly:

* **BM25 R@1 per class and surface.** If the paraphrase set scores near 1.0 the corpus
  cannot discriminate between retrievers and the tier is worthless for its purpose. The
  ``verbatim`` surface is the deliberately-easy control: the gap between the two is how
  much of the difficulty comes from wording rather than from the haystack.
* **Near-duplicate competitors.** ``.rag-403/eval-corpus-plan.md`` §4's metric — the mean
  number of *other* meetings scoring within 10% of gold — measured identically here so
  the synthetic corpus can be placed on the same scale as AMI's 49.2 and Committee's 2.2.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from .bm25 import Bm25
from .bm25 import near_duplicate_competitors
from .bm25 import rank_of_first_gold
from .bm25 import recall_at_k
from .bm25 import reciprocal_rank
from .textindex import Corpus
from .textindex import load_jsonl


def measure_corpus(corpus_dir: Path, limit: int | None = None) -> dict:
    """Measure BM25 difficulty and near-duplicate structure over a generated corpus.

    Args:
        corpus_dir: A directory written by ``build_corpus``.
        limit: Optional cap on queries per class, for fast smoke runs.

    Returns:
        The ``metrics.json`` payload.
    """
    corpus_dir = Path(corpus_dir)
    corpus = Corpus.load(corpus_dir)
    queries = load_jsonl(corpus_dir / "queries.jsonl")
    engine = Bm25(corpus)

    groups: dict[str, list[dict]] = {}
    seen: dict[str, int] = {}
    for query in sorted(queries, key=lambda q: q["query_id"]):
        key = f"{query['query_class']}/{query['surface']}"
        seen[key] = seen.get(key, 0) + 1
        if limit is not None and seen[key] > limit:
            continue
        groups.setdefault(key, []).append(query)

    per_group = {
        name: _measure_group(engine, rows, len(corpus.docs))
        for name, rows in sorted(groups.items())
    }
    all_rows = [q for rows in groups.values() for q in rows]
    return {
        "corpus_id": json.loads((corpus_dir / "config.json").read_text())["corpus_id"],
        "documents": len(corpus.docs),
        "bm25": {"k1": engine.k1, "b": engine.b, "unit": "whole meeting as one document"},
        "queries_measured": len(all_rows),
        "by_class_and_surface": per_group,
        "overall": _measure_group(engine, all_rows, len(corpus.docs)),
    }


def _measure_group(engine: Bm25, rows: list[dict], pool: int) -> dict:
    """Aggregate BM25 metrics over one class/surface group."""
    if not rows:
        return {}
    recalls: dict[int, list[float]] = {k: [] for k in (1, 5, 10)}
    rr: list[float] = []
    ranks: list[int] = []
    competitors: list[int] = []
    unanswered = 0
    for query in rows:
        gold = set(query["gold_files"])
        ranked = engine.rank(query["text"])
        if not ranked:
            unanswered += 1
        for k in recalls:
            recalls[k].append(recall_at_k(ranked, gold, k))
        rr.append(reciprocal_rank(ranked, gold))
        rank = rank_of_first_gold(ranked, gold)
        ranks.append(rank if rank is not None else pool + 1)
        if len(gold) == 1:
            scores = engine.raw_scores(query["text"])
            competitors.append(near_duplicate_competitors(scores, next(iter(gold))))
    out = {
        "queries": len(rows),
        "unanswered_scored_zero": unanswered,
        "R@1": round(statistics.mean(recalls[1]), 4),
        "R@5": round(statistics.mean(recalls[5]), 4),
        "R@10": round(statistics.mean(recalls[10]), 4),
        "MRR": round(statistics.mean(rr), 4),
        "median_rank_of_first_gold": int(statistics.median(ranks)),
    }
    if competitors:
        out["near_duplicate_competitors_mean"] = round(statistics.mean(competitors), 2)
        out["near_duplicate_competitors_pct_of_pool"] = round(
            100 * statistics.mean(competitors) / max(1, pool), 2
        )
        out["near_duplicate_competitors_single_gold_queries"] = len(competitors)
    return out


def write_metrics(corpus_dir: Path, metrics: dict) -> Path:
    """Write ``metrics.json`` (outside the determinism set — it is a measurement)."""
    path = Path(corpus_dir) / "metrics.json"
    path.write_text(
        json.dumps(metrics, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return path
