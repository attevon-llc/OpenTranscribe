"""BM25 characterisation of a generated corpus, and the rank hygiene it depends on.

**Scope.** This is a *corpus characterisation* tool, not the Stage-1 metric engine. It
answers two questions about the data — "is this set discriminative?" (BM25 R@1) and "did I
manufacture the AMI near-duplicate pathology?" (competitors within 10% of gold) — using
the same measurement definitions ``.rag-403/eval-corpus-plan.md`` §4 used on QMSum, so the
two tables are comparable. nDCG and the graded chunk-level judgements stay with the Stage-1
harness and ``pytrec_eval_terrier``; nothing here duplicates that.

**Rank hygiene (survey §1.6).** Document ids in this product are ``{file_uuid}_{chunk_index}``
and digests will be ``{uuid}_digest``. ``'d'`` outsorts every digit and trec_eval breaks
ties by docid *descending*, so a digest tied with a chunk of the same file is ranked first
by the evaluator — which could manufacture the very win Stage 3 is gated on. RRF produces
such ties structurally. :func:`normalise_run` therefore resolves ties itself, by ascending
``(doc_type, file_uuid, chunk_index)``, and re-emits strictly decreasing scores, so no
downstream evaluator's id-ordering rule can influence a result.

**Missing queries (survey §1.5).** :func:`evaluate` iterates the **qrels** query set and
substitutes 0.0 for a query the run did not answer. ``mean(results.values())`` over a
result dict that silently omits unanswered queries flatters every regression.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .textindex import Corpus
from .textindex import tokenize

BM25_K1 = 1.2
BM25_B = 0.75


@dataclass(frozen=True)
class RunItem:
    """One retrieved candidate, carrying the fields the tie-break sorts on."""

    doc_id: str
    score: float
    doc_type: str = "chunk"
    file_uuid: str = ""
    chunk_index: int = -1


def normalise_run(items: list[RunItem]) -> list[RunItem]:
    """Return ``items`` ranked with all ties resolved and scores strictly decreasing.

    Sort key is ``(-score, doc_type, file_uuid, chunk_index)``. The re-emitted scores are
    ``len(items) - rank``, which preserves the order and destroys any information an
    evaluator could use to re-break ties by document id.
    """
    ordered = sorted(items, key=lambda i: (-i.score, i.doc_type, i.file_uuid, i.chunk_index))
    total = len(ordered)
    return [
        RunItem(i.doc_id, float(total - rank), i.doc_type, i.file_uuid, i.chunk_index)
        for rank, i in enumerate(ordered)
    ]


class Bm25:
    """Okapi BM25 over whole meetings, matching the §4 QMSum measurement."""

    def __init__(self, corpus: Corpus, k1: float = BM25_K1, b: float = BM25_B) -> None:
        """Precompute document lengths and inverse document frequencies."""
        self.corpus = corpus
        self.k1, self.b = k1, b
        self.doc_len = {uid: len(doc.tokens) for uid, doc in corpus.docs.items()}
        self.avg_len = sum(self.doc_len.values()) / max(1, len(self.doc_len))
        n_docs = len(corpus.docs)
        self.idf = {
            term: math.log(1 + (n_docs - len(docs) + 0.5) / (len(docs) + 0.5))
            for term, docs in corpus.token_index.items()
        }

    def rank(self, query: str) -> list[RunItem]:
        """Score every document containing at least one query term, best first."""
        scores: dict[str, float] = {}
        for term in tokenize(query):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for file_uuid in self.corpus.token_index[term]:
                freq = self.corpus.term_freq[file_uuid].get(term, 0)
                norm = 1 - self.b + self.b * self.doc_len[file_uuid] / self.avg_len
                scores[file_uuid] = scores.get(file_uuid, 0.0) + idf * (
                    freq * (self.k1 + 1) / (freq + self.k1 * norm)
                )
        items = [RunItem(uid, score, "file", uid, -1) for uid, score in sorted(scores.items())]
        return normalise_run(items)

    def raw_scores(self, query: str) -> dict[str, float]:
        """Return unnormalised BM25 scores, for the near-duplicate competitor count."""
        out: dict[str, float] = {}
        for term in tokenize(query):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for file_uuid in self.corpus.token_index[term]:
                freq = self.corpus.term_freq[file_uuid].get(term, 0)
                norm = 1 - self.b + self.b * self.doc_len[file_uuid] / self.avg_len
                out[file_uuid] = out.get(file_uuid, 0.0) + idf * (
                    freq * (self.k1 + 1) / (freq + self.k1 * norm)
                )
        return out


def recall_at_k(ranked: list[RunItem], gold: set[str], k: int) -> float:
    """Fraction of gold documents present in the top ``k``."""
    if not gold:
        return 0.0
    top = {item.doc_id for item in ranked[:k]}
    return len(top & gold) / len(gold)


def reciprocal_rank(ranked: list[RunItem], gold: set[str]) -> float:
    """1/rank of the first gold document, or 0.0 if none is retrieved."""
    for rank, item in enumerate(ranked, start=1):
        if item.doc_id in gold:
            return 1.0 / rank
    return 0.0


def rank_of_first_gold(ranked: list[RunItem], gold: set[str]) -> int | None:
    """Rank (1-based) of the first gold document, or None."""
    for rank, item in enumerate(ranked, start=1):
        if item.doc_id in gold:
            return rank
    return None


def evaluate(
    qrels: dict[str, set[str]], runs: dict[str, list[RunItem]], ks: tuple[int, ...] = (1, 5, 10)
) -> dict[str, float]:
    """Aggregate recall@k and MRR over the **qrels** query set.

    A query present in ``qrels`` but absent from ``runs`` scores 0.0 and is included in
    the mean — ``trec_eval -c`` semantics, which is not the library default anywhere.
    """
    if not qrels:
        return {}
    totals = {f"recall@{k}": 0.0 for k in ks}
    totals["mrr"] = 0.0
    for query_id, gold in qrels.items():
        ranked = runs.get(query_id, [])
        for k in ks:
            totals[f"recall@{k}"] += recall_at_k(ranked, gold, k)
        totals["mrr"] += reciprocal_rank(ranked, gold)
    return {name: value / len(qrels) for name, value in sorted(totals.items())}


def near_duplicate_competitors(scores: dict[str, float], gold: str, threshold: float = 0.9) -> int:
    """Count non-gold documents scoring at least ``threshold`` x the gold score.

    The metric from ``.rag-403/eval-corpus-plan.md`` §4, reproduced exactly so the
    synthetic corpus's number is comparable to QMSum's 49.2 (AMI Product) and 2.2
    (Committee). A gold score of zero yields 0 rather than counting the whole corpus.
    """
    gold_score = scores.get(gold, 0.0)
    if gold_score <= 0.0:
        return 0
    limit = threshold * gold_score
    return sum(1 for uid, score in scores.items() if uid != gold and score >= limit)
