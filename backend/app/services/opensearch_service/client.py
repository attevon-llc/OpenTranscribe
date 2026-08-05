"""OpenSearch client construction and low-level index primitives.

Owns the process-wide ``opensearch_client`` singleton. Every other module in
this package reads it through this module (``client.opensearch_client``) so the
lazy re-initialisation performed by :func:`get_opensearch_client` is visible
everywhere.
"""

import logging
from typing import Any

from opensearchpy import OpenSearch
from opensearchpy import RequestsHttpConnection

from app.core.config import settings
from app.core.opensearch_auth import opensearch_connection_kwargs

logger = logging.getLogger(__name__)


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
    except (ConnectionError, ValueError) as e:
        logger.error(f"Configuration error initializing OpenSearch client: {e}")
        opensearch_client = None
    except Exception as e:
        logger.error(f"Unexpected error initializing OpenSearch client: {e}")
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
    except Exception as e:
        logger.warning(f"Lazy OpenSearch client initialization failed: {e}")
        return None


def _is_alias(name: str) -> bool:
    """Check if a name is an alias (not a concrete index)."""
    if not opensearch_client:
        return False
    try:
        return bool(opensearch_client.indices.exists_alias(name=name))
    except Exception:
        return False


def _get_alias_target(alias_name: str) -> str | None:
    """Get the concrete index an alias points to. Returns None if not an alias."""
    if not opensearch_client:
        return None
    try:
        result = opensearch_client.indices.get_alias(name=alias_name)
        # result is {concrete_index_name: {aliases: {alias_name: {}}}}
        indices = list(result.keys())
        return indices[0] if indices else None
    except Exception:
        return None


def _safe_index_exists(index_name: str) -> bool:
    """Check if an index exists, returning False on any error."""
    if not opensearch_client:
        return False
    try:
        return bool(opensearch_client.indices.exists(index=index_name))
    except Exception:
        return False


def _get_index_embedding_dimension(index_name: str) -> int | None:
    """Get the knn_vector dimension from an index's mapping."""
    if not opensearch_client:
        return None
    try:
        mapping = opensearch_client.indices.get_mapping(index=index_name)
        props = mapping.get(index_name, {}).get("mappings", {}).get("properties", {})
        emb = props.get("embedding", {})
        dim = emb.get("dimension")
        return int(dim) if dim is not None else None
    except Exception:
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
