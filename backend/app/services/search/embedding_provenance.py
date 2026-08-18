"""Which model produced the vectors in the transcript chunks index (issue #437).

Cosine similarity between vectors from two different embedding models is not a
similarity — it is a number. Hybrid search will happily rank two such populations
against each other, and nothing about the result looks wrong. So the index needs
to be able to answer "which model embedded this document?", and the answer needs
to be derivable from the documents themselves rather than from a second copy of
the configuration that can drift away from them.

The ``embedding_model`` keyword field already existed for exactly this and was
being written with the string ``"neural"`` — the embedding *mode*, not the model.
Measured on the epic's index before this module: 210,908 documents, cardinality
**1**, the single value ``"neural"``. A constant distinguishes nothing.

**Provenance keys off the ingest pipeline, never off the settings table.** There
are two SystemSettings keys — ``search.embedding_model`` (the HuggingFace name,
which drives the index's ``knn_vector`` dimension) and
``search.opensearch_model_id`` (the ML Commons id, which drives the pipeline) —
and nothing keeps them consistent: ``POST /search/models`` wrote only the first
and ``PUT /search/models/neural/active`` only the second. A label read from the
settings table can therefore name a model that never touched the vector it is
attached to, which is worse than no label. :func:`active_embedding_model` returns
what :func:`~app.services.search.indexing_service.ensure_neural_ingest_pipeline`
resolved from the pipeline it actually wrote.

**``"neural"`` means UNKNOWN, and keeps meaning it.** Every document indexed
before this module carries it, and there is no way to find out afterwards which
model produced those vectors — the model id is not recorded anywhere per
document, and re-deriving it would mean re-embedding, which is the thing we are
trying to avoid having to do blindly. So ``"neural"`` is kept as the one unknown
bucket rather than being replaced by a new sentinel (which would split the
unknown population in two for no gain) and rather than being backfilled with the
current model (which would assert something we cannot know). New writes that
cannot resolve a model land in that same bucket.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Any

logger = logging.getLogger(__name__)

#: The value ``embedding_model`` carries when the model behind the vector is not
#: known. It is deliberately the literal string every pre-#437 document already
#: holds, so "unknown" is one bucket and not two.
EMBEDDING_MODEL_UNKNOWN = "neural"

#: Aggregation bucket for documents with no ``embedding_model`` at all. Those are
#: the text-only writes (``embedding_model = None`` when the neural pipeline is
#: unavailable): they have no vector, so they are not part of the mixed-vector
#: question — they are a *different* problem, invisible to kNN entirely.
EMBEDDING_MODEL_ABSENT = "__no_embedding__"

_lock = threading.Lock()
_active_model: str = EMBEDDING_MODEL_UNKNOWN


def set_active_embedding_model(label: str | None) -> None:
    """Record the model the neural ingest pipeline was just pointed at.

    Called from the one place that writes the pipeline, with the model id it
    wrote, so the label and the pipeline cannot disagree.

    Args:
        label: Model name to attribute new documents to, or ``None`` when it
            could not be resolved — which stores :data:`EMBEDDING_MODEL_UNKNOWN`
            rather than leaving the previous model's name in place.
    """
    global _active_model
    with _lock:
        _active_model = label or EMBEDDING_MODEL_UNKNOWN


def reset_active_embedding_model() -> None:
    """Forget the resolved model. Paired with ``reset_neural_pipeline_state``."""
    set_active_embedding_model(None)


def active_embedding_model() -> str:
    """The label to stamp on documents being indexed now.

    Returns:
        A model name, or :data:`EMBEDDING_MODEL_UNKNOWN` when the pipeline has
        not been verified in this process or its model could not be named.
    """
    with _lock:
        return _active_model


def resolve_model_label(model_id: str | None) -> str | None:
    """Turn an ML Commons model id into the model name that identifies the vectors.

    The **name** is the provenance that matters, not the id: registering the same
    model twice produces two ids and identical vectors, so an id-keyed label would
    report a mixed index where there is none. The name is what
    ``OPENSEARCH_EMBEDDING_MODELS`` is keyed by and what an operator recognises.

    Args:
        model_id: ML Commons model id the ingest pipeline was written with.

    There is deliberately **no** ``try``/``except`` here. Both calls below already
    absorb their own failures and return an empty result:
    ``get_ml_model_service()`` constructs with ``get_opensearch_client()``, which
    catches and returns ``None``, and ``get_model_status`` catches every exception
    and returns ``{}``. A guard here would be dead code. Should either ever start
    propagating, both call sites sit inside ``ensure_neural_ingest_pipeline``'s own
    handler, which marks the pipeline unavailable — a strictly better outcome than
    a swallow that would leave it believing the model was resolved.

    Returns:
        The model name, or ``None`` if it cannot be resolved — callers must treat
        that as unknown and must not substitute the configured model, which is
        the value that is demonstrably able to disagree with the pipeline.
    """
    if not model_id:
        return None

    from app.services.search.ml_model_service import get_ml_model_service

    name = get_ml_model_service().get_model_status(model_id).get("name")
    if not name:
        logger.warning(f"ML model {model_id} reports no name; provenance will be unknown")
        return None
    return str(name)


@dataclass(frozen=True)
class EmbeddingProvenance:
    """What the chunks index says about the models behind its vectors.

    Attributes:
        verdict: One of ``unavailable`` / ``empty`` / ``unattributed`` /
            ``uniform`` / ``partially_unattributed`` / ``mixed``.
        counts: Bucket label to document count, including the unknown bucket and
            :data:`EMBEDDING_MODEL_ABSENT`.
        known_models: The labels that actually name a model, sorted.
        total: Documents surveyed.
        unattributed: Documents in the :data:`EMBEDDING_MODEL_UNKNOWN` bucket.
        no_embedding: Documents with no ``embedding_model`` field at all.
        mixed: True only when two or more *named* models are present. Proven, not
            suspected.
    """

    verdict: str
    counts: dict[str, int] = field(default_factory=dict)
    known_models: tuple[str, ...] = ()
    total: int = 0
    unattributed: int = 0
    no_embedding: int = 0
    mixed: bool = False

    @property
    def comparable(self) -> bool:
        """Whether every vector in the index is provably from one model."""
        return self.verdict in {"empty", "uniform"}

    def _named(self, limit: int = 3) -> str:
        """The model names, abbreviated — the aggregation returns up to 50."""
        if len(self.known_models) <= limit:
            return ", ".join(self.known_models)
        shown = ", ".join(self.known_models[:limit])
        return f"{shown} and {len(self.known_models) - limit} more"

    def describe(self) -> str:
        """One line an operator can act on."""
        if self.verdict == "unavailable":
            return "Embedding provenance could not be surveyed (index unreachable)."
        if self.verdict == "empty":
            return "The chunks index is empty."
        if self.verdict == "uniform":
            return f"All {self.total} documents were embedded by {self.known_models[0]}."
        if self.verdict == "unattributed":
            return (
                f"All {self.total} documents predate embedding-model provenance "
                f"(#437); which model produced their vectors is unknowable."
            )
        if self.verdict == "partially_unattributed":
            return (
                f"{self.unattributed} of {self.total} documents predate embedding-model "
                f"provenance (#437), so they cannot be proven comparable with the "
                f"{self.total - self.unattributed} embedded by "
                f"{self._named()}. A full reindex resolves it."
            )
        return (
            f"MIXED VECTOR SPACE: {self.total} documents span "
            f"{len(self.known_models)} embedding models ({self._named()}). "
            f"Cosine similarity between them is meaningless and hybrid search is "
            f"ranking them against each other. A full reindex is required."
        )


def _classify(counts: dict[str, int], known: list[str], unattributed: int, total: int) -> str:
    """Pick the verdict. Split out so the rule is one readable expression."""
    if total == 0:
        return "empty"
    if len(known) > 1:
        return "mixed"
    if not known:
        return "unattributed"
    return "partially_unattributed" if unattributed else "uniform"


def survey_embedding_models(index_name: str | None = None) -> EmbeddingProvenance:
    """Ask the index which models its vectors came from. One aggregation.

    ``mixed`` requires **two named models**. A single named model beside the
    legacy unknown bucket is ``partially_unattributed``, not ``mixed``: the
    unknown documents *might* be from that same model, and calling that a mixed
    index would fire on every existing deployment the first time it indexed
    anything after this change — an alarm that is wrong for the common case
    trains people to ignore the alarm that is right for the dangerous one.

    Args:
        index_name: Index to survey. Defaults to the configured chunks index.

    Only ``OpenSearchException`` is caught, and that is the measured set rather
    than a guess: both calls below raise ``opensearchpy.exceptions.ConnectionError``
    against an unreachable cluster, which is a ``TransportError`` and therefore an
    ``OpenSearchException``. Anything else — a malformed body, a bug in the parsing
    that follows — is a defect in this function, and a probe that reported its own
    defect as ``unavailable`` would be one more signal that cannot be told apart
    from a working system.

    Returns:
        An :class:`EmbeddingProvenance`. A failed query yields ``unavailable``
        and never ``uniform`` — "I could not ask" must not read as "all clear".
    """
    from opensearchpy.exceptions import OpenSearchException

    from app.core.config import settings
    from app.services.opensearch_service import opensearch_client

    index = index_name or settings.OPENSEARCH_CHUNKS_INDEX

    if not opensearch_client:
        return EmbeddingProvenance(verdict="unavailable")

    try:
        if not opensearch_client.indices.exists(index=index):
            return EmbeddingProvenance(verdict="empty")
        response: dict[str, Any] = opensearch_client.search(
            index=index,
            body={
                "size": 0,
                "aggs": {
                    "models": {
                        "terms": {
                            "field": "embedding_model",
                            "size": 50,
                            "missing": EMBEDDING_MODEL_ABSENT,
                        }
                    }
                },
            },
        )
    except OpenSearchException as e:
        logger.warning(f"Could not survey embedding provenance on {index}: {e}")
        return EmbeddingProvenance(verdict="unavailable")

    agg = response.get("aggregations", {}).get("models", {})
    counts = {str(b["key"]): int(b["doc_count"]) for b in agg.get("buckets", [])}
    total = sum(counts.values()) + int(agg.get("sum_other_doc_count", 0) or 0)

    unattributed = counts.get(EMBEDDING_MODEL_UNKNOWN, 0)
    no_embedding = counts.get(EMBEDDING_MODEL_ABSENT, 0)
    known = sorted(
        label for label in counts if label not in {EMBEDDING_MODEL_UNKNOWN, EMBEDDING_MODEL_ABSENT}
    )

    verdict = _classify(counts, known, unattributed, total)
    return EmbeddingProvenance(
        verdict=verdict,
        counts=counts,
        known_models=tuple(known),
        total=total,
        unattributed=unattributed,
        no_embedding=no_embedding,
        mixed=verdict == "mixed",
    )


# --------------------------------------------------------------------------- #
# The advisory a SEARCH response carries (#437 follow-up)                      #
# --------------------------------------------------------------------------- #
#
# `survey_embedding_models` had three readers — the status endpoint, `model_switch`,
# and a beat-task log — and NONE of them is the person reading the results. So a
# PROVEN mixed index went on ranking two incomparable vector populations against
# each other with no signal to anyone looking at the answers. "Do not dispatch a
# re-embed from a beat tick" is a sound argument; "do not tell the reader" is a
# different decision that was never argued.
#
# It is a deployment-level fact, not per-query data, and it changes only after a
# model switch or a reindex — so it is cached rather than surveyed per request.
# The survey is 2.5-6.7 ms against a 210k-document index; paying that on every
# search would be a real regression on the hottest path in the app.
_ADVISORY_TTL_SECONDS = 60.0
_advisory_cache: tuple[float, dict[str, Any] | None] | None = None
_advisory_lock = threading.Lock()


def search_provenance_advisory() -> dict[str, Any] | None:
    """A compact warning for the search response, or ``None`` when all is well.

    Returns:
        ``None`` when the index is *comparable* (``empty`` or ``uniform``) — the
        overwhelmingly common case, so an ordinary search response is unchanged.
        Otherwise a small dict naming the verdict and the models involved.

        ``unattributed`` and ``partially_unattributed`` deliberately return
        ``None`` too: every deployment enters those states the moment it indexes
        anything after #437 landed, and an advisory that fires for everybody is
        one people learn to ignore before it fires for a real mixed index.
    """
    global _advisory_cache

    now = time.monotonic()
    with _advisory_lock:
        if _advisory_cache is not None and now < _advisory_cache[0]:
            return _advisory_cache[1]

    survey = survey_embedding_models()
    advisory: dict[str, Any] | None = None
    if survey.verdict == "mixed":
        advisory = {
            "code": "mixed_embedding_models",
            "verdict": survey.verdict,
            "models": list(survey.known_models),
            "message": survey.describe(),
        }

    with _advisory_lock:
        _advisory_cache = (now + _ADVISORY_TTL_SECONDS, advisory)
    return advisory


def reset_search_provenance_advisory() -> None:
    """Drop the cached advisory. For tests and for the model-switch path."""
    global _advisory_cache

    with _advisory_lock:
        _advisory_cache = None
