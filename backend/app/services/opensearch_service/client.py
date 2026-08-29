"""OpenSearch client construction and low-level index primitives.

Owns the process-wide ``opensearch_client`` singleton. Every other module in
this package reads it through this module (``client.opensearch_client``) so the
lazy re-initialisation performed by :func:`get_opensearch_client` is visible
everywhere.

The index-introspection helpers below deliberately separate **"the thing is
absent"** from **"the cluster could not answer"**. Collapsing the two (the
previous ``except Exception: return False``) made a misconfigured or
unreachable cluster indistinguishable from an empty one — and callers use
these answers to decide whether to create, alias, or *delete* an index.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

from opensearchpy import OpenSearch
from opensearchpy import RequestsHttpConnection
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError
from opensearchpy.exceptions import ImproperlyConfigured
from opensearchpy.exceptions import NotFoundError
from opensearchpy.exceptions import SerializationError
from opensearchpy.exceptions import TransportError

from app.core.config import settings
from app.core.opensearch_auth import opensearch_connection_kwargs

logger = logging.getLogger(__name__)


class OpenSearchUnavailableError(RuntimeError):
    """The cluster could not answer a request (connection/auth/transport).

    Raised by the index-introspection helpers instead of returning a
    "not present" answer, so a broken cluster can never be mistaken for an
    empty one by index bootstrap, alias migration, or index deletion logic.
    """


# Errors that mean "the cluster could not answer", as opposed to NotFoundError
# which means "the cluster answered: that index/alias does not exist".
# NotFoundError also subclasses TransportError, so it must be caught FIRST.
CLUSTER_UNAVAILABLE_ERRORS = (
    OpenSearchConnectionError,  # includes ConnectionTimeout and SSLError
    TransportError,  # includes Authentication/Authorization/Request/Conflict
    SerializationError,
    ImproperlyConfigured,
    OSError,  # builtin socket/DNS failures raised below opensearch-py
)


# Initialize the OpenSearch client (skipped when OPENSEARCH_ENABLED=false)
opensearch_client: OpenSearch | None
if not settings.OPENSEARCH_ENABLED:
    logger.info("OpenSearch is disabled (OPENSEARCH_ENABLED=false), skipping client initialization")
    opensearch_client = None
else:
    try:
        opensearch_client = OpenSearch(
            **opensearch_connection_kwargs(connection_class=RequestsHttpConnection)
        )
        logger.info("OpenSearch client initialized successfully")
    except (ImproperlyConfigured, OpenSearchConnectionError, ValueError, OSError) as e:
        logger.error(f"Configuration error initializing OpenSearch client: {e}")
        opensearch_client = None
    except Exception:
        # Construction is pure config parsing; anything else is unexpected, but
        # a bad OpenSearch config must not prevent the process from booting.
        logger.exception("Unexpected error initializing OpenSearch client")
        opensearch_client = None


# Lazy singleton for SentenceTransformer model (~80MB) — loaded once.
_sentence_transformer_model = None


def _speaker_org_filter_clauses(organization_id: int | None) -> list[dict[str, Any]]:
    """Tenant-scope ``bool.filter`` clauses for speaker/voiceprint kNN queries.

    Delegates to the shared search-plane helper so the SQL, transcript-search,
    and speaker-search planes all encode tenancy identically. Org context -> a
    ``term`` on ``organization_id``; personal scope (None) -> exclude any doc
    that carries ``organization_id`` (so org voiceprints never match a personal
    search). Community-edition: callers pass None and docs are org-less, so the
    personal gate is a behavior-preserving no-op.
    """
    from app.services.search.tenant_scope import org_filter_clauses

    return org_filter_clauses(organization_id)


# Bounds a stalled Hub round trip (~80MB model, normally served from local disk
# cache) rather than the load itself — see app/utils/hf_hub_offline.py.
_SENTENCE_TRANSFORMER_LOAD_TIMEOUT_S = 30.0


def _get_sentence_transformer():
    """Lazy singleton for SentenceTransformer model."""
    global _sentence_transformer_model
    if _sentence_transformer_model is None:
        from sentence_transformers import SentenceTransformer

        from app.utils.hf_hub_offline import hf_offline_requested
        from app.utils.hf_hub_offline import load_with_timeout

        kwargs: dict[str, Any] = {}
        if hf_offline_requested():
            kwargs["local_files_only"] = True

        _sentence_transformer_model = load_with_timeout(
            lambda: SentenceTransformer("all-MiniLM-L6-v2", **kwargs),
            timeout=_SENTENCE_TRANSFORMER_LOAD_TIMEOUT_S,
            label="OpenSearch query-embedding SentenceTransformer",
        )
    return _sentence_transformer_model


def get_opensearch_client() -> "OpenSearch | None":
    """Get the OpenSearch client, attempting lazy initialization if None.

    If the client was not initialized at module load time (e.g., OpenSearch
    was not yet available), this function attempts to create a new client.

    Returns:
        OpenSearch client instance, or None if connection fails.
    """
    global opensearch_client
    if opensearch_client is not None:
        return opensearch_client

    try:
        opensearch_client = OpenSearch(
            **opensearch_connection_kwargs(connection_class=RequestsHttpConnection)
        )
        logger.info("OpenSearch client lazily initialized successfully")
        return opensearch_client
    except (ImproperlyConfigured, OpenSearchConnectionError, ValueError, OSError) as e:
        logger.warning(f"Lazy OpenSearch client initialization failed: {e}")
        return None


def _is_alias(name: str) -> bool:
    """Report whether ``name`` resolves to an alias rather than a concrete index.

    Args:
        name: Alias or index name to test.

    Returns:
        True if ``name`` is an alias; False if the cluster answered that it is
        not (absent, or a concrete index).

    Raises:
        OpenSearchUnavailableError: The cluster could not answer.
    """
    if not opensearch_client:
        return False
    try:
        return bool(opensearch_client.indices.exists_alias(name=name))
    except NotFoundError:
        return False
    except CLUSTER_UNAVAILABLE_ERRORS as e:
        raise OpenSearchUnavailableError(f"Could not check alias '{name}': {e}") from e


def _get_alias_target(alias_name: str) -> str | None:
    """Resolve the concrete index an alias points to.

    Args:
        alias_name: Alias to resolve.

    Returns:
        The concrete index name, or None if ``alias_name`` is not an alias.

    Raises:
        OpenSearchUnavailableError: The cluster could not answer.
    """
    if not opensearch_client:
        return None
    try:
        result = opensearch_client.indices.get_alias(name=alias_name)
        # result is {concrete_index_name: {aliases: {alias_name: {}}}}
        indices = list(result.keys())
        return indices[0] if indices else None
    except NotFoundError:
        return None
    except CLUSTER_UNAVAILABLE_ERRORS as e:
        raise OpenSearchUnavailableError(f"Could not resolve alias '{alias_name}': {e}") from e


def _safe_index_exists(index_name: str) -> bool:
    """Report whether a concrete index exists.

    Args:
        index_name: Index to test.

    Returns:
        True if the index exists, False if the cluster answered that it does not.

    Raises:
        OpenSearchUnavailableError: The cluster could not answer. Callers must
            not treat this as "absent" — see the module docstring.
    """
    if not opensearch_client:
        return False
    try:
        return bool(opensearch_client.indices.exists(index=index_name))
    except NotFoundError:
        return False
    except CLUSTER_UNAVAILABLE_ERRORS as e:
        raise OpenSearchUnavailableError(f"Could not check index '{index_name}': {e}") from e


def _get_index_embedding_dimension(index_name: str) -> int | None:
    """Read the ``embedding`` knn_vector dimension from an index mapping.

    Args:
        index_name: Index whose mapping to read.

    Returns:
        The declared dimension, or None when the index has no ``embedding``
        field, no declared dimension, or does not exist.

    Raises:
        OpenSearchUnavailableError: The cluster could not answer.
    """
    if not opensearch_client:
        return None
    try:
        mapping = opensearch_client.indices.get_mapping(index=index_name)
    except NotFoundError:
        return None
    except CLUSTER_UNAVAILABLE_ERRORS as e:
        raise OpenSearchUnavailableError(f"Could not read mapping for '{index_name}': {e}") from e

    props = mapping.get(index_name, {}).get("mappings", {}).get("properties", {})
    dim = props.get("embedding", {}).get("dimension")
    if dim is None:
        return None
    try:
        return int(dim)
    except (TypeError, ValueError):
        logger.warning(f"Index '{index_name}' declares a non-numeric embedding dimension: {dim!r}")
        return None


def _resolve_concrete_index(name: str) -> str:
    """Resolve *name* to the concrete index a mapping response is keyed by.

    ``indices.get_mapping(index=<alias>)`` answers with the **concrete** index
    as the top-level key, so any code doing ``mapping.get(name)`` silently reads
    ``{}`` when handed an alias — and this deployment reaches the speaker plane
    through the ``speakers`` alias.

    Args:
        name: An alias or a concrete index name.

    Returns:
        The concrete index behind an alias, or *name* unchanged when it is
        already concrete or cannot be resolved.
    """
    try:
        if _is_alias(name):
            return _get_alias_target(name) or name
    except OpenSearchUnavailableError:
        return name
    return name


def _supports_ann_search(index_name: str) -> bool:
    """Report whether ``embedding`` on *index_name* can serve an ANN ``knn`` query.

    A ``knn_vector`` field is **not** automatically ANN-searchable. Declared
    without a ``method`` block it has no HNSW graph, supports only exact
    script-score scoring, and rejects an ANN ``knn`` query with
    ``400 … Field 'embedding' is not built for ANN search``. The legacy
    ``transcripts`` index in this deployment is exactly that shape.

    This distinction is load-bearing: that 400 carries the string
    ``search_phase_execution_exception``, which
    :func:`_is_index_corruption_error` matches — so without this check a
    perfectly intact index reads as corrupt and the health check rebuilds it on
    every tick.

    Args:
        index_name: Index whose mapping to read.

    Returns:
        True when ``embedding`` declares a kNN ``method`` (i.e. an ANN index).

    Raises:
        OpenSearchUnavailableError: The cluster could not answer.
    """
    if not opensearch_client:
        return False
    concrete = _resolve_concrete_index(index_name)
    try:
        mapping = opensearch_client.indices.get_mapping(index=concrete)
    except NotFoundError:
        return False
    except CLUSTER_UNAVAILABLE_ERRORS as e:
        raise OpenSearchUnavailableError(f"Could not read mapping for '{concrete}': {e}") from e

    embedding = (
        mapping.get(concrete, {}).get("mappings", {}).get("properties", {}).get("embedding", {})
    )
    return embedding.get("type") == "knn_vector" and bool(embedding.get("method"))


#: OpenSearch's rejection when ``embedding`` is a ``knn_vector`` with no ANN
#: method. Lower-cased for substring matching. This is a MAPPING statement, not
#: a health one — see :func:`probe_knn_health`.
_ANN_UNSUPPORTED_MARKER = "not built for ann search"


def _is_index_corruption_error(error: Exception) -> bool:
    """Check if an exception indicates OpenSearch index corruption or wrong mapping.

    ``index_closed_exception`` is included beside the pre-existing
    ``already_closed``: they are the same fault reported by two different layers
    (the coordinating node vs. the shard), and the existing close/reopen repair
    strategy fixes both. Without it a closed index fell through to "unknown" and
    no repair was ever attempted.
    """
    error_str = str(error).lower()
    return any(
        indicator in error_str
        for indicator in [
            "503",
            "search_phase_execution_exception",
            "already_closed",
            "index_closed_exception",
            "no_shard_available",
            "not knn_vector type",
        ]
    )


@dataclass(frozen=True)
class KnnProbeResult:
    """Verdict of a single cheap kNN query against one index.

    Attributes:
        status: One of ``healthy``, ``empty``, ``absent``, ``unsupported``,
            ``unknown``, ``corrupt``.
        detail: Human-readable context, present for the non-healthy verdicts.
        latency_ms: Round-trip time of the probe query, when one was issued.
    """

    status: str
    detail: str | None = None
    latency_ms: float | None = None

    @property
    def is_serviceable(self) -> bool:
        """True when the vector plane answered, or has nothing to answer with.

        An **empty** index is serviceable: it has nothing to return, which is not
        a fault. ``unsupported`` counts too — the index is intact, it simply does
        not host an ANN graph, so an ANN probe says nothing about its health and
        must never be read as a fault.

        ``absent`` and ``unknown`` are deliberately excluded: neither is evidence
        that the vector plane works.
        """
        return self.status in ("healthy", "empty", "unsupported")

    @property
    def is_corrupt(self) -> bool:
        """True only for a positively identified corrupt vector plane.

        Repair is destructive, so it keys off this rather than off
        ``not is_serviceable`` — the latter would also fire for ``absent`` and
        ``unknown``, neither of which repair can help.
        """
        return self.status == "corrupt"


def probe_knn_health(index_name: str) -> KnnProbeResult:
    """Ask the vector plane of *index_name* a question only it can answer.

    Registry state is not runtime truth (issue #540): a deployment can report a
    configured neural pipeline, a deployed ML model and a healthy BM25 plane
    while **every** kNN query answers ``503 search_phase_execution_exception``
    because the HNSW segments are corrupt. Lucene's text segments are crash-safe;
    the vector files are the fragile part, so a ``match_all`` probe — what
    :func:`~app.services.opensearch_service.repair.check_and_repair_indices` used
    to rely on — cannot see this failure at all.

    The query is a **literal** vector, never a ``neural`` query. A neural query
    round-trips through ML Commons text embedding, so a failure would be
    ambiguous between "the embedding model is down" and "the vector segments are
    corrupt" — and those have opposite remedies.

    Args:
        index_name: Concrete index to probe. Not an alias: the mapping read
            underneath is keyed by concrete index name.

    Returns:
        A :class:`KnnProbeResult`. ``corrupt`` is returned **only** for a 5xx
        error :func:`_is_index_corruption_error` recognises; anything else is
        ``unsupported`` or ``unknown``. Repair deletes an index on ``corrupt``,
        so over-reporting it is far more damaging than under-reporting.
    """
    if not opensearch_client:
        return KnnProbeResult("unknown", detail="OpenSearch client not initialized")

    try:
        if not _safe_index_exists(index_name):
            return KnnProbeResult("absent", detail=f"Index '{index_name}' does not exist")
        # Mapping reads are keyed by concrete index; the kNN query itself is
        # happy against an alias.
        dimension = _get_index_embedding_dimension(_resolve_concrete_index(index_name))
        ann_capable = _supports_ann_search(index_name)
    except OpenSearchUnavailableError as e:
        return KnnProbeResult("unknown", detail=str(e))

    if dimension is None:
        return KnnProbeResult(
            "unsupported", detail=f"Index '{index_name}' declares no knn_vector dimension"
        )
    if not ann_capable:
        return KnnProbeResult(
            "unsupported",
            detail=f"Index '{index_name}' has a knn_vector field with no ANN method",
        )

    # A unit vector along the first axis. Direction is irrelevant — the probe
    # asks whether the graph can be traversed at all, not what it returns.
    vector = [1.0] + [0.0] * (dimension - 1)
    body = {
        "size": 0,
        "_source": False,
        "query": {"knn": {"embedding": {"vector": vector, "k": 1}}},
    }

    started = time.monotonic()
    try:
        opensearch_client.search(index=index_name, body=body)
    except Exception as e:  # noqa: BLE001 - classified below; see the docstring
        # "This field cannot serve an ANN query" is a statement about the
        # MAPPING, not the segments — the index is intact. It must be checked
        # first and matched NARROWLY: the rejection is a 400 carrying the
        # literal string `search_phase_execution_exception`, so
        # `_is_index_corruption_error` matches it and would report a healthy
        # index as corrupt.
        #
        # Deliberately not a blanket "any 4xx is fine": `index_closed_exception`
        # is also a 400 and is a real fault, so treating the whole class as
        # benign would report a closed index as serviceable.
        if _ANN_UNSUPPORTED_MARKER in str(e).lower():
            return KnnProbeResult("unsupported", detail=str(e))
        if _is_index_corruption_error(e):
            return KnnProbeResult("corrupt", detail=str(e))
        return KnnProbeResult("unknown", detail=str(e))
    latency_ms = (time.monotonic() - started) * 1000.0

    # A kNN query against an empty index succeeds exactly like one against a
    # populated index, so the hit count cannot tell them apart — only a doc
    # count can. Getting this wrong reports a freshly rebuilt index as corrupt
    # and rebuilds it again, forever.
    try:
        count = int(opensearch_client.count(index=index_name).get("count", 0))
    except Exception as e:  # noqa: BLE001 - the probe itself already succeeded
        logger.warning(f"kNN probe of '{index_name}' could not read a doc count: {e}")
        return KnnProbeResult("healthy", latency_ms=latency_ms)

    if count == 0:
        return KnnProbeResult(
            "empty",
            detail="Index exists and is queryable but holds no documents",
            latency_ms=latency_ms,
        )
    return KnnProbeResult("healthy", latency_ms=latency_ms)


#: ``index name -> (monotonic timestamp, verdict)``. Deliberately a wall-clock
#: TTL rather than the verify-once-and-trust-forever module flag used by
#: ``indexing_service.is_neural_pipeline_available`` — a sticky boolean is the
#: same "registry state assumed to be runtime truth" shape as issue #540 itself.
_knn_health_cache: dict[str, tuple[float, KnnProbeResult]] = {}

KNN_HEALTH_CACHE_TTL_SECONDS = 15.0


def probe_knn_health_cached(
    index_name: str, ttl: float = KNN_HEALTH_CACHE_TTL_SECONDS
) -> KnnProbeResult:
    """TTL-cached :func:`probe_knn_health`, for surfaces that get polled.

    Args:
        index_name: Concrete index to probe.
        ttl: Seconds a verdict stays fresh. Short by design: a status page left
            open must reflect reality within seconds, not until the next restart.

    Returns:
        The cached verdict when fresh, otherwise a newly measured one.
    """
    now = time.monotonic()
    cached = _knn_health_cache.get(index_name)
    if cached is not None and now - cached[0] < ttl:
        return cached[1]
    result = probe_knn_health(index_name)
    _knn_health_cache[index_name] = (now, result)
    return result


def reset_knn_health_cache() -> None:
    """Drop every cached kNN verdict.

    Called after a repair so the next read measures the rebuilt index instead of
    replaying the verdict that triggered the repair.
    """
    _knn_health_cache.clear()
