"""Non-embedding speaker document metadata updates and lookups."""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING
from typing import Any

from opensearchpy.exceptions import OpenSearchException

from app.core.embedding_models import get_speaker_index
from app.services.opensearch_service import client as _client

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


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

    except OpenSearchException as e:
        logger.warning(f"Error updating speaker segment count: {e}", exc_info=True)
        return False


def update_speaker_display_name(
    speaker_uuid: str, display_name: str | None
) -> dict[str, Any] | None:
    """
    Update the display name of a speaker in OpenSearch

    Args:
        speaker_uuid: UUID of the speaker
        display_name: New display name (or None to clear)
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return None

    try:
        # Update the speaker document with new display name using UUID
        update_body = {
            "doc": {
                "display_name": display_name,
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        }

        response: dict[str, Any] = _client.opensearch_client.update(
            index=get_speaker_index(),
            id=str(speaker_uuid),
            body=update_body,
            refresh="wait_for",
        )

        logger.info(f"Updated display name for speaker {speaker_uuid} to '{display_name}'")
        return response

    except OpenSearchException as e:
        logger.exception(f"Error updating speaker display name: {e}")
        return None


def update_speaker_profile(
    speaker_uuid: str,
    profile_id: int | None,
    profile_uuid: str | None,
    verified: bool = False,
    display_name: str | None = None,
) -> dict[str, Any] | None:
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
        return None

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

        response: dict[str, Any] = _client.opensearch_client.update(
            index=get_speaker_index(),
            id=str(speaker_uuid),
            body=update_body,
        )

        logger.info(
            f"Updated profile assignment for speaker {speaker_uuid} to profile {profile_uuid}, verified={verified}"
        )
        return response

    except OpenSearchException as e:
        logger.exception(f"Error updating speaker profile assignment: {e}")
        return None


#: Speakers read (and documents fetched) per round trip by the profile sync.
SYNC_CHUNK_SIZE = 500

_SYNCED_FIELDS = ["profile_id", "profile_uuid", "display_name", "verified"]


def sync_speaker_profiles_to_opensearch(db: Session, *, chunk_size: int | None = None) -> dict:
    """Bulk-sync speaker profile_id, display_name, and verified from PostgreSQL to OpenSearch.

    Finds all speakers that have a profile_id or display_name in PostgreSQL and
    updates their corresponding OpenSearch documents. This repairs drift caused by
    profile assignments that bypassed the normal API update path.

    Works in id-keyed chunks: one SELECT of plain columns (profile UUID joined in, so no lazy
    ``speaker.profile`` loads), the transaction ended, then one ``mget`` for the chunk's
    documents and one bulk request for the documents that actually differ. No database
    transaction is held across an OpenSearch call, and a document already in step is left
    alone, so a repeat run converges to zero updates.
    (https://opensearch.org/docs/latest/api-reference/document-apis/multi-get/,
    https://opensearch.org/docs/latest/api-reference/document-apis/bulk/)

    Returns:
        Dict with counts: updated, skipped (no document), errors.
    """
    from opensearchpy import helpers
    from opensearchpy.exceptions import OpenSearchException
    from sqlalchemy import or_

    from app.models.speaker import Speaker
    from app.models.speaker import SpeakerProfile

    client = _client.opensearch_client
    if not client:
        logger.warning("OpenSearch client not initialized, skipping profile sync")
        return {"updated": 0, "skipped": 0, "errors": 0}

    size = chunk_size or SYNC_CHUNK_SIZE
    index = get_speaker_index()
    updated = skipped = errors = 0
    last_id = 0

    while True:
        rows = (
            db.query(
                Speaker.id,
                Speaker.uuid,
                Speaker.profile_id,
                Speaker.display_name,
                Speaker.verified,
                SpeakerProfile.uuid.label("profile_uuid"),
            )
            .outerjoin(SpeakerProfile, SpeakerProfile.id == Speaker.profile_id)
            .filter(
                Speaker.id > last_id,
                or_(Speaker.profile_id.isnot(None), Speaker.display_name.isnot(None)),
            )
            .order_by(Speaker.id)
            .limit(size)
            .all()
        )
        # The rows are plain tuples: end the read transaction before touching the network.
        db.commit()
        if not rows:
            break
        last_id = int(rows[-1].id)

        try:
            fetched = client.mget(
                index=index,
                body={"ids": [str(r.uuid) for r in rows]},
                _source_includes=_SYNCED_FIELDS,
            )["docs"]
        except OpenSearchException as e:
            logger.warning(f"Speaker profile sync: mget failed for a chunk of {len(rows)}: {e}")
            errors += len(rows)
            continue
        documents = {d["_id"]: d for d in fetched}

        now = datetime.datetime.now(datetime.UTC).isoformat()
        actions: list[dict[str, Any]] = []
        for row in rows:
            speaker_uuid = str(row.uuid)
            document = documents.get(speaker_uuid)
            if not document or not document.get("found"):
                skipped += 1
                continue
            wanted: dict[str, Any] = {
                "profile_id": int(row.profile_id) if row.profile_id else None,
                "profile_uuid": str(row.profile_uuid) if row.profile_uuid else None,
                "verified": bool(row.verified),
            }
            if row.display_name:
                wanted["display_name"] = str(row.display_name)
            stored = document.get("_source") or {}
            if all(stored.get(k) == v for k, v in wanted.items()):
                continue
            actions.append(
                {
                    "_op_type": "update",
                    "_index": index,
                    "_id": speaker_uuid,
                    "doc": {**wanted, "updated_at": now},
                }
            )

        if actions:
            try:
                ok, failures = helpers.bulk(
                    client, actions, raise_on_error=False, raise_on_exception=False
                )
                updated += int(ok)
                errors += len(failures) if isinstance(failures, list) else 0
            except OpenSearchException as e:
                logger.warning(f"Speaker profile sync: bulk update failed for a chunk: {e}")
                errors += len(actions)

    logger.info(
        f"Speaker profile sync complete: {updated} updated, {skipped} skipped "
        f"(no OS doc), {errors} errors"
    )
    return {"updated": updated, "skipped": skipped, "errors": errors}
