"""Speaker embedding removal, merging, and orphan cleanup."""

import datetime
import logging

from opensearchpy.exceptions import NotFoundError

from app.core.constants import get_speaker_index
from app.core.constants import get_speaker_index_v3
from app.core.constants import get_speaker_index_v4
from app.services.opensearch_service import client as _client
from app.services.opensearch_service.client import CLUSTER_UNAVAILABLE_ERRORS
from app.services.opensearch_service.client import OpenSearchUnavailableError
from app.services.opensearch_service.client import _is_alias
from app.services.opensearch_service.client import _safe_index_exists
from app.services.opensearch_service.indices import ensure_indices_exist

logger = logging.getLogger(__name__)


def merge_speaker_embeddings(
    source_speaker_uuid: str, target_speaker_uuid: str, new_collection_ids: list[int]
):
    """
    Merge two speaker embeddings (used when combining speakers)

    Args:
        source_speaker_uuid: UUID of speaker to merge from
        target_speaker_uuid: UUID of speaker to merge into
        new_collection_ids: Updated collection IDs for the target
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return

    try:
        # Delete the source speaker document from main index
        _client.opensearch_client.delete(index=get_speaker_index(), id=str(source_speaker_uuid))

        # Also remove source from v4 staging index if it exists (mid-migration cleanup)
        import contextlib as _ctx

        v4_index = get_speaker_index_v4()
        with _ctx.suppress(Exception):
            if _client.opensearch_client.indices.exists(index=v4_index):
                _client.opensearch_client.delete(index=v4_index, id=str(source_speaker_uuid))

        # Update the target speaker's collections using UUID
        update_body = {
            "doc": {
                "collection_ids": new_collection_ids,
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        }

        response = _client.opensearch_client.update(
            index=get_speaker_index(),
            id=str(target_speaker_uuid),
            body=update_body,
        )

        logger.info(f"Merged speaker {source_speaker_uuid} into {target_speaker_uuid}")
        return response

    except Exception as e:
        logger.error(f"Error merging speaker embeddings: {e}")


def cleanup_orphaned_embeddings(user_id: int) -> dict:
    """Count potentially orphaned speaker embeddings.

    NOTE: This is a diagnostic stub. Actual cleanup requires database
    validation and is not yet implemented. Use
    ``cleanup_orphaned_speaker_embeddings()`` for real orphan removal.

    Args:
        user_id: User ID to inspect.

    Returns:
        Dict with embedding_count and diagnostic status.
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return {"embedding_count": 0, "status": "diagnostic_only"}

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        # Find all embeddings for user
        query = {
            "query": {"term": {"user_id": user_id}},
            "size": 1000,
            "_source": ["speaker_id", "profile_id"],
        }

        response = _client.opensearch_client.search(index=get_speaker_index(), body=query)

        count = len(response["hits"]["hits"])
        logger.info(
            f"Found {count} embeddings for user {user_id} (diagnostic only, no cleanup performed)"
        )

        return {"embedding_count": count, "status": "diagnostic_only"}

    except Exception as e:
        logger.error(f"Error counting orphaned embeddings: {e}")
        return {"embedding_count": 0, "status": "diagnostic_only"}


def remove_speaker_embedding(speaker_uuid: str) -> bool:
    """Remove a speaker embedding from all speaker indices (main + v4 staging).

    Cleans both the main speaker index (V3 or post-finalization V4) and the
    v4 staging index if it exists. This prevents orphaned entries when speakers
    are deleted or merged.

    Args:
        speaker_uuid: UUID of the speaker

    Returns:
        True if the main index deletion succeeded, False otherwise
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    success = False

    # Delete from all speaker indices (v3, v4, and alias target). Each index is
    # independent: a speaker normally lives in only one of them, so "absent" is
    # the common case and must not stop the loop.
    indices_to_clean = {get_speaker_index(), get_speaker_index_v3(), get_speaker_index_v4()}
    for idx in indices_to_clean:
        try:
            if _safe_index_exists(idx) or _is_alias(idx):
                _client.opensearch_client.delete(index=idx, id=str(speaker_uuid))
                logger.debug(f"Removed speaker {speaker_uuid} from {idx}")
                success = True
        except NotFoundError:
            logger.debug(f"Speaker {speaker_uuid} not present in {idx}")
        except (OpenSearchUnavailableError, *CLUSTER_UNAVAILABLE_ERRORS) as e:
            # A real failure to delete leaves an orphan voiceprint behind, so it
            # is reported (the periodic consistency task reconciles it) but does
            # not abort cleanup of the remaining indices.
            logger.warning(f"Could not remove speaker {speaker_uuid} from {idx}: {e}")

    if success:
        logger.info(f"Removed speaker {speaker_uuid} from speaker indices")

    return success


def cleanup_orphaned_speaker_embeddings(user_id: int) -> int:
    """
    Remove speaker embeddings from OpenSearch for MediaFiles that no longer exist in PostgreSQL.

    Args:
        user_id: ID of the user to clean up orphaned documents for

    Returns:
        Number of orphaned documents removed
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return 0

    try:
        from app.db.session_utils import session_scope
        from app.models.media import MediaFile

        with session_scope() as db:
            # Get all existing MediaFile IDs for this user
            existing_media_file_ids = set(
                row[0] for row in db.query(MediaFile.id).filter(MediaFile.user_id == user_id).all()
            )
            logger.info(
                f"Found {len(existing_media_file_ids)} existing MediaFiles for user {user_id}: {existing_media_file_ids}"
            )

        # Query OpenSearch for all speaker documents for this user
        query = {
            "size": 1000,  # Adjust if needed
            "query": {
                "bool": {
                    "must": [
                        {"term": {"user_id": user_id}},
                        {
                            "bool": {"must_not": {"exists": {"field": "document_type"}}}
                        },  # Only speaker docs, not profiles
                    ]
                }
            },
            "_source": ["speaker_id", "speaker_uuid", "media_file_id"],
        }

        response = _client.opensearch_client.search(index=get_speaker_index(), body=query)

        orphaned_speaker_uuids = []
        for hit in response["hits"]["hits"]:
            source = hit["_source"]
            media_file_id = source.get("media_file_id")
            speaker_id = source.get("speaker_id")
            speaker_uuid = source.get("speaker_uuid")

            if media_file_id and media_file_id not in existing_media_file_ids:
                orphaned_speaker_uuids.append(speaker_uuid)
                logger.info(
                    f"Found orphaned speaker {speaker_uuid} (ID: {speaker_id}) referencing non-existent MediaFile {media_file_id}"
                )

        # Delete orphaned documents using UUIDs
        deleted_count = 0
        for speaker_uuid in orphaned_speaker_uuids:
            try:
                _client.opensearch_client.delete(index=get_speaker_index(), id=str(speaker_uuid))
                logger.info(f"Deleted orphaned speaker document for speaker {speaker_uuid}")
                deleted_count += 1
            except Exception as e:
                logger.error(f"Error deleting orphaned speaker {speaker_uuid}: {e}")

        logger.info(
            f"Cleanup completed: removed {deleted_count} orphaned speaker documents for user {user_id}"
        )
        return deleted_count

    except Exception as e:
        logger.error(f"Error during orphaned speaker cleanup: {e}")
        return 0
