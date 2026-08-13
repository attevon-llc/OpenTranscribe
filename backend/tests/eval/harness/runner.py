"""Driving the production retrieval path over an eval corpus.

**Which path.** Addendum §4 is explicit: benchmark the *chat* path, not
``/api/search``. Chat fuses over ``dynamic_rrf_window(size)``
(``max(100, min(size*4, 500))``) while the search UI always fuses over 500, so
numbers from one do not characterise the other. The named entry point in §4 —
``retrieve_chunks`` — lives in ``app/services/search/chunk_retrieval.py`` and is
imported by ``services/chat/retrieval.py`` and by nothing else, so driving it
*is* driving the chat path. Verified on this branch, not assumed.

Two stages are measurable, and the difference matters for D5:

``retrieve``
    ``retrieve_chunks`` alone — the candidate pool, which is what every fusion
    or indexing change moves. **The default and the committed control.**
``rerank``
    ``retrieve_chunks`` -> production cross-encoder ``rerank`` ->
    ``diversity_sample``: what actually reaches the prompt. Off by default
    because it needs the cross-encoder weights in the model cache, and it
    raises rather than silently degrading to a no-op if they are missing.

Neither stage needs an LLM. D6 requires the no-LLM deployment to stay
first-class, and a retrieval benchmark that could not run without one would
quietly make the no-LLM path unmeasurable.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from tests.eval.harness.corpora import EvalQuery
from tests.eval.harness.metrics import RunDoc

logger = logging.getLogger(__name__)

STAGES = ("retrieve", "rerank")

#: ``corpus``      retrieve across every accessible file — what chat actually does,
#:                 and the number every later stage has to move.
#: ``gold-files``  restrict the scope to the query's own gold files. An **oracle**:
#:                 it assumes perfect file selection, so it is an upper bound, not a
#:                 system result. It exists because the two failure modes are
#:                 different — "the right meeting never surfaced" and "the right
#:                 meeting surfaced and the wrong passage in it won" — and a single
#:                 corpus-wide number cannot tell them apart.
SCOPES = ("corpus", "gold-files")

#: Candidate pool the harness always asks for, independent of admin settings.
#: Phase 7 sweeps ``final_chunks``; pinning the pool here is what keeps
#: phase-over-phase deltas comparable (addendum §4, fixed-k metrics).
DEFAULT_SIZE = 48


@dataclass
class RetrievalConfig:
    """Everything about a run that could move a number."""

    stage: str = "retrieve"
    size: int = DEFAULT_SIZE
    search_mode: str = "hybrid"
    scope: str = "corpus"
    workers: int = 4
    max_per_file: int = 3
    final_chunks: int = 20
    rerank_max_pairs: int = 50

    def as_dict(self) -> dict[str, object]:
        base: dict[str, object] = {
            "path": "chat retrieval (app.services.search.chunk_retrieval.retrieve_chunks)",
            "stage": self.stage,
            "candidate_pool": self.size,
            "search_mode": self.search_mode,
            "scope": self.scope,
            "workers": self.workers,
            "scope_note": (
                "corpus-wide, file_uuids=None"
                if self.scope == "corpus"
                else "ORACLE: file_uuids = the query's own gold files (upper bound)"
            ),
            "llm_required": False,
        }
        if self.stage == "rerank":
            base.update(
                {
                    "rerank_max_pairs": self.rerank_max_pairs,
                    "max_chunks_per_file": self.max_per_file,
                    "final_chunks": self.final_chunks,
                }
            )
        return base


def _to_run_docs(hits) -> list[RunDoc]:
    """Retrieved hits as run documents, labelled by the plane they came from.

    A negative ``chunk_index`` is the digest sentinel (index v6), so it is also
    the only signal available here that a *digest* reached the chat path — the
    hit dataclass carries no ``doc_type``. Labelling it honestly matters twice
    over: the id must be the one the index actually holds
    (``{uuid}_digest_{n}``, not ``{uuid}_-1``), and the tie-break sorts on
    ``doc_type``, which is what stops a digest winning a tie on its NAME. As of
    Stage 3 the chat filter excludes digests, so this branch should not fire;
    it exists because it will in Stage 4, and because a mislabelled document is
    worse than an excluded one.
    """
    from app.services.ingest_artifacts.index_mapping import DOC_TYPE_CHUNK
    from app.services.ingest_artifacts.index_mapping import DOC_TYPE_DIGEST
    from app.services.ingest_artifacts.index_mapping import digest_document_id

    docs: list[RunDoc] = []
    for hit in hits:
        chunk_index = int(hit.chunk_index)
        is_digest = chunk_index < 0
        docs.append(
            RunDoc(
                doc_id=(
                    digest_document_id(hit.file_uuid, -1 - chunk_index)
                    if is_digest
                    else f"{hit.file_uuid}_{chunk_index}"
                ),
                score=float(hit.score),
                doc_type=DOC_TYPE_DIGEST if is_digest else DOC_TYPE_CHUNK,
                file_uuid=hit.file_uuid,
                chunk_index=chunk_index,
            )
        )
    return docs


def execute(
    queries: list[EvalQuery],
    *,
    user_id: int,
    config: RetrievalConfig,
    organization_id: int | None = None,
    progress_every: int = 200,
) -> dict[str, list[RunDoc]]:
    """Run every query through the production retriever.

    Args:
        queries: Scoreable queries, in a deterministic order.
        user_id: Owner of the injected corpus — enforced by the index filter.
        config: Retrieval knobs, pinned by the harness.
        organization_id: Tenant, or None for personal scope.
        progress_every: Log cadence; does not affect results.

    Returns:
        ``query_id -> [RunDoc]`` in provider-ranked order. A query that retrieved
        nothing is present with an empty list, so the caller can tell "answered
        with nothing" from "never asked".

    Raises:
        RuntimeError: If ``stage='rerank'`` and the cross-encoder is unavailable.
            Silently skipping the rerank would report a rerank number that never
            reranked anything.
    """
    if config.stage not in STAGES:
        raise ValueError(f"Unknown stage {config.stage!r}; expected one of {STAGES}")
    if config.scope not in SCOPES:
        raise ValueError(f"Unknown scope {config.scope!r}; expected one of {SCOPES}")

    from app.services.search.chunk_retrieval import diversity_sample
    from app.services.search.chunk_retrieval import retrieve_chunks

    reranker = None
    if config.stage == "rerank":
        from app.services.chat.reranker import get_reranker
        from app.services.chat.reranker import rerank as rerank_fn

        if get_reranker() is None:
            raise RuntimeError(
                "stage='rerank' needs the cross-encoder weights in the model cache; "
                "refusing to report a rerank measurement that did not rerank."
            )
        reranker = rerank_fn

    def one(query: EvalQuery) -> tuple[str, list[RunDoc]]:
        # None means "every accessible file"; a LIST means exactly those. The two
        # are not interchangeable — an empty list matches nothing.
        scope: list[str] | None = None
        if config.scope == "gold-files":
            scope = sorted({span.file_uuid for span in query.spans})
        hits = retrieve_chunks(
            query.text,
            user_id=user_id,
            organization_id=organization_id,
            file_uuids=scope,
            size=config.size,
            search_mode=config.search_mode,
        )
        if reranker is not None:
            hits = reranker(query.text, hits, max_pairs=config.rerank_max_pairs)
            hits = diversity_sample(hits, max_per_file=config.max_per_file, cap=config.final_chunks)
        return query.query_id, _to_run_docs(hits)

    run: dict[str, list[RunDoc]] = {}
    results: Iterable[tuple[str, list[RunDoc]]]
    if config.workers <= 1:
        results = (one(query) for query in queries)
    else:
        # Retrieval is network-bound, and each query is independent — the result
        # is keyed by query id, so completion order cannot reach a number.
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(max_workers=config.workers)
        results = pool.map(one, queries)

    for position, (query_id, docs) in enumerate(results, start=1):
        run[query_id] = docs
        if progress_every and position % progress_every == 0:
            logger.info("Retrieved %d/%d queries", position, len(queries))
    return run
