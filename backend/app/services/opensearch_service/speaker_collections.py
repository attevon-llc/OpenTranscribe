"""Speaker-collection membership on the speaker index."""

import datetime
import logging
from typing import Any

from opensearchpy.exceptions import OpenSearchException

from app.core.embedding_models import get_speaker_index
from app.services.opensearch_service import client as _client

logger = logging.getLogger(__name__)


def update_speaker_collections(
    speaker_uuid: str, profile_id: int, profile_uuid: str, collection_ids: list[int]
) -> dict[str, Any] | None:
    """
    Update speaker embedding collections when a speaker is labeled/assigned to profile

    Args:
        speaker_uuid: Speaker UUID
        profile_id: Profile ID the speaker is assigned to (for internal queries)
        profile_uuid: Profile UUID the speaker is assigned to
        collection_ids: List of collection IDs to assign
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return None

    try:
        # Update the speaker document in OpenSearch using UUID
        update_body = {
            "doc": {
                "profile_id": profile_id,
                "profile_uuid": str(profile_uuid) if profile_uuid else None,
                "collection_ids": collection_ids,
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        }

        response: dict[str, Any] = _client.opensearch_client.update(
            index=get_speaker_index(),
            id=str(speaker_uuid),
            body=update_body,
        )

        logger.info(f"Updated speaker {speaker_uuid} collections: {collection_ids}")
        return response

    except OpenSearchException as e:
        logger.exception(f"Error updating speaker collections: {e}")
        return None


def move_speaker_to_profile_collection(
    unlabeled_speaker_uuid: str,
    target_profile_id: int,
    target_profile_uuid: str,
    target_collection_ids: list[int],
) -> dict[str, Any] | None:
    """
    Move an unlabeled speaker embedding to a profile's collection

    Args:
        unlabeled_speaker_uuid: UUID of the unlabeled speaker
        target_profile_id: ID of the target profile (for internal queries)
        target_profile_uuid: UUID of the target profile
        target_collection_ids: Target collection IDs
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return None

    try:
        # Update the speaker's profile and collection assignments using UUID
        update_body = {
            "doc": {
                "profile_id": target_profile_id,
                "profile_uuid": str(target_profile_uuid) if target_profile_uuid else None,
                "collection_ids": target_collection_ids,
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        }

        response: dict[str, Any] = _client.opensearch_client.update(
            index=get_speaker_index(),
            id=str(unlabeled_speaker_uuid),
            body=update_body,
        )

        logger.info(f"Moved speaker {unlabeled_speaker_uuid} to profile {target_profile_uuid}")
        return response

    except OpenSearchException as e:
        logger.exception(f"Error moving speaker to profile collection: {e}")
        return None


def bulk_update_collection_assignments(updates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Bulk update collection assignments for multiple speakers

    Args:
        updates: List of update dictionaries with speaker_uuid, profile_id, profile_uuid, collection_ids
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return None

    try:
        # Prepare bulk update operations
        bulk_body: list[dict[str, Any]] = []
        for update in updates:
            # Update action using UUID as document ID
            bulk_body.append(
                {
                    "update": {
                        "_index": get_speaker_index(),
                        "_id": str(update["speaker_uuid"]),
                    }
                }
            )

            # Update document
            doc_update: dict[str, Any] = {
                "doc": {
                    "profile_id": update.get("profile_id"),
                    "profile_uuid": str(update.get("profile_uuid"))
                    if update.get("profile_uuid")
                    else None,
                    "collection_ids": update.get("collection_ids", []),
                    "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
                }
            }
            bulk_body.append(doc_update)

        # Execute bulk operation
        response: dict[str, Any] = _client.opensearch_client.bulk(body=bulk_body)

        if response["errors"]:
            logger.error(f"Bulk collection update had errors: {response}")
        else:
            logger.info(f"Successfully updated collections for {len(updates)} speakers")

        return response

    except OpenSearchException as e:
        logger.exception(f"Error bulk updating collection assignments: {e}")
        return None
