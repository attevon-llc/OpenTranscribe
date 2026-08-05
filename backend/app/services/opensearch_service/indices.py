"""Index creation / bootstrap for the transcript and speaker indices."""

import logging

from app.core.config import settings
from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4
from app.core.constants import SENTENCE_TRANSFORMER_DIMENSION
from app.core.constants import get_speaker_index
from app.core.constants import get_speaker_index_v3
from app.core.constants import get_speaker_index_v4
from app.services.opensearch_service import client as _client
from app.services.opensearch_service.aliases import _count_docs
from app.services.opensearch_service.aliases import migrate_to_alias_based_indices
from app.services.opensearch_service.client import CLUSTER_UNAVAILABLE_ERRORS
from app.services.opensearch_service.client import OpenSearchUnavailableError
from app.services.opensearch_service.client import _is_alias
from app.services.opensearch_service.client import _safe_index_exists

logger = logging.getLogger(__name__)


def _ensure_versioned_speaker_index(index_name: str, dimension: int) -> None:
    """Create a versioned speaker index if it doesn't exist."""
    if not _client.opensearch_client:
        return
    try:
        if _client.opensearch_client.indices.exists(index=index_name):
            return
        config = {
            "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0, "knn": True}},
            "mappings": {
                "properties": {
                    "document_type": {"type": "keyword"},
                    "speaker_id": {"type": "integer"},
                    "speaker_uuid": {"type": "keyword"},
                    "profile_id": {"type": "integer"},
                    "profile_uuid": {"type": "keyword"},
                    "profile_name": {"type": "keyword"},
                    "user_id": {"type": "integer"},
                    # Tenant scope (cloud-edition seam; absent on personal docs)
                    "organization_id": {"type": "integer"},
                    "name": {"type": "keyword"},
                    "display_name": {"type": "keyword"},
                    "collection_ids": {"type": "integer"},
                    "media_file_id": {"type": "integer"},
                    "segment_count": {"type": "integer"},
                    "speaker_count": {"type": "integer"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": dimension,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "lucene",
                            "parameters": {"ef_construction": 128, "m": 24},
                        },
                    },
                }
            },
        }
        _client.opensearch_client.indices.create(index=index_name, body=config)
        logger.info(f"Created versioned speaker index: {index_name} (dim={dimension})")
    except CLUSTER_UNAVAILABLE_ERRORS as e:
        logger.error(f"Error creating speaker index {index_name}: {e}")


def _restore_v3_from_backup(v3_index: str) -> None:
    """Reindex the legacy ``speakers_v3_backup`` into ``speakers_v3`` when empty.

    No-op unless both indices exist, ``v3_index`` is **confirmed** empty, and the
    backup has documents. A count that could not be obtained reads as None, not
    0, so an unreachable cluster never triggers a restore.

    Args:
        v3_index: The concrete v3 speaker index to restore into.
    """
    from app.core.constants import get_speaker_index_v3_backup

    if not _client.opensearch_client:
        return

    v3_backup = get_speaker_index_v3_backup()
    if not (_safe_index_exists(v3_backup) and _safe_index_exists(v3_index)):
        return
    if _count_docs(v3_index) != 0:
        return
    backup_count = _count_docs(v3_backup) or 0
    if backup_count == 0:
        return

    try:
        # Filter: only reindex docs with valid embeddings that match the v3
        # dimension (512). Docs with null embeddings or wrong dimensions would
        # fail knn_vector parsing.
        result = _client.opensearch_client.reindex(
            body={
                "source": {
                    "index": v3_backup,
                    "query": {
                        "bool": {
                            "must": [{"exists": {"field": "embedding"}}],
                            "must_not": [{"term": {"embedding": []}}],
                        }
                    },
                },
                "dest": {"index": v3_index},
            },
            wait_for_completion=True,
        )
    except CLUSTER_UNAVAILABLE_ERRORS as e:
        logger.warning(f"Failed to restore v3 backup: {e}")
        return

    restored = result.get("created", 0) + result.get("updated", 0)
    failures = result.get("failures", [])
    logger.info(
        f"Restored {restored} docs from '{v3_backup}' → '{v3_index}' "
        f"({len(failures)} failures, backup had {backup_count} docs)"
    )
    for f in failures[:3]:
        logger.warning(f"Reindex failure: {f.get('cause', {}).get('reason', f)}")


def ensure_indices_exist():
    """
    Ensure the transcript and speaker indices exist, creating them if necessary.

    For speaker indices, uses an alias-based scheme:
    - speakers_v3: concrete index with 512-dim embeddings
    - speakers_v4: concrete index with 256-dim embeddings
    - speakers: alias pointing to whichever is active
    """
    if not settings.OPENSEARCH_ENABLED:
        return
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping index creation")
        return

    try:
        # Create transcript index if it doesn't exist
        if not _client.opensearch_client.indices.exists(index=settings.OPENSEARCH_TRANSCRIPT_INDEX):
            transcript_index_config = {
                "settings": {
                    "index": {"number_of_shards": 1, "number_of_replicas": 0},
                    "analysis": {"analyzer": {"default": {"type": "standard"}}},
                },
                "mappings": {
                    "properties": {
                        "file_id": {"type": "integer"},
                        "file_uuid": {"type": "keyword"},
                        "user_id": {"type": "integer"},
                        "content": {"type": "text"},
                        "speakers": {"type": "keyword"},
                        "tags": {"type": "keyword"},
                        "upload_time": {"type": "date"},
                        "title": {"type": "text"},
                        "embedding": {
                            "type": "knn_vector",
                            "dimension": SENTENCE_TRANSFORMER_DIMENSION,
                        },
                    }
                },
            }

            _client.opensearch_client.indices.create(
                index=settings.OPENSEARCH_TRANSCRIPT_INDEX, body=transcript_index_config
            )

            logger.info(f"Created transcript index: {settings.OPENSEARCH_TRANSCRIPT_INDEX}")

        # Migrate concrete 'speakers' index to alias-based scheme (0.3.3 upgrade)
        migration_result = migrate_to_alias_based_indices()
        if migration_result.get("status") not in ("already_migrated", "skipped"):
            logger.info(f"Speaker index alias migration: {migration_result}")

        # Ensure the versioned speaker indices exist
        from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V3

        v3_index = get_speaker_index_v3()
        _ensure_versioned_speaker_index(v3_index, PYANNOTE_EMBEDDING_DIMENSION_V3)
        _ensure_versioned_speaker_index(get_speaker_index_v4(), PYANNOTE_EMBEDDING_DIMENSION_V4)

        _restore_v3_from_backup(v3_index)

        # Ensure the 'speakers' alias exists pointing to something
        alias_name = get_speaker_index()
        if not _is_alias(alias_name) and not _safe_index_exists(alias_name):
            # Default to v4 for fresh installs
            target = get_speaker_index_v4()
            if _safe_index_exists(target):
                _client.opensearch_client.indices.put_alias(index=target, name=alias_name)
                logger.info(f"Created default alias: {alias_name} → {target}")

    except OpenSearchUnavailableError as e:
        logger.error(f"Index bootstrap aborted — OpenSearch unavailable: {e}")
    except CLUSTER_UNAVAILABLE_ERRORS as e:
        # The previous handler was `except ConnectionError` — the *builtin*,
        # which opensearch-py's ConnectionError does not subclass, so every
        # cluster failure fell through to the generic handler below.
        logger.error(f"OpenSearch error creating indices: {e}")
    except ValueError as e:
        logger.error(f"Configuration error creating indices: {e}")
    except Exception:
        # Startup path: index bootstrap must never take the process down, so a
        # genuinely unexpected error is swallowed — but with a full traceback.
        logger.exception("Unexpected error creating indices")


def create_speaker_index_v4(index_name: str | None = None) -> bool:
    """
    Create a new speaker index with v4 dimensions (256-dim).
    Used for migration from v3 to v4.

    Args:
        index_name: Name for the new index (defaults to speakers_v4)

    Returns:
        True if successful
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    index_name = index_name or get_speaker_index_v4()

    try:
        # Check if index already exists
        if _client.opensearch_client.indices.exists(index=index_name):
            logger.info(f"V4 speaker index already exists: {index_name}")
            return True

        speaker_index_config = {
            "settings": {
                "index": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                    "knn": True,
                }
            },
            "mappings": {
                "properties": {
                    "document_type": {"type": "keyword"},
                    "speaker_id": {"type": "integer"},
                    "speaker_uuid": {"type": "keyword"},
                    "profile_id": {"type": "integer"},
                    "profile_uuid": {"type": "keyword"},
                    "profile_name": {"type": "keyword"},
                    "user_id": {"type": "integer"},
                    # Tenant scope (cloud-edition seam; absent on personal docs)
                    "organization_id": {"type": "integer"},
                    "name": {"type": "keyword"},
                    "display_name": {"type": "keyword"},
                    "collection_ids": {"type": "integer"},
                    "media_file_id": {"type": "integer"},
                    "segment_count": {"type": "integer"},
                    "speaker_count": {"type": "integer"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": PYANNOTE_EMBEDDING_DIMENSION_V4,  # 256-dim for v4
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "lucene",
                            "parameters": {
                                "ef_construction": 128,
                                "m": 24,
                            },
                        },
                    },
                }
            },
        }

        _client.opensearch_client.indices.create(index=index_name, body=speaker_index_config)
        logger.info(f"Created v4 speaker index: {index_name}")
        return True

    except CLUSTER_UNAVAILABLE_ERRORS as e:
        logger.error(f"Error creating v4 speaker index: {e}")
        return False


def ensure_v4_index_exists() -> bool:
    """Ensure speakers_v4 index exists, creating if needed. Idempotent."""
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    v4_index = get_speaker_index_v4()
    try:
        if _safe_index_exists(v4_index):
            return True
    except OpenSearchUnavailableError as e:
        logger.warning(f"Error checking v4 index existence: {e}")

    return create_speaker_index_v4()
