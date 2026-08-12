"""Transcript indexing service for OpenSearch chunk-level search."""

import contextlib
import datetime
import logging
import time
from collections.abc import Iterator
from typing import Any

from app.core.config import settings
from app.services.opensearch_service import get_opensearch_client
from app.services.opensearch_service import opensearch_client

from .chunking_service import chunk_transcript_by_speaker_turns

logger = logging.getLogger(__name__)

# Track whether neural pipeline is available
_neural_pipeline_verified = False
_neural_pipeline_available = False

# Index version -- bump when mappings or analysis settings change.
# Stored in index _meta so ensure_chunks_index_exists() can detect stale indices.
# v5: added organization_id (tenant scope) for cloud-edition isolation.
_INDEX_VERSION = 5

# Transient bulk error types that are safe to retry
_RETRYABLE_ERROR_TYPES = frozenset(
    {
        "es_rejected_execution_exception",
        "circuit_breaking_exception",
        "cluster_block_exception",
    }
)

# Permanent error types that should NOT be retried
_PERMANENT_ERROR_TYPES = frozenset(
    {
        "mapper_parsing_exception",
        "strict_dynamic_mapping_exception",
        "illegal_argument_exception",
    }
)

# Index config for transcript chunks
TRANSCRIPT_CHUNKS_INDEX_BODY = {
    "settings": {
        "index": {
            "knn": True,
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "sort.field": ["file_uuid", "chunk_index"],
            "sort.order": ["asc", "asc"],
        },
        "analysis": {
            "analyzer": {
                "transcript": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": [
                        "lowercase",
                        "english_stop",
                        "english_snowball",
                        "shingle_filter",
                    ],
                }
            },
            "filter": {
                "english_stop": {"type": "stop", "stopwords": "_english_"},
                "english_snowball": {"type": "snowball", "language": "English"},
                "shingle_filter": {
                    "type": "shingle",
                    "min_shingle_size": 2,
                    "max_shingle_size": 3,
                    "output_unigrams": True,
                },
            },
        },
    },
    "mappings": {
        "_meta": {
            "version": _INDEX_VERSION,
        },
        "properties": {
            # Identity
            "file_id": {"type": "integer"},
            "file_uuid": {"type": "keyword"},
            "user_id": {"type": "integer"},
            # Tenant scope (cloud-edition seam; absent on community/personal docs)
            "organization_id": {"type": "integer"},
            "chunk_index": {"type": "integer"},
            # Content (BM25 searchable)
            "content": {
                "type": "text",
                "analyzer": "transcript",
                "fields": {"exact": {"type": "text", "analyzer": "standard"}},
            },
            "title": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}},
            },
            # Metadata (filterable)
            "speaker": {"type": "keyword", "eager_global_ordinals": True},
            "speakers": {"type": "keyword"},
            "tags": {"type": "keyword", "eager_global_ordinals": True},
            "content_type": {"type": "keyword"},
            "duration": {"type": "float"},
            "file_size": {"type": "long"},
            "collection_ids": {"type": "integer"},
            "accessible_user_ids": {"type": "integer"},
            "upload_time": {"type": "date"},
            "language": {"type": "keyword"},
            # Timestamps (for video navigation)
            "start_time": {"type": "float"},
            "end_time": {"type": "float"},
            # Vector embedding
            "embedding": {
                "type": "knn_vector",
                "dimension": 384,  # Updated dynamically at index creation time
                "method": {
                    "name": "hnsw",
                    "space_type": "cosinesimil",
                    "engine": "lucene",
                    "parameters": {
                        "ef_construction": 256,
                        "m": 16,
                    },
                },
            },
            # Tracking
            "embedding_model": {"type": "keyword"},
            "indexed_at": {"type": "date"},
        },
    },
}


def _invalidate_chat_retrieval_cache() -> None:
    """Mark the searchable corpus as changed so chat can't serve stale passages.

    RAG chat caches retrieval results for a few minutes. Without this, for the
    length of that window an answer could quote a recording that was just
    deleted, quarantined or re-transcribed — and cite a link that no longer
    resolves. Contained: indexing must never fail because a cache marker
    could not be written.
    """
    try:
        from app.services.chat.retrieval_cache import bump_corpus_version

        bump_corpus_version()
    except Exception:  # noqa: BLE001
        logger.debug("Could not bump chat corpus version", exc_info=True)


def chunk_plane_query(file_uuid: str, *, from_chunk_index: int | None = None) -> dict[str, Any]:
    """Build the query that selects the CHUNK-plane documents of one file.

    **Every delete against the chunks index is built here, and nowhere else.**
    #383 Phase 3 adds non-chunk documents (per-file digests, discriminated by a
    ``doc_type`` field) to this same index. A delete that only matches on
    ``file_uuid`` would take the digests with it. Keeping the predicate in one
    function means that change is a single line added to ``filters`` below::

        filters.append({"term": {"doc_type": "chunk"}})

    Do not write a ``delete_by_query`` against ``OPENSEARCH_CHUNKS_INDEX``
    anywhere else — a scattered copy is exactly what this function prevents.

    Args:
        file_uuid: UUID of the media file whose chunks are being selected.
        from_chunk_index: When set, restrict to chunks at or above this index —
            the stale tail left behind by a longer previous chunking.

    Returns:
        An OpenSearch query body fragment (the value of ``"query"``).
    """
    filters: list[dict[str, Any]] = [{"term": {"file_uuid": file_uuid}}]
    if from_chunk_index is not None:
        filters.append({"range": {"chunk_index": {"gte": from_chunk_index}}})
    return {"bool": {"filter": filters}}


def _build_hybrid_search_pipeline() -> dict[str, Any]:
    """Build the RRF search pipeline configuration with configurable rank_constant.

    Lower rank_constant values give more weight to top-ranked results.
    Default 40 is tuned for transcript search (shorter queries, focused collections).
    The standard value of 60 from the original RRF paper is optimized for web search.
    """
    return {
        "description": "Hybrid BM25 + vector search with RRF",
        "phase_results_processors": [
            {
                "score-ranker-processor": {
                    "combination": {
                        "technique": "rrf",
                        "rank_constant": settings.SEARCH_RRF_RANK_CONSTANT,
                    }
                }
            }
        ],
    }


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
            # Check index version from _meta
            _check_index_version(index_name)
            return True

        # Get dimension from settings service (reads from DB with default fallback)
        from app.services.search.settings_service import get_search_embedding_dimension

        dimension = get_search_embedding_dimension()
        index_body = _get_index_body_with_dimension(dimension)

        opensearch_client.indices.create(index=index_name, body=index_body)
        logger.info(f"Created transcript chunks index: {index_name} (version={_INDEX_VERSION})")

        # Create alias
        alias_name = "transcript_search"
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
            alias_name = "transcript_search"
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
        alias_name = "transcript_search"
        opensearch_client.indices.put_alias(index=index_name, name=alias_name)
        logger.info(f"Created alias {alias_name} -> {index_name}")

        return True
    except Exception as e:
        logger.error(f"Error recreating index for dimension {dimension}: {e}")
        return False


def ensure_search_pipeline_exists() -> bool:
    """Ensure the hybrid search pipeline exists with the correct rank_constant.

    If the pipeline exists but has a different rank_constant than configured,
    it will be recreated with the correct value.

    Returns:
        True if pipeline exists or was created, False on error.
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    pipeline_id = settings.OPENSEARCH_SEARCH_PIPELINE
    pipeline_body = _build_hybrid_search_pipeline()
    try:
        # Check if pipeline exists and has correct rank_constant
        try:
            response = opensearch_client.transport.perform_request(
                "GET", f"/_search/pipeline/{pipeline_id}"
            )
            # Verify rank_constant matches configured value
            existing = response.get(pipeline_id, response) if isinstance(response, dict) else {}
            processors = existing.get("phase_results_processors", [])
            for proc in processors:
                ranker = proc.get("score-ranker-processor", {})
                combo = ranker.get("combination", {})
                existing_rc = combo.get("rank_constant")
                if existing_rc is not None and existing_rc != settings.SEARCH_RRF_RANK_CONSTANT:
                    logger.info(
                        f"Search pipeline rank_constant mismatch: "
                        f"{existing_rc} vs {settings.SEARCH_RRF_RANK_CONSTANT}, recreating"
                    )
                    # Delete and recreate
                    opensearch_client.transport.perform_request(
                        "DELETE", f"/_search/pipeline/{pipeline_id}"
                    )
                    break
            else:
                # Pipeline exists with correct config
                return True
        except Exception:
            logger.debug(f"Search pipeline {pipeline_id} not found, will create it")

        # Create pipeline
        opensearch_client.transport.perform_request(
            "PUT",
            f"/_search/pipeline/{pipeline_id}",
            body=pipeline_body,
        )
        logger.info(
            f"Created search pipeline: {pipeline_id} "
            f"(rank_constant={settings.SEARCH_RRF_RANK_CONSTANT})"
        )
        return True
    except Exception as e:
        logger.error(f"Error creating search pipeline: {e}")
        return False


def _get_model_id_from_service() -> str | None:
    """Get the active model ID from the ML model service.

    Returns:
        Model ID string or None if not available.
    """
    try:
        from .ml_model_service import get_ml_model_service

        ml_service = get_ml_model_service()
        return ml_service.get_active_model_id()
    except Exception as e:
        logger.warning(f"Could not get active model: {e}")
        return None


def _check_existing_pipeline_model(pipeline_id: str, expected_model_id: str) -> bool | None:
    """Check if existing pipeline has the expected model.

    Args:
        pipeline_id: Pipeline ID to check.
        expected_model_id: Expected model ID.

    Returns:
        True if pipeline exists with correct model, False if model mismatch, None if not found.
    """
    if not opensearch_client:
        return None

    try:
        response = opensearch_client.ingest.get_pipeline(id=pipeline_id)
        current_pipeline = response.get(pipeline_id, {})
        processors = current_pipeline.get("processors", [])

        for processor in processors:
            if "text_embedding" in processor:
                current_model = processor["text_embedding"].get("model_id")
                if current_model == expected_model_id:
                    return True
                logger.info(
                    f"Neural pipeline model mismatch: {current_model} vs {expected_model_id}, updating"
                )
                return False
        return None
    except Exception:
        logger.debug(f"Neural ingest pipeline {pipeline_id} not found, will create it")
        return None


def ensure_neural_ingest_pipeline(model_id: str | None = None) -> bool:
    """Ensure the neural ingest pipeline exists with the specified model.

    The neural ingest pipeline uses OpenSearch's text_embedding processor
    to generate embeddings server-side during document ingestion.

    Args:
        model_id: OpenSearch ML model ID. If None, attempts to get from service.

    Returns:
        True if pipeline exists or was created, False on error.
    """
    global _neural_pipeline_verified, _neural_pipeline_available

    if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
        logger.debug("Neural search disabled, skipping neural ingest pipeline")
        return False

    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    pipeline_id = settings.OPENSEARCH_NEURAL_PIPELINE

    # Get model_id if not provided
    if not model_id:
        model_id = _get_model_id_from_service()

    if not model_id:
        logger.warning("No model_id available for neural ingest pipeline")
        return False

    try:
        # Check if pipeline exists with correct model
        pipeline_check = _check_existing_pipeline_model(pipeline_id, model_id)
        if pipeline_check is True:
            _neural_pipeline_verified = True
            _neural_pipeline_available = True
            return True

        # Create or update pipeline (try with batch_size first, fall back without)
        batch_size = settings.SEARCH_NEURAL_BATCH_SIZE
        text_embedding_config: dict[str, Any] = {
            "model_id": model_id,
            "field_map": {"content": "embedding"},
            "batch_size": batch_size,
            "ignore_failure": False,
        }
        pipeline_body: dict[str, Any] = {
            "description": f"Neural embedding pipeline for transcript search (model: {model_id})",
            "processors": [
                {
                    "text_embedding": text_embedding_config,
                }
            ],
        }

        try:
            opensearch_client.ingest.put_pipeline(id=pipeline_id, body=pipeline_body)
            logger.info(
                f"Created/updated neural ingest pipeline: {pipeline_id} "
                f"with model {model_id} (batch_size={batch_size})"
            )
        except Exception as batch_err:
            logger.warning(
                f"Neural pipeline creation with batch_size={batch_size} failed: {batch_err}. "
                f"Retrying without batch_size."
            )
            # Fall back to pipeline without batch_size for older OpenSearch versions
            text_embedding_config.pop("batch_size", None)
            opensearch_client.ingest.put_pipeline(id=pipeline_id, body=pipeline_body)
            logger.info(
                f"Created/updated neural ingest pipeline: {pipeline_id} "
                f"with model {model_id} (no batch_size)"
            )

        _neural_pipeline_verified = True
        _neural_pipeline_available = True
        return True

    except Exception as e:
        logger.error(f"Error creating neural ingest pipeline: {e}")
        _neural_pipeline_verified = False
        _neural_pipeline_available = False
        return False


def is_neural_pipeline_available() -> bool:
    """Check if the neural ingest pipeline is available.

    Returns:
        True if neural pipeline is configured and available.
    """
    global _neural_pipeline_verified, _neural_pipeline_available

    if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
        return False

    if _neural_pipeline_verified:
        return _neural_pipeline_available

    # Try to verify
    return ensure_neural_ingest_pipeline()


def reset_neural_pipeline_state() -> None:
    """Reset the neural pipeline verification state.

    Call this when switching models or after configuration changes.
    """
    global _neural_pipeline_verified, _neural_pipeline_available
    _neural_pipeline_verified = False
    _neural_pipeline_available = False


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


@contextlib.contextmanager
def _suspended_refresh_for_large_index(
    index_name: str,
    *,
    chunk_count: int,
    threshold: int,
) -> Iterator[None]:
    """Suspend an index's ``refresh_interval`` while a bulk load runs.

    Only triggers when ``chunk_count >= threshold`` — small transcripts
    still benefit from near-real-time search. On exit the prior interval
    is restored and a manual refresh is issued so the newly loaded
    chunks become searchable immediately.

    Args:
        index_name: Target OpenSearch index.
        chunk_count: Number of chunks about to be loaded.
        threshold: Minimum chunk count that activates the suspension.
    """
    if chunk_count < threshold or not opensearch_client:
        yield
        return

    prior_interval: str | None = None
    try:
        settings_resp = opensearch_client.indices.get_settings(index=index_name)
        prior_interval = (
            settings_resp.get(index_name, {})
            .get("settings", {})
            .get("index", {})
            .get("refresh_interval")
        )
    except Exception as e:
        logger.debug(f"Could not read refresh_interval for {index_name}, skipping suspension: {e}")
        yield
        return

    try:
        opensearch_client.indices.put_settings(
            index=index_name, body={"index": {"refresh_interval": "-1"}}
        )
        logger.info(
            f"Suspended refresh on {index_name} for bulk load of {chunk_count} chunks "
            f"(prior refresh_interval={prior_interval!r})"
        )
        yield
    finally:
        try:
            restored = prior_interval if prior_interval is not None else "1s"
            opensearch_client.indices.put_settings(
                index=index_name, body={"index": {"refresh_interval": restored}}
            )
            opensearch_client.indices.refresh(index=index_name)
            logger.info(f"Restored refresh_interval={restored!r} on {index_name} and refreshed")
        except Exception as e:
            logger.warning(
                f"Failed to restore refresh_interval on {index_name}: {e}. "
                f'Run `PUT /{index_name}/_settings -d \'{{"index":{{"refresh_interval":"1s"}}}}\'`'
            )


class TranscriptIndexingService:
    """Handles chunking, embedding, and indexing transcripts into OpenSearch.

    Uses OpenSearch neural search for embedding generation. Embeddings are
    generated server-side via the neural ingest pipeline, which eliminates
    Python embedding overhead and enables hot-swap model changes.

    If neural search is not available, documents are indexed without embeddings
    and search falls back to BM25-only (keyword search).
    """

    def index_transcript_chunks(
        self,
        file_id: int,
        file_uuid: str,
        user_id: int,
        segments: list[dict[str, Any]],
        title: str,
        speakers: list[str],
        tags: list[str],
        upload_time: str | None = None,
        language: str = "en",
        content_type: str = "",
        duration: float | None = None,
        file_size: int | None = None,
        collection_ids: list[int] | None = None,
        accessible_user_ids: list[int] | None = None,
        organization_id: int | None = None,
    ) -> dict[str, Any] | int:
        """Chunk and index a transcript.

        Embedding modes (in priority order):
        1. Neural pipeline available — OpenSearch generates embeddings server-side
        2. No embedding — BM25 keyword search only

        Args:
            file_id: Media file integer ID.
            file_uuid: Media file UUID.
            user_id: Owner user ID.
            segments: Transcript segments with start, end, text, speaker.
            title: File title.
            speakers: All speaker names in the file.
            tags: Tags associated with the file.
            upload_time: Upload time ISO string (defaults to now).
            language: Language code.
            content_type: MIME content type of the file.
            duration: Duration in seconds.
            file_size: File size in bytes.
            collection_ids: List of collection IDs the file belongs to.
            accessible_user_ids: List of user IDs with access to this file.
                Includes owner + users/groups with collection shares.
                If None, defaults to [user_id] (owner only).

        Returns:
            Dict with indexing stats or int (chunk count).
        """
        client = get_opensearch_client()
        if not client:
            logger.warning("OpenSearch client not initialized, skipping chunk indexing")
            return 0

        if not segments:
            logger.warning(f"No segments to index for file {file_uuid}")
            return 0

        ensure_chunks_index_exists()
        ensure_search_pipeline_exists()

        if upload_time is None:
            upload_time = datetime.datetime.now(datetime.UTC).isoformat()

        # 1. Chunk segments
        t_chunk_start = time.time()
        chunks = chunk_transcript_by_speaker_turns(
            segments=segments,
            file_uuid=file_uuid,
            file_id=file_id,
            user_id=user_id,
            title=title,
            speakers=speakers,
            tags=tags,
            upload_time=upload_time,
            language=language,
            content_type=content_type,
            duration=duration,
            file_size=file_size,
            collection_ids=collection_ids,
            organization_id=organization_id,
        )
        chunk_ms = round((time.time() - t_chunk_start) * 1000)

        if not chunks:
            logger.warning(f"No chunks generated for file {file_uuid}")
            return 0

        # 2. Add indexed_at timestamp and accessible_user_ids
        now = datetime.datetime.now(datetime.UTC).isoformat()
        effective_user_ids = accessible_user_ids if accessible_user_ids else [user_id]
        for chunk in chunks:
            chunk["indexed_at"] = now
            chunk["accessible_user_ids"] = effective_user_ids

        # 3. Choose embedding mode and index
        t_index_start = time.time()
        try:
            use_neural = is_neural_pipeline_available()
            if use_neural:
                for chunk in chunks:
                    chunk["embedding_model"] = "neural"
                logger.debug(f"Using neural ingest pipeline for file {file_uuid}")
            else:
                for chunk in chunks:
                    chunk["embedding_model"] = None
                logger.warning(f"Neural pipeline not available for {file_uuid}, text-only")

            # For very large transcripts (6h+ recordings produce 500+ chunks)
            # suspend index refresh during the bulk load so we don't pay the
            # per-batch refresh cost. The context manager restores the prior
            # refresh_interval on exit.
            with _suspended_refresh_for_large_index(
                settings.OPENSEARCH_CHUNKS_INDEX,
                chunk_count=len(chunks),
                threshold=settings.SEARCH_LARGE_TRANSCRIPT_CHUNKS,
            ):
                indexed = self._bulk_index_chunks(chunks, use_neural_pipeline=use_neural)

            # Doc ids are deterministic (``{file_uuid}_{chunk_index}``), so the bulk
            # load above OVERWRITES chunks 0..len(chunks)-1 but cannot touch a longer
            # previous chunking's tail. Without this, a re-index after a segment merge,
            # a speaker-turn change or a SEARCH_CHUNK_TARGET_WORDS bump leaves stale
            # documents — old text, old speakers, old timestamps — that keep surfacing
            # in search and in RAG chat retrieval (issue #400).
            stale_removed = self._prune_stale_chunks(file_uuid, keep_count=len(chunks))

            index_ms = round((time.time() - t_index_start) * 1000)
            total_ms = chunk_ms + index_ms
            mode_str = "neural" if use_neural else "text-only"
            logger.info(
                f"Indexed {indexed} chunks for file {file_uuid} "
                f"(mode: {mode_str}, chunk={chunk_ms}ms, index={index_ms}ms)"
            )
            # New/changed transcript content: chat must not keep serving
            # retrieval results computed against the previous version.
            _invalidate_chat_retrieval_cache()
            return {
                "chunk_count": indexed,
                "chunk_ms": chunk_ms,
                "index_ms": index_ms,
                "total_ms": total_ms,
                "mode": mode_str,
                "neural": use_neural,
                "stale_removed": stale_removed,
            }
        except Exception as e:
            logger.error(f"Bulk indexing failed for file {file_uuid}: {e}")
            return 0

    def delete_transcript_chunks(self, file_uuid: str) -> int:
        """Delete all chunks for a file.

        Args:
            file_uuid: UUID of the file to delete chunks for.

        Returns:
            Number of chunks deleted.
        """
        if not opensearch_client:
            return 0

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        try:
            if not opensearch_client.indices.exists(index=index_name):
                return 0

            response = opensearch_client.delete_by_query(
                index=index_name,
                body={"query": chunk_plane_query(file_uuid)},
                refresh=True,
            )
            deleted: int = response.get("deleted", 0)
            logger.info(f"Deleted {deleted} chunks for file {file_uuid}")
            if deleted:
                _invalidate_chat_retrieval_cache()
            return deleted
        except Exception as e:
            logger.error(f"Error deleting chunks for file {file_uuid}: {e}")
            return 0

    def _prune_stale_chunks(self, file_uuid: str, *, keep_count: int) -> int:
        """Delete chunks left behind by a longer previous chunking of this file.

        Called after every bulk load. Chunks ``0..keep_count-1`` were just
        overwritten in place (the doc id is derived from ``chunk_index``); anything
        at or above ``keep_count`` belongs to a chunking that no longer exists.

        **The count runs first so the first index after transcription pays nothing.**
        A ``count`` is a cheap search against the existing searcher; the
        ``delete_by_query`` it guards forces a whole-index ``refresh``, which is the
        part worth avoiding on the hot path where there is provably nothing to delete.

        The count reads the searcher, so it can miss documents written within the
        index's ``refresh_interval`` (1 s). That only matters if the same file were
        re-indexed twice inside one second, which no pipeline path does — a re-index
        follows a transcript edit, a reprocess, or a recovery sweep.

        A failure here is logged, not raised: the new chunks are already indexed and
        correct, and the caller must not report a successful index as a failure.

        Args:
            file_uuid: UUID of the media file just indexed.
            keep_count: Number of chunks the new chunking produced.

        Returns:
            Number of stale chunks deleted.
        """
        if not opensearch_client:
            return 0

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        query = chunk_plane_query(file_uuid, from_chunk_index=keep_count)
        try:
            count_response = opensearch_client.count(index=index_name, body={"query": query})
            if not count_response.get("count", 0):
                return 0

            response = opensearch_client.delete_by_query(
                index=index_name,
                body={"query": query},
                refresh=True,
                conflicts="proceed",
            )
            deleted = int(response.get("deleted", 0))
            logger.info(
                f"Pruned {deleted} stale chunk(s) for file {file_uuid} "
                f"(re-chunk shrank to {keep_count} chunks)"
            )
            return deleted
        except Exception as e:
            logger.error(
                f"Could not prune stale chunks for file {file_uuid} "
                f"(chunks >= {keep_count} may still be searchable): {e}"
            )
            return 0

    def reindex_transcript(
        self,
        file_id: int,
        file_uuid: str,
        user_id: int,
        segments: list[dict[str, Any]],
        title: str,
        speakers: list[str],
        tags: list[str],
        upload_time: str | None = None,
        language: str = "en",
        content_type: str = "",
        duration: float | None = None,
        file_size: int | None = None,
        collection_ids: list[int] | None = None,
        accessible_user_ids: list[int] | None = None,
        organization_id: int | None = None,
    ) -> int:
        """Re-chunk and re-index a single transcript.

        Args:
            Same as index_transcript_chunks.

        Returns:
            Number of chunks indexed.
        """
        # Delete existing chunks first. Not redundant with the tail prune
        # ``index_transcript_chunks`` now does (issue #400): a full rebuild wants a
        # clean slate, and this is the only path that also clears the chunks of a
        # transcript that now yields NO chunks at all — in which case
        # ``index_transcript_chunks`` returns early without pruning.
        self.delete_transcript_chunks(file_uuid)

        # Re-index
        result = self.index_transcript_chunks(
            file_id=file_id,
            file_uuid=file_uuid,
            user_id=user_id,
            segments=segments,
            title=title,
            speakers=speakers,
            tags=tags,
            upload_time=upload_time,
            language=language,
            content_type=content_type,
            duration=duration,
            file_size=file_size,
            collection_ids=collection_ids,
            accessible_user_ids=accessible_user_ids,
            organization_id=organization_id,
        )
        # Extract chunk count from result (dict or int)
        if isinstance(result, dict):
            chunk_count: int = result.get("chunk_count", 0)
            return chunk_count
        return result

    def _bulk_index_chunks(
        self, chunks: list[dict[str, Any]], use_neural_pipeline: bool = False
    ) -> int:
        """Bulk index chunks to OpenSearch in batches.

        Splits chunks into batches of SEARCH_BULK_BATCH_SIZE to avoid
        timeouts on large files. Failed documents with transient errors
        are retried with exponential backoff.

        Args:
            chunks: List of chunk documents to index.
            use_neural_pipeline: If True, use neural ingest pipeline for embedding.

        Returns:
            Number of successfully indexed chunks.
        """
        if not opensearch_client:
            raise RuntimeError("OpenSearch client not initialized")

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        batch_size = settings.SEARCH_BULK_BATCH_SIZE
        total_indexed = 0

        for batch_start in range(0, len(chunks), batch_size):
            batch = chunks[batch_start : batch_start + batch_size]
            bulk_body: list[Any] = []

            for chunk in batch:
                doc_id = f"{chunk['file_uuid']}_{chunk['chunk_index']}"
                index_action: dict[str, Any] = {
                    "index": {
                        "_index": index_name,
                        "_id": doc_id,
                    }
                }

                # Use neural ingest pipeline if enabled
                if use_neural_pipeline:
                    index_action["index"]["pipeline"] = settings.OPENSEARCH_NEURAL_PIPELINE

                bulk_body.append(index_action)
                bulk_body.append(chunk)

            response = opensearch_client.bulk(body=bulk_body, refresh=False)

            if response.get("errors"):
                failed_docs = self._extract_failed_docs(response, batch)
                succeeded = len(batch) - len(failed_docs)
                total_indexed += succeeded

                if failed_docs:
                    retried = self._retry_failed_docs(failed_docs, index_name, use_neural_pipeline)
                    total_indexed += retried
            else:
                total_indexed += len(batch)

            if len(chunks) > batch_size:
                logger.debug(
                    f"Bulk batch {batch_start // batch_size + 1}: "
                    f"indexed {len(batch)} chunks (offset {batch_start})"
                )

        return total_indexed

    def _extract_failed_docs(
        self, response: dict[str, Any], batch: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Extract documents that failed with transient errors from a bulk response.

        Permanent errors (e.g. mapping exceptions) are logged and skipped.
        Transient errors (e.g. circuit breaker, rejected execution) are returned
        for retry.

        Args:
            response: OpenSearch bulk response.
            batch: The original batch of chunk documents.

        Returns:
            List of chunk documents that should be retried.
        """
        failed_docs: list[dict[str, Any]] = []
        permanent_count = 0

        for i, item in enumerate(response.get("items", [])):
            index_result = item.get("index", {})
            error_info = index_result.get("error")
            if not error_info:
                continue

            error_type = error_info.get("type", "")
            error_reason = error_info.get("reason", "")

            if error_type in _PERMANENT_ERROR_TYPES:
                # Permanent error -- log and skip
                permanent_count += 1
                logger.error(f"Permanent bulk index error (skipping): {error_type}: {error_reason}")
            elif error_type in _RETRYABLE_ERROR_TYPES:
                # Known transient error -- eligible for retry
                if i < len(batch):
                    failed_docs.append(batch[i])
                logger.warning(
                    f"Transient bulk index error (will retry): {error_type}: {error_reason}"
                )
            else:
                # Unknown error type -- log and skip (don't blindly retry)
                permanent_count += 1
                logger.error(f"Unknown bulk index error (skipping): {error_type}: {error_reason}")

        if permanent_count:
            logger.error(f"Bulk indexing had {permanent_count} permanent errors (not retried)")
        if failed_docs:
            logger.info(f"Bulk indexing has {len(failed_docs)} transient failures to retry")

        return failed_docs

    def _retry_failed_docs(
        self,
        failed_docs: list[dict[str, Any]],
        index_name: str,
        use_neural: bool,
        max_retries: int = 2,
    ) -> int:
        """Retry failed documents with exponential backoff.

        Args:
            failed_docs: List of chunk documents to retry.
            index_name: OpenSearch index name.
            use_neural: Whether to use the neural ingest pipeline.
            max_retries: Maximum number of retry attempts.

        Returns:
            Number of successfully indexed documents after retries.
        """
        if not opensearch_client or not failed_docs:
            return 0

        retried_count = 0
        remaining = list(failed_docs)

        for attempt in range(1, max_retries + 1):
            if not remaining:
                break

            backoff = attempt  # 1s, 2s
            logger.info(
                f"Retrying {len(remaining)} failed docs (attempt {attempt}/{max_retries}, "
                f"backoff {backoff}s)"
            )
            time.sleep(backoff)

            bulk_body: list[Any] = []
            for chunk in remaining:
                doc_id = f"{chunk['file_uuid']}_{chunk['chunk_index']}"
                index_action: dict[str, Any] = {
                    "index": {
                        "_index": index_name,
                        "_id": doc_id,
                    }
                }
                if use_neural:
                    index_action["index"]["pipeline"] = settings.OPENSEARCH_NEURAL_PIPELINE
                bulk_body.append(index_action)
                bulk_body.append(chunk)

            try:
                response = opensearch_client.bulk(body=bulk_body, refresh=False)
            except Exception as e:
                logger.error(f"Retry attempt {attempt} bulk call failed: {e}")
                continue

            if not response.get("errors"):
                retried_count += len(remaining)
                remaining = []
                break

            # Check which ones still failed
            still_failed: list[dict[str, Any]] = []
            for i, item in enumerate(response.get("items", [])):
                index_result = item.get("index", {})
                if index_result.get("error"):
                    if i < len(remaining):
                        still_failed.append(remaining[i])
                else:
                    retried_count += 1

            remaining = still_failed

        if remaining:
            logger.error(f"{len(remaining)} documents failed after {max_retries} retries")

        if retried_count:
            logger.info(f"Successfully retried {retried_count} documents")

        return retried_count
