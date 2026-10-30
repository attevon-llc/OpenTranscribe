"""Chunks-index lifecycle: create, recreate for a new vector dimension, additive mapping
steps, version check and the ``speaker_id`` coverage survey."""

import logging
from typing import Any

from app.core.config import settings
from app.services.ingest_artifacts import index_mapping as digest_mapping
from app.services.opensearch_service import opensearch_client

from .index_schema import _INDEX_VERSION
from .index_schema import ADDITIVE_MAPPING_STEPS
from .index_schema import CHUNKS_ALIAS_NAME
from .index_schema import TRANSCRIPT_CHUNKS_INDEX_BODY

logger = logging.getLogger(__name__)


def ensure_chunks_index_exists() -> bool:
    """Ensure the transcript chunks index exists with proper kNN config.

    Returns:
        True if index exists or was created, False on error.
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    try:
        if opensearch_client.indices.exists(index=index_name):
            # Check MAJOR index version from _meta (log-only; a real upgrade
            # needs a full reindex, never done here).
            _check_index_version(index_name)
            # Apply any pending MINOR (additive) mapping steps. Idempotent and
            # non-destructive — see _apply_pending_additive_steps. This is the
            # ONLY thing standing between "add a field" and "bump _INDEX_VERSION
            # and cost every deployment a full reindex" for a change that does
            # not actually need one.
            _apply_pending_additive_steps(index_name)
            return True

        # Get dimension from settings service (reads from DB with default fallback)
        from app.services.search.settings_service import get_search_embedding_dimension

        dimension = get_search_embedding_dimension()
        index_body = _get_index_body_with_dimension(dimension)

        opensearch_client.indices.create(index=index_name, body=index_body)
        logger.info(f"Created transcript chunks index: {index_name} (version={_INDEX_VERSION})")

        # Create alias
        alias_name = CHUNKS_ALIAS_NAME
        if not opensearch_client.indices.exists_alias(name=alias_name):
            opensearch_client.indices.put_alias(index=index_name, name=alias_name)
            logger.info(f"Created alias {alias_name} -> {index_name}")

        return True
    except Exception as e:
        logger.error(f"Error creating chunks index: {e}")
        return False


def recreate_index_for_dimension(dimension: int) -> bool:
    """Recreate the chunks index if the embedding dimension has changed.

    This is called during model switch to ensure the index mapping matches
    the new model's vector dimension. The old index is deleted and a new
    one is created with the correct dimension.

    Args:
        dimension: The new embedding vector dimension.

    Returns:
        True if index was recreated or already correct, False on error.
    """
    if not opensearch_client:
        return False

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    try:
        if opensearch_client.indices.exists(index=index_name):
            # Check current dimension from mapping
            mapping = opensearch_client.indices.get_mapping(index=index_name)
            current_dim = (
                mapping.get(index_name, {})
                .get("mappings", {})
                .get("properties", {})
                .get("embedding", {})
                .get("dimension", 0)
            )

            if current_dim == dimension:
                logger.info(
                    f"Index {index_name} already has dimension {dimension}, no recreation needed"
                )
                return True

            logger.info(
                f"Dimension mismatch: index has {current_dim}, need {dimension}. Recreating index."
            )

            # Remove alias first if it exists
            alias_name = CHUNKS_ALIAS_NAME
            try:
                if opensearch_client.indices.exists_alias(name=alias_name):
                    opensearch_client.indices.delete_alias(index=index_name, name=alias_name)
            except Exception as e:
                logger.debug(f"Could not remove alias {alias_name} before index recreation: {e}")

            # Delete the old index
            opensearch_client.indices.delete(index=index_name)
            logger.info(f"Deleted old index {index_name}")

        # Create with new dimension
        index_body = _get_index_body_with_dimension(dimension)
        opensearch_client.indices.create(index=index_name, body=index_body)
        logger.info(f"Created index {index_name} with dimension {dimension}")

        # Recreate alias
        alias_name = CHUNKS_ALIAS_NAME
        opensearch_client.indices.put_alias(index=index_name, name=alias_name)
        logger.info(f"Created alias {alias_name} -> {index_name}")

        return True
    except Exception as e:
        logger.error(f"Error recreating index for dimension {dimension}: {e}")
        return False


def _get_index_body_with_dimension(dimension: int) -> dict[str, Any]:
    """Get index body with the correct embedding dimension."""
    import copy

    body: dict[str, Any] = copy.deepcopy(TRANSCRIPT_CHUNKS_INDEX_BODY)
    body["mappings"]["properties"]["embedding"]["dimension"] = dimension
    return body


def _check_index_version(index_name: str) -> None:
    """Check the index version stored in _meta and log a warning if outdated.

    Args:
        index_name: Name of the index to check.
    """
    if not opensearch_client:
        return

    try:
        mapping = opensearch_client.indices.get_mapping(index=index_name)
        meta = mapping.get(index_name, {}).get("mappings", {}).get("_meta", {})
        stored_version = meta.get("version", 0)

        if stored_version < _INDEX_VERSION:
            logger.warning(
                f"Index '{index_name}' is version {stored_version}, "
                f"latest is {_INDEX_VERSION}. "
                f"Run a full reindex to pick up mapping and analyzer changes."
            )
        elif stored_version == _INDEX_VERSION:
            logger.debug(f"Index '{index_name}' is at current version {_INDEX_VERSION}")
    except Exception as e:
        logger.debug(f"Could not check index version for {index_name}: {e}")


def _apply_pending_additive_steps(index_name: str) -> None:
    """Bring an EXISTING index's additive (MINOR) schema up to date, in place.

    This is the machinery that makes "add a field" not cost a reindex.
    ``_check_index_version`` above only *logs* a MAJOR-version drift — nothing
    upgrades a live index's analyzer or vector dimension without a full
    ``recreate_index_for_dimension`` (which deletes it). An additive field is a
    different kind of change: no existing document has an opinion about a field
    it has never seen, so OpenSearch's ``indices.put_mapping`` can add one to a
    live index with no downtime, no delete, and no reindex.

    **Idempotent by construction, twice over.** The version comparison below
    skips work once ``additive_version`` already meets the target, so a second
    call in the same process is a single ``get_mapping`` and nothing else. And
    even without that gate, PUTting an unchanged field definition is itself a
    no-op to OpenSearch's mapping-merge — so calling this against an index that
    somehow already has the fields (say, from a hand-run ``put_mapping``) is
    still safe; only a field whose type genuinely CHANGED would error, and that
    is precisely a case this mechanism is not for (bump ``_INDEX_VERSION``
    instead).

    **Deliberately never calls ``recreate_index_for_dimension``.** That
    function deletes and recreates the index; nothing an additive step needs
    to do requires that, and reaching for it here would turn a live,
    zero-downtime field addition into the exact destructive operation this
    machinery exists to avoid. See
    ``tests/unit/test_index_additive_version.py`` for the structural guard.

    Args:
        index_name: The chunks index to check and, if needed, update.
    """
    if not opensearch_client:
        return

    try:
        mapping = opensearch_client.indices.get_mapping(index=index_name)
        meta = mapping.get(index_name, {}).get("mappings", {}).get("_meta", {}) or {}
        current_additive = int(meta.get("additive_version", 0) or 0)

        pending = [step for step in ADDITIVE_MAPPING_STEPS if step.version > current_additive]
        if not pending:
            logger.debug(
                f"Index '{index_name}' additive schema is current "
                f"(additive_version={current_additive})"
            )
            return

        for step in pending:
            opensearch_client.indices.put_mapping(
                index=index_name, body={"properties": step.properties}
            )
            logger.info(
                f"Applied additive mapping step {step.version} to '{index_name}': "
                f"{step.description}"
            )

        # Preserve whatever MAJOR version is already stored — this call must
        # never claim the index reached a major version it did not actually
        # reindex to. Only additive_version moves.
        new_additive_version = max(step.version for step in pending)
        opensearch_client.indices.put_mapping(
            index=index_name,
            body={
                "_meta": {
                    "version": meta.get("version", 0),
                    "additive_version": new_additive_version,
                }
            },
        )
        logger.info(f"Index '{index_name}' additive_version now {new_additive_version}")
    except Exception as e:
        logger.error(f"Could not apply pending additive mapping steps to '{index_name}': {e}")


def survey_speaker_id_coverage() -> dict[str, Any]:
    """Measure what fraction of the CHUNK plane carries the new ``speaker_id`` field.

    This is the instrument a future "flip the speaker filter from names to ids"
    decision would read before proposing it (services/search/CLAUDE.md's rule for a
    measured change, same shape as ``embedding_provenance.survey_embedding_models``).
    It does **not** decide anything itself — ``speaker_id``/``profile_id`` are written
    going forward and backfilled lazily (rename propagation, per-file reindex, the
    opt-in maintenance pass), so coverage starts near 0% on any existing deployment
    and climbs only as documents are naturally rewritten.

    **Deliberately not built on the ``_get_indexed_uuids`` shape.** That function's
    ``terms`` aggregation on ``file_uuid`` has a hard 50,000-bucket ceiling — past
    that many distinct files it silently truncates and undercounts. This survey
    never enumerates distinct values at all: a ``filter`` aggregation returns one
    bounded ``doc_count``, correct at any corpus size.

    Returns:
        ``{"verdict": ..., "total": int, "with_speaker_id": int, "coverage_ratio": float}``.
        ``verdict`` is ``"unavailable"`` (no client, no index, or the query failed —
        never read as "zero coverage"), ``"empty"`` (index exists, chunk plane has
        no documents), or ``"measured"``.
    """
    empty_result: dict[str, Any] = {
        "verdict": "unavailable",
        "total": 0,
        "with_speaker_id": 0,
        "coverage_ratio": 0.0,
    }
    if not opensearch_client:
        return empty_result

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    try:
        if not opensearch_client.indices.exists(index=index_name):
            return empty_result

        response = opensearch_client.search(
            index=index_name,
            body={
                "size": 0,
                "track_total_hits": True,
                "query": {"bool": {"filter": [digest_mapping.chunk_plane_clause()]}},
                "aggs": {"with_speaker_id": {"filter": {"exists": {"field": "speaker_id"}}}},
            },
        )
    except Exception as e:
        logger.debug(f"Could not survey speaker_id coverage: {e}")
        return empty_result

    total = int((response.get("hits") or {}).get("total", {}).get("value", 0) or 0)
    if total == 0:
        return {"verdict": "empty", "total": 0, "with_speaker_id": 0, "coverage_ratio": 0.0}

    with_speaker_id = int(
        (response.get("aggregations") or {}).get("with_speaker_id", {}).get("doc_count", 0) or 0
    )
    return {
        "verdict": "measured",
        "total": total,
        "with_speaker_id": with_speaker_id,
        "coverage_ratio": round(with_speaker_id / total, 4),
    }
