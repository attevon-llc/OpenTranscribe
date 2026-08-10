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


def _get_sentence_transformer():
    """Lazy singleton for SentenceTransformer model."""
    global _sentence_transformer_model
    if _sentence_transformer_model is None:
        from sentence_transformers import SentenceTransformer

        _sentence_transformer_model = SentenceTransformer("all-MiniLM-L6-v2")
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


def _is_index_corruption_error(error: Exception) -> bool:
    """Check if an exception indicates OpenSearch index corruption or wrong mapping."""
    error_str = str(error).lower()
    return any(
        indicator in error_str
        for indicator in [
            "503",
            "search_phase_execution_exception",
            "already_closed",
            "no_shard_available",
            "not knn_vector type",
        ]
    )
