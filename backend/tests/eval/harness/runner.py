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
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tests.eval.harness.corpora import EvalQuery
from tests.eval.harness.metrics import RunDoc

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.search.fusion import FusionConfig

logger = logging.getLogger(__name__)

STAGES = ("retrieve", "rerank", "route")

#: ``corpus``      retrieve across every accessible file — what chat actually does,
#:                 and the number every later stage has to move.
#: ``gold-files``  restrict the scope to the query's own gold files. An **oracle**:
#:                 it assumes perfect file selection, so it is an upper bound, not a
#:                 system result. It exists because the two failure modes are
#:                 different — "the right meeting never surfaced" and "the right
#:                 meeting surfaced and the wrong passage in it won" — and a single
#:                 corpus-wide number cannot tell them apart.
SCOPES = ("corpus", "gold-files")

#: The production retrieval budget — candidate pool, what reaches the prompt,
#: and the per-file ceiling. These are Stage 5's ``48/12/4`` sweep axes, and they
#: are the **shipped** values (``core/constants.DEFAULT_CHAT_RAG_*``): a rerank
#: measurement taken at some other budget describes a deployment nobody runs.
#: Restated rather than imported because ``app.core.constants`` pulls
#: ``faster_whisper`` in at import time and these logic tests must stay
#: stack-free; ``test_eval_fusion_arm`` fails if the two ever drift apart.
DEFAULT_SIZE = 48
DEFAULT_FINAL_CHUNKS = 12
DEFAULT_MAX_PER_FILE = 4
DEFAULT_RERANK_MAX_PAIRS = 50


@dataclass
class RetrievalConfig:
    """Everything about a run that could move a number."""

    stage: str = "retrieve"
    size: int = DEFAULT_SIZE
    #: Digest sections the ``route`` stage fetches when the router asks for the
    #: digest tier. Matches ``service.py``'s production default.
    digest_size: int = 6
    search_mode: str = "hybrid"
    scope: str = "corpus"
    workers: int = 4
    max_per_file: int = DEFAULT_MAX_PER_FILE
    final_chunks: int = DEFAULT_FINAL_CHUNKS
    rerank_max_pairs: int = DEFAULT_RERANK_MAX_PAIRS
    #: Hybrid fusion strategy for every retrieval call in this run (#363).
    #: ``None`` means the deployment's configured default, which is a different
    #: statement from naming ``rrf`` explicitly and is recorded as such.
    fusion: FusionConfig | None = None

    def fusion_provenance(self) -> dict[str, object]:
        """The fusion arm this run used, resolved and named.

        A sweep whose results files do not identify their own arm is a table of
        numbers nobody can attribute, and the arms differ by one CLI flag. So
        the *resolved* strategy is recorded — never the flag — together with the
        OpenSearch pipeline id it derives, which is the thing actually attached
        to every query and therefore the only unforgeable label.

        Only the parameters that apply to the strategy are emitted, mirroring
        ``FusionConfig.slug()``: ``rank_constant`` is inert under
        ``normalization`` and recording it there would describe a knob the run
        did not turn.

        Returns:
            A JSON-safe description, or a reason string when the app layer is
            not importable (logic tests run without a stack).
        """
        try:
            from app.services.search.fusion import RRF
            from app.services.search.fusion import resolve_fusion
            from app.services.search.fusion import search_pipeline_id
        except Exception as exc:  # noqa: BLE001 - recorded, never fatal
            return {"resolved": "UNRESOLVED", "reason": str(exc)}

        cfg = resolve_fusion(self.fusion)
        described: dict[str, object] = {
            "strategy": cfg.strategy,
            "pipeline_id": search_pipeline_id(cfg),
            # "the deployment default happened to be RRF" and "this arm asked
            # for RRF" are different claims about the same numbers.
            "selected_explicitly": self.fusion is not None,
        }
        if cfg.strategy == RRF:
            described["rank_constant"] = cfg.rank_constant
        else:
            described["normalization_technique"] = cfg.normalization_technique
            described["combination_technique"] = cfg.combination_technique
            described["weights"] = list(cfg.weights) if cfg.weights is not None else None
        return described

    def as_dict(self) -> dict[str, object]:
        base: dict[str, object] = {
            "path": "chat retrieval (app.services.search.chunk_retrieval.retrieve_chunks)",
            "stage": self.stage,
            "candidate_pool": self.size,
            "search_mode": self.search_mode,
            "scope": self.scope,
            "workers": self.workers,
            "fusion": self.fusion_provenance(),
            "scope_note": (
                "corpus-wide, file_uuids=None"
                if self.scope == "corpus"
                else "ORACLE: file_uuids = the query's own gold files (upper bound)"
            ),
            "llm_required": False,
        }
        if self.stage == "route":
            base.update(
                {
                    "digest_size": self.digest_size,
                    "ranked_plane": (
                        "chunk only — digest hits are measured as FILE SELECTION, not "
                        "mixed into the ranking (see RouteRecord)"
                    ),
                }
            )
        if self.stage == "rerank":
            base.update(
                {
                    "rerank_max_pairs": self.rerank_max_pairs,
                    "max_chunks_per_file": self.max_per_file,
                    "final_chunks": self.final_chunks,
                }
            )
        return base


@dataclass(frozen=True)
class RouteRecord:
    """What the router did for one query, and what each tier actually returned.

    The digest leg is recorded **beside** the ranked list rather than merged into
    it, and that is the central measurement decision of this stage.

    Mixing digest documents into the ranking would look like the obvious thing to
    do and would be wrong twice over. The qrels judge *chunks* — they are built
    by mapping gold turn spans onto whatever chunks the indexer produced — so a
    digest document is unjudged, scores 0, and pushes real relevant chunks down
    the list. The routed run would then score WORSE than the control by
    construction, and the number would be an artefact of the judgement space
    rather than a fact about retrieval. Fixing that by judging digests too would
    mean inventing a second relevance rule mid-epic, which is how a qrels file
    stops meaning anything.

    So the ranked list stays chunk-only and comparable to the control, and the
    digest leg is measured for the thing it is actually claimed to do. Stage 1
    established that with an oracle gold-file scope nDCG@10 goes 0.1052 ->
    0.3296 — **roughly two thirds of the loss is file selection**, which is the
    entire argument for a digest tier. So the question this record answers is
    "did the digest leg surface a gold FILE the chunk leg missed", not "where did
    a digest rank".
    """

    intent: str
    tiers: tuple[str, ...]
    #: Files the digest leg surfaced, best first. Empty when the router did not
    #: ask for the digest tier — which is not the same as the tier returning
    #: nothing, and ``tiers`` is what distinguishes them.
    digest_files: tuple[str, ...] = ()
    #: Files the chunk leg surfaced, best first, deduplicated.
    chunk_files: tuple[str, ...] = ()


def _ordered_files(hits) -> tuple[str, ...]:
    """File uuids in rank order, first occurrence wins, no duplicates."""
    seen: dict[str, None] = {}
    for hit in hits:
        seen.setdefault(str(hit.file_uuid), None)
    return tuple(seen)


def _to_run_docs(hits, *, preserve_order: bool = False) -> list[RunDoc]:
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

    ⚠️ **``preserve_order`` exists because scoring the rerank stage by SCORE
    measured a pipeline that does not exist.** ``normalise_run`` re-sorts every
    run by ``-score``, which is right for ``retrieve`` (OpenSearch already
    returns score order, and the re-sort only makes the tie-break id-blind) and
    **wrong** after ``diversity_sample``, whose whole job is to interleave files
    so one recording cannot crowd out the rest. Re-sorting by score undoes that
    interleaving, so the metric saw a score-ordered list the model is never
    given. Measured over 60 synthetic queries: the re-sort changed the prompt
    order for **40**, and made the scored top-5 depend on ``final_chunks`` — a
    parameter that provably cannot move the prompt's top-5, since
    ``diversity_sample`` is prefix-invariant in its ``cap`` — for **23**.

    A second defect went with it. ``rerank`` writes the cross-encoder score back
    onto its first ``max_pairs`` hits and leaves the tail carrying RRF scores;
    cross-encoder scores are routinely **negative** while RRF scores are small
    positives, so with ``candidate_pool > rerank_max_pairs`` a score sort floats
    the *un-reranked* tail above every reranked document. Production never had
    this bug — ``diversity_sample`` walks list order — it was purely the
    harness reading a ranking out of two incomparable score scales.

    With ``preserve_order`` the score is derived from the hit's **position**, so
    the metric ranks exactly what the prompt receives and no tie can occur.

    Args:
        hits: Chunk hits in the order the stage produced them.
        preserve_order: Score by rank rather than by the hit's own score. Set
            for stages whose output order is deliberate rather than score-sorted.

    Returns:
        Run documents in input order.
    """
    from app.services.ingest_artifacts.index_mapping import DOC_TYPE_CHUNK
    from app.services.ingest_artifacts.index_mapping import DOC_TYPE_DIGEST
    from app.services.ingest_artifacts.index_mapping import digest_document_id

    total = len(hits)
    docs: list[RunDoc] = []
    for position, hit in enumerate(hits):
        chunk_index = int(hit.chunk_index)
        is_digest = chunk_index < 0
        docs.append(
            RunDoc(
                doc_id=(
                    digest_document_id(hit.file_uuid, -1 - chunk_index)
                    if is_digest
                    else f"{hit.file_uuid}_{chunk_index}"
                ),
                score=float(total - position) if preserve_order else float(hit.score),
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
    records: dict[str, RouteRecord] | None = None,
    retrieval_ms: list[float] | None = None,
) -> dict[str, list[RunDoc]]:
    """Run every query through the production retriever.

    Args:
        queries: Scoreable queries, in a deterministic order.
        user_id: Owner of the injected corpus — enforced by the index filter.
        config: Retrieval knobs, pinned by the harness.
        organization_id: Tenant, or None for personal scope.
        progress_every: Log cadence; does not affect results.
        records: Optional out-parameter, filled by ``stage='route'`` with one
            :class:`RouteRecord` per query. Follows the same shape as
            ``prompting.build_messages(diagnostics=...)`` rather than changing
            the return type, so every existing caller is unaffected.
        retrieval_ms: Optional out-parameter collecting the wall-clock cost of
            each ``retrieve_chunks`` call. #383 Phase 7 gates each A/B on
            "nDCG@10, recall and **p95 added latency**", and a fusion processor
            is exactly the kind of change that can buy relevance with time.
            Only the chunk leg is timed: a rerank or digest cost belongs to
            those stages, not to the fusion arm under test. The caller writes
            it to ``runinfo.json``, never to ``metrics.json`` — a duration
            cannot be byte-identical across runs, and the deterministic
            document must stay deterministic.

    Returns:
        ``query_id -> [RunDoc]`` in provider-ranked order. A query that retrieved
        nothing is present with an empty list, so the caller can tell "answered
        with nothing" from "never asked".

    Raises:
        RuntimeError: If ``stage='rerank'`` and the cross-encoder is unavailable.
            Silently skipping the rerank would report a rerank number that never
            reranked anything. Also if ``stage='route'`` is asked for without a
            ``records`` dict: the routed run's whole reason to exist is the
            digest-leg evidence, and a caller that drops it would get numbers
            byte-identical to the control under a different name — the exact
            "control wearing a new name" failure this stage was added to avoid.
    """
    if config.stage not in STAGES:
        raise ValueError(f"Unknown stage {config.stage!r}; expected one of {STAGES}")
    if config.scope not in SCOPES:
        raise ValueError(f"Unknown scope {config.scope!r}; expected one of {SCOPES}")

    if config.stage == "route" and records is None:
        raise RuntimeError(
            "stage='route' needs a records dict to write the digest-leg evidence into; "
            "without it the run is byte-identical to stage='retrieve' under another name."
        )

    from app.services.search.chunk_retrieval import diversity_sample
    from app.services.search.chunk_retrieval import retrieve_chunks

    router = None
    retrieve_digests_fn = None
    if config.stage == "route":
        from app.services.chat.router import route as route_fn
        from app.services.search.chunk_retrieval import retrieve_digests

        router = route_fn
        retrieve_digests_fn = retrieve_digests

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
        started = time.perf_counter()
        hits = retrieve_chunks(
            query.text,
            user_id=user_id,
            organization_id=organization_id,
            file_uuids=scope,
            size=config.size,
            search_mode=config.search_mode,
            fusion=config.fusion,
        )
        if retrieval_ms is not None:
            # list.append is atomic under the GIL, so the worker pool needs no
            # lock; order is not meaningful anyway — the caller takes quantiles.
            retrieval_ms.append((time.perf_counter() - started) * 1000.0)
        if router is not None:
            # The production router, on the production question text. No
            # harness-side classifier: a second copy would drift, and then the
            # measurement would be of the copy.
            decision = router(query.text)
            digests = []
            if decision.wants_digest and retrieve_digests_fn is not None:
                digests = retrieve_digests_fn(
                    query.text,
                    user_id=user_id,
                    organization_id=organization_id,
                    file_uuids=scope,
                    size=config.digest_size,
                    search_mode=config.search_mode,
                    fusion=config.fusion,
                )
            assert records is not None  # guaranteed by the guard above
            records[query.query_id] = RouteRecord(
                intent=decision.intent,
                tiers=tuple(decision.tiers),
                digest_files=_ordered_files(digests),
                chunk_files=_ordered_files(hits),
            )
        if reranker is not None:
            hits = reranker(query.text, hits, max_pairs=config.rerank_max_pairs)
            hits = diversity_sample(hits, max_per_file=config.max_per_file, cap=config.final_chunks)
        # Only the rerank stage produces an order that is deliberate rather than
        # score-sorted; see `_to_run_docs`.
        return query.query_id, _to_run_docs(hits, preserve_order=reranker is not None)

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
