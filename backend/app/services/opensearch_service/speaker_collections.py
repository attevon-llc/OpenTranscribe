"""Speaker-collection membership on the speaker index."""

import datetime
import logging
from typing import Any

from app.core.constants import get_speaker_index
from app.services.opensearch_service import client as _client
from app.services.opensearch_service.indices import ensure_indices_exist

logger = logging.getLogger(__name__)


def update_speaker_collections(
    speaker_uuid: str, profile_id: int, profile_uuid: str, collection_ids: list[int]
):
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
        return

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

        response = _client.opensearch_client.update(
            index=get_speaker_index(),
            id=str(speaker_uuid),
            body=update_body,
        )

        logger.info(f"Updated speaker {speaker_uuid} collections: {collection_ids}")
        return response

    except Exception as e:
        logger.error(f"Error updating speaker collections: {e}")


def move_speaker_to_profile_collection(
    unlabeled_speaker_uuid: str,
    target_profile_id: int,
    target_profile_uuid: str,
    target_collection_ids: list[int],
):
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
        return

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

        response = _client.opensearch_client.update(
            index=get_speaker_index(),
            id=str(unlabeled_speaker_uuid),
            body=update_body,
        )

        logger.info(f"Moved speaker {unlabeled_speaker_uuid} to profile {target_profile_uuid}")
        return response

    except Exception as e:
        logger.error(f"Error moving speaker to profile collection: {e}")


def bulk_update_collection_assignments(updates: list[dict[str, Any]]):
    """
    Bulk update collection assignments for multiple speakers

    Args:
        updates: List of update dictionaries with speaker_uuid, profile_id, profile_uuid, collection_ids
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return

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
        response = _client.opensearch_client.bulk(body=bulk_body)

        if response["errors"]:
            logger.error(f"Bulk collection update had errors: {response}")
        else:
            logger.info(f"Successfully updated collections for {len(updates)} speakers")

        return response

    except Exception as e:
        logger.error(f"Error bulk updating collection assignments: {e}")


def get_speakers_in_collection(collection_id: int, user_id: int) -> list[dict[str, Any]]:
    """
    Get all speakers in a specific collection

    Args:
        collection_id: Collection ID
        user_id: User ID

    Returns:
        List of speaker documents in the collection
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return []

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        size_limit = 1000
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"user_id": user_id}},
                        {"term": {"collection_ids": collection_id}},
                    ]
                }
            },
            "size": size_limit,  # Upper bound for speakers per collection; log warning if exceeded
            "_source": [
                "speaker_id",
                "speaker_uuid",
                "profile_id",
                "profile_uuid",
                "name",
                "media_file_id",
                "segment_count",
                "created_at",
            ],
        }

        response = _client.opensearch_client.search(index=get_speaker_index(), body=query)

        total_hits = response["hits"]["total"]["value"]
        if total_hits > size_limit:
            logger.warning(
                "Results truncated: %d hits but size limit is %d",
                total_hits,
                size_limit,
            )

        speakers = []
        for hit in response["hits"]["hits"]:
            source = hit["_source"]
            speakers.append(
                {
                    "speaker_id": source["speaker_id"],
                    "speaker_uuid": source.get("speaker_uuid"),
                    "profile_id": source.get("profile_id"),
                    "profile_uuid": source.get("profile_uuid"),
                    "name": source["name"],
                    "media_file_id": source.get("media_file_id"),
                    "segment_count": source.get("segment_count", 1),
                    "created_at": source.get("created_at"),
                }
            )

        return speakers

    except Exception as e:
        logger.error(f"Error getting speakers in collection: {e}")
        return []
