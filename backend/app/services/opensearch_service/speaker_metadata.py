"""Non-embedding speaker document metadata updates and lookups."""

import datetime
import logging
from typing import Any

from app.core.config import settings
from app.core.constants import get_speaker_index
from app.services.opensearch_service import client as _client
from app.services.opensearch_service.indices import ensure_indices_exist

logger = logging.getLogger(__name__)


def find_speaker_across_media(speaker_uuid: str, user_id: int) -> list[dict[str, Any]]:
    """
    Find all media files where a specific speaker appears

    Args:
        speaker_uuid: UUID of the speaker
        user_id: ID of the user

    Returns:
        List of media files where this speaker appears
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return []

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        # First, get the speaker's name from the speaker index using UUID
        speaker_doc = _client.opensearch_client.get(index=get_speaker_index(), id=str(speaker_uuid))

        if not speaker_doc or "_source" not in speaker_doc:
            return []

        speaker_name = speaker_doc["_source"]["name"]

        # Search for transcripts containing this speaker
        size_limit = 100
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"user_id": user_id}},
                        {"term": {"speakers": speaker_name}},
                    ]
                }
            },
            "size": size_limit,  # Max media files returned; increase if users have very large libraries
            "_source": ["file_id", "file_uuid", "title", "upload_time"],
        }

        response = _client.opensearch_client.search(
            index=settings.OPENSEARCH_TRANSCRIPT_INDEX, body=query
        )

        total_hits = response["hits"]["total"]["value"]
        if total_hits > size_limit:
            logger.warning(
                "Results truncated: %d hits but size limit is %d",
                total_hits,
                size_limit,
            )

        # Process results
        results = []
        for hit in response["hits"]["hits"]:
            source = hit["_source"]
            results.append(
                {
                    "file_id": source["file_id"],
                    "file_uuid": source.get("file_uuid"),
                    "title": source["title"],
                    "upload_time": source["upload_time"],
                }
            )

        return results

    except Exception as e:
        logger.error(f"Error finding speaker across media: {e}")
        return []


def update_speaker_segment_count(speaker_uuid: str, segment_count: int) -> bool:
    """
    Update only the segment_count of a speaker in OpenSearch.

    Args:
        speaker_uuid: UUID of the speaker
        segment_count: New segment count value

    Returns:
        True if successful, False otherwise
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    try:
        update_body = {
            "doc": {
                "segment_count": segment_count,
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        }

        _client.opensearch_client.update(
            index=get_speaker_index(),
            id=str(speaker_uuid),
            body=update_body,
        )

        logger.info(f"Updated segment_count for speaker {speaker_uuid} to {segment_count}")
        return True

    except Exception as e:
        logger.warning(f"Error updating speaker segment count: {e}")
        return False


def update_speaker_display_name(speaker_uuid: str, display_name: str | None):
    """
    Update the display name of a speaker in OpenSearch

    Args:
        speaker_uuid: UUID of the speaker
        display_name: New display name (or None to clear)
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return

    try:
        # Update the speaker document with new display name using UUID
        update_body = {
            "doc": {
                "display_name": display_name,
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        }

        response = _client.opensearch_client.update(
            index=get_speaker_index(),
            id=str(speaker_uuid),
            body=update_body,
            refresh="wait_for",
        )

        logger.info(f"Updated display name for speaker {speaker_uuid} to '{display_name}'")
        return response

    except Exception as e:
        logger.error(f"Error updating speaker display name: {e}")


def update_speaker_profile(
    speaker_uuid: str,
    profile_id: int | None,
    profile_uuid: str | None,
    verified: bool = False,
    display_name: str | None = None,
):
    """
    Update the profile assignment of a speaker in OpenSearch

    Args:
        speaker_uuid: UUID of the speaker
        profile_id: Profile ID to assign (or None to clear, for internal queries)
        profile_uuid: Profile UUID to assign (or None to clear)
        verified: Whether the speaker is verified
        display_name: Optional display name to sync
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return

    try:
        # Update the speaker document with new profile assignment using UUID
        doc: dict = {
            "profile_id": profile_id,
            "profile_uuid": str(profile_uuid) if profile_uuid else None,
            "verified": verified,
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        if display_name is not None:
            doc["display_name"] = display_name
        update_body = {"doc": doc}

        response = _client.opensearch_client.update(
            index=get_speaker_index(),
            id=str(speaker_uuid),
            body=update_body,
        )

        logger.info(
            f"Updated profile assignment for speaker {speaker_uuid} to profile {profile_uuid}, verified={verified}"
        )
        return response

    except Exception as e:
        logger.error(f"Error updating speaker profile assignment: {e}")


def sync_speaker_profiles_to_opensearch(db) -> dict:
    """Bulk-sync speaker profile_id, display_name, and verified from PostgreSQL to OpenSearch.

    Finds all speakers that have a profile_id or display_name in PostgreSQL and
    updates their corresponding OpenSearch documents. This repairs drift caused by
    profile assignments that bypassed the normal API update path.

    Returns:
        Dict with counts: updated, skipped, errors.
    """
    from app.models.media import Speaker

    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping profile sync")
        return {"updated": 0, "skipped": 0, "errors": 0}

    speakers = (
        db.query(Speaker)
        .filter((Speaker.profile_id.isnot(None)) | (Speaker.display_name.isnot(None)))
        .all()
    )

    updated = 0
    skipped = 0
    errors = 0

    for speaker in speakers:
        speaker_uuid = str(speaker.uuid)
        try:
            # update_speaker_profile() catches its OWN exceptions and never
            # raises (it is a fire-and-forget helper for its other callers),
            # so a try/except around it can never observe a failed write —
            # every call used to count as "updated" even when the OpenSearch
            # document did not exist and nothing was written. Checking
            # existence first, and reading the (dict-or-None) return value,
            # is what actually distinguishes the three outcomes.
            if not _client.opensearch_client.exists(index=get_speaker_index(), id=speaker_uuid):
                skipped += 1
                continue

            profile_uuid = str(speaker.profile.uuid) if speaker.profile else None
            response = update_speaker_profile(
                speaker_uuid=speaker_uuid,
                profile_id=int(speaker.profile_id) if speaker.profile_id else None,
                profile_uuid=profile_uuid,
                verified=bool(speaker.verified) if hasattr(speaker, "verified") else False,
                display_name=str(speaker.display_name) if speaker.display_name else None,
            )
            if response is None:
                errors += 1
            else:
                updated += 1
        except Exception as e:
            logger.warning(f"Error syncing speaker {speaker_uuid}: {e}")
            errors += 1

    logger.info(
        f"Speaker profile sync complete: {updated} updated, {skipped} skipped "
        f"(no OS doc), {errors} errors"
    )
    return {"updated": updated, "skipped": skipped, "errors": errors}
