"""Writes of speaker (voiceprint) embeddings, single and bulk, v3 and v4."""

import datetime
import logging
from typing import Any

from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V3
from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4
from app.core.constants import get_speaker_index
from app.core.constants import get_speaker_index_v4
from app.services.opensearch_service import client as _client
from app.services.opensearch_service.client import _is_index_corruption_error
from app.services.opensearch_service.indices import ensure_indices_exist
from app.services.opensearch_service.repair import _repair_index

logger = logging.getLogger(__name__)


def add_speaker_embedding_v4(
    speaker_id: int,
    speaker_uuid: str,
    user_id: int,
    name: str,
    embedding: list[float],
    profile_id: int | None = None,
    profile_uuid: str | None = None,
    collection_ids: list[int] | None = None,
    media_file_id: int | None = None,
    segment_count: int = 1,
    display_name: str | None = None,
    organization_id: int | None = None,
):
    """
    Add a speaker embedding to the v4 staging index during migration.

    This function indexes to the _v4 staging index instead of the main speaker index,
    allowing migration to proceed without affecting the production index.

    Args:
        speaker_id: ID of the speaker in the database (for internal queries)
        speaker_uuid: UUID of the speaker (used as document ID)
        user_id: ID of the user who owns the speaker profile
        name: Name of the speaker
        embedding: Vector embedding of the speaker's voice (256-dim for v4)
        profile_id: Optional speaker profile ID (for internal queries)
        profile_uuid: Optional speaker profile UUID
        collection_ids: Optional list of collection IDs
        media_file_id: Optional source media file ID
        segment_count: Number of segments used to create embedding
        display_name: Optional display name for the speaker
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping speaker embedding")
        return

    v4_index = get_speaker_index_v4()

    try:
        # Validate embedding before indexing
        if embedding is None:
            logger.error(f"Cannot index speaker {speaker_uuid}: embedding is None")
            return

        if not isinstance(embedding, list) or len(embedding) == 0:
            logger.error(f"Cannot index speaker {speaker_uuid}: invalid embedding format")
            return

        # Dimension safety check: v4 index expects 256-dim embeddings
        emb_len = len(embedding)
        if emb_len != PYANNOTE_EMBEDDING_DIMENSION_V4:
            logger.error(
                f"Cannot index speaker {speaker_uuid} to v4: dimension mismatch "
                f"{emb_len} != {PYANNOTE_EMBEDDING_DIMENSION_V4}"
            )
            return

        logger.info(
            f"Indexing speaker {speaker_uuid} (ID: {speaker_id}) to v4 index with embedding length: {emb_len}"
        )

        # Prepare document (organization_id only written for org files — personal
        # docs stay org-less to match the personal-scope search gate)
        doc = {
            "speaker_id": speaker_id,
            "speaker_uuid": str(speaker_uuid),
            "profile_id": profile_id,
            "profile_uuid": str(profile_uuid) if profile_uuid else None,
            "user_id": user_id,
            "name": name,
            "display_name": display_name,
            "collection_ids": collection_ids or [],
            "media_file_id": media_file_id,
            "segment_count": segment_count,
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "embedding": embedding,
        }
        if organization_id is not None:
            doc["organization_id"] = organization_id

        # Index the document to the v4 staging index using UUID as document ID
        response = _client.opensearch_client.index(
            index=v4_index,
            body=doc,
            id=str(speaker_uuid),  # Use speaker_uuid as document ID
        )

        logger.info(
            f"Indexed speaker embedding for speaker {speaker_uuid} (ID: {speaker_id}) to v4 index: {response}"
        )
        return response

    except Exception as e:
        logger.error(
            f"Error indexing speaker embedding to v4 for speaker {speaker_uuid} (ID: {speaker_id}): {e}"
        )


def bulk_add_speaker_embeddings_v4(embeddings_data: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Bulk-index v4 (256-dim) speaker embeddings in one OpenSearch round-trip.

    Every post-transcription run stores one centroid per detected speaker.
    A 10-speaker meeting previously hit 10 sequential ``index()`` calls
    against OpenSearch (~200-500 ms of round-trip overhead); this helper
    batches them into one bulk request. Phase 2 PR #9, item D14.

    The per-speaker dimension guard mirrors ``add_speaker_embedding_v4`` —
    malformed entries are skipped rather than failing the whole batch, so
    a single bad embedding can't block the rest from landing.

    Args:
        embeddings_data: List of dicts shaped like the kwargs of
            ``add_speaker_embedding_v4``. ``speaker_uuid``, ``speaker_id``,
            ``user_id``, ``name``, and ``embedding`` are required. All
            other fields default the same way as the single-write helper.

    Returns:
        The OpenSearch bulk response dict, or None when the client is
        unavailable or no valid entries remain after validation.
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping bulk v4 speaker embeddings")
        return None
    if not embeddings_data:
        return None

    v4_index = get_speaker_index_v4()
    bulk_body: list[dict[str, Any]] = []
    now_iso = datetime.datetime.now(datetime.UTC).isoformat()
    accepted = 0

    for data in embeddings_data:
        speaker_uuid = data.get("speaker_uuid")
        speaker_id = data.get("speaker_id")
        embedding = data.get("embedding")

        if speaker_uuid is None or speaker_id is None:
            logger.error("Bulk v4 add skipping entry: missing speaker_uuid/speaker_id")
            continue
        if not isinstance(embedding, list) or len(embedding) != PYANNOTE_EMBEDDING_DIMENSION_V4:
            logger.error(
                f"Bulk v4 add skipping speaker {speaker_uuid}: embedding dim "
                f"{len(embedding) if isinstance(embedding, list) else 'n/a'} "
                f"!= {PYANNOTE_EMBEDDING_DIMENSION_V4}"
            )
            continue

        bulk_body.append(
            {
                "index": {
                    "_index": v4_index,
                    "_id": str(speaker_uuid),
                }
            }
        )
        profile_uuid = data.get("profile_uuid")
        v4_doc: dict[str, Any] = {
            "speaker_id": speaker_id,
            "speaker_uuid": str(speaker_uuid),
            "profile_id": data.get("profile_id"),
            "profile_uuid": str(profile_uuid) if profile_uuid else None,
            "user_id": data["user_id"],
            "name": data["name"],
            "display_name": data.get("display_name"),
            "collection_ids": data.get("collection_ids") or [],
            "media_file_id": data.get("media_file_id"),
            "segment_count": data.get("segment_count", 1),
            "created_at": now_iso,
            "updated_at": now_iso,
            "embedding": embedding,
        }
        if data.get("organization_id") is not None:
            v4_doc["organization_id"] = data["organization_id"]
        bulk_body.append(v4_doc)
        accepted += 1

    if accepted == 0:
        return None

    try:
        response = _client.opensearch_client.bulk(body=bulk_body)
        if response.get("errors"):
            logger.error(f"Bulk v4 speaker embedding indexing had errors: {response}")
        else:
            logger.info(
                f"Bulk-indexed {accepted} v4 speaker embeddings into {v4_index} in one request"
            )
        return response  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(f"Error bulk-indexing v4 speaker embeddings: {e}")
        return None


def add_speaker_embedding(
    speaker_id: int,
    speaker_uuid: str,
    user_id: int,
    name: str,
    embedding: list[float],
    profile_id: int | None = None,
    profile_uuid: str | None = None,
    collection_ids: list[int] | None = None,
    media_file_id: int | None = None,
    segment_count: int = 1,
    display_name: str | None = None,
    target_index: str | None = None,
    organization_id: int | None = None,
):
    """
    Add a speaker embedding to OpenSearch with collection support

    Args:
        speaker_id: ID of the speaker in the database (for internal queries)
        speaker_uuid: UUID of the speaker (used as document ID)
        user_id: ID of the user who owns the speaker profile
        name: Name of the speaker
        embedding: Vector embedding of the speaker's voice
        profile_id: Optional speaker profile ID (for internal queries)
        profile_uuid: Optional speaker profile UUID
        collection_ids: Optional list of collection IDs
        media_file_id: Optional source media file ID
        segment_count: Number of segments used to create embedding
        display_name: Optional display name for the speaker
        target_index: Optional override index name (e.g. 'speakers_v3').
            Defaults to the 'speakers' alias when None.
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping speaker embedding")
        return

    index_name = target_index or get_speaker_index()

    try:
        ensure_indices_exist()

        # Validate embedding before indexing
        if embedding is None:
            logger.error(f"Cannot index speaker {speaker_uuid}: embedding is None")
            return

        if not isinstance(embedding, list) or len(embedding) == 0:
            logger.error(f"Cannot index speaker {speaker_uuid}: invalid embedding format")
            return

        # Dimension safety check: prevent writing wrong-dimension vectors
        emb_len = len(embedding)
        if emb_len not in (PYANNOTE_EMBEDDING_DIMENSION_V3, PYANNOTE_EMBEDDING_DIMENSION_V4):
            logger.error(
                f"Cannot index speaker {speaker_uuid}: unexpected embedding dimension "
                f"{emb_len} (expected {PYANNOTE_EMBEDDING_DIMENSION_V3} or "
                f"{PYANNOTE_EMBEDDING_DIMENSION_V4})"
            )
            return

        logger.info(
            f"Indexing speaker {speaker_uuid} (ID: {speaker_id}) with embedding length: {emb_len}"
        )

        # Prepare document (organization_id only written for org files)
        doc = {
            "speaker_id": speaker_id,
            "speaker_uuid": str(speaker_uuid),
            "profile_id": profile_id,
            "profile_uuid": str(profile_uuid) if profile_uuid else None,
            "user_id": user_id,
            "name": name,
            "display_name": display_name,
            "collection_ids": collection_ids or [],
            "media_file_id": media_file_id,
            "segment_count": segment_count,
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "embedding": embedding,
        }
        if organization_id is not None:
            doc["organization_id"] = organization_id

        # Index the document using UUID as document ID
        response = _client.opensearch_client.index(
            index=index_name,
            body=doc,
            id=str(speaker_uuid),  # Use speaker_uuid as document ID
        )

        logger.info(
            f"Indexed speaker embedding for speaker {speaker_uuid} (ID: {speaker_id}) "
            f"to {index_name}: {response}"
        )
        return response

    except Exception as e:
        # Retry once for transient connection errors before falling through
        # to the index corruption check below
        if isinstance(e, (ConnectionError, OSError)):
            logger.warning(f"Transient error indexing speaker {speaker_uuid}, retrying once: {e}")
            import time as _time

            _time.sleep(0.5)
            try:
                response = _client.opensearch_client.index(
                    index=index_name,
                    body=doc,
                    id=str(speaker_uuid),
                )
                logger.info(
                    f"Retry succeeded: indexed speaker {speaker_uuid} after transient error"
                )
                return response
            except Exception as retry_err:
                logger.error(f"Retry failed for speaker {speaker_uuid}: {retry_err}")
                # Fall through to index corruption check with the retry error
                e = retry_err
        if _is_index_corruption_error(e):
            logger.warning(
                f"Index corruption detected indexing speaker {speaker_uuid}, attempting repair..."
            )
            if _repair_index(index_name):
                try:
                    doc = {
                        "speaker_id": speaker_id,
                        "speaker_uuid": str(speaker_uuid),
                        "profile_id": profile_id,
                        "profile_uuid": str(profile_uuid) if profile_uuid else None,
                        "user_id": user_id,
                        "name": name,
                        "display_name": display_name,
                        "collection_ids": collection_ids or [],
                        "media_file_id": media_file_id,
                        "segment_count": segment_count,
                        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
                        "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
                        "embedding": embedding,
                    }
                    if organization_id is not None:
                        doc["organization_id"] = organization_id
                    response = _client.opensearch_client.index(
                        index=index_name,
                        body=doc,
                        id=str(speaker_uuid),
                    )
                    logger.info(f"Retry succeeded: indexed speaker {speaker_uuid} after repair")
                    return response
                except Exception as retry_err:
                    logger.error(
                        f"Retry after repair failed for speaker {speaker_uuid}: {retry_err}"
                    )
        else:
            logger.error(
                f"Error indexing speaker embedding for speaker {speaker_uuid} (ID: {speaker_id}): {e}"
            )


def bulk_add_speaker_embeddings(embeddings_data: list[dict[str, Any]]):
    """
    Bulk add multiple speaker embeddings for efficient indexing

    Args:
        embeddings_data: List of embedding data dictionaries with speaker_uuid
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return

    try:
        ensure_indices_exist()

        # Prepare bulk operations
        bulk_body = []
        for data in embeddings_data:
            # Index action using UUID as document ID
            bulk_body.append(
                {
                    "index": {
                        "_index": get_speaker_index(),
                        "_id": str(data["speaker_uuid"]),
                    }
                }
            )

            # Document (organization_id only written for org files)
            doc_data: dict[str, Any] = {
                "speaker_id": data["speaker_id"],
                "speaker_uuid": str(data["speaker_uuid"]),
                "profile_id": data.get("profile_id"),
                "profile_uuid": str(data.get("profile_uuid")) if data.get("profile_uuid") else None,
                "user_id": data["user_id"],
                "name": data["name"],
                "collection_ids": data.get("collection_ids", []),
                "media_file_id": data.get("media_file_id"),
                "segment_count": data.get("segment_count", 1),
                "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
                "embedding": data["embedding"],
            }
            if data.get("organization_id") is not None:
                doc_data["organization_id"] = data["organization_id"]
            bulk_body.append(doc_data)

        # Execute bulk operation
        response = _client.opensearch_client.bulk(body=bulk_body)

        if response["errors"]:
            logger.error(f"Bulk indexing had errors: {response}")
        else:
            logger.info(f"Successfully bulk indexed {len(embeddings_data)} speaker embeddings")

        return response

    except Exception as e:
        logger.error(f"Error bulk indexing speaker embeddings: {e}")
