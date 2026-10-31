"""Speaker merge route and its OpenSearch/embedding/analytics helpers."""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.speakers_profile_workflow import _clear_video_cache_for_speaker
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.user import User
from app.schemas.speaker import Speaker as SpeakerSchema
from app.services.privacy.redact_view import redact_speaker_rows
from app.services.request_context import RequestContext
from app.services.speaker_cleanup import capture_speaker_impact
from app.services.speaker_cleanup import raise_if_incomplete
from app.services.speaker_cleanup import reconcile_speaker_impact
from app.services.speaker_identity_rules import carry_blacklist
from app.services.speaker_provenance_view import attach_speaker_provenance
from app.services.speaker_status_service import SpeakerStatusService
from app.services.uuid_helpers import require_speaker_access

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/{speaker_uuid}/merge/{target_speaker_uuid}", response_model=SpeakerSchema)
def merge_speakers(
    request: Request,
    speaker_uuid: str,
    target_speaker_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Merge two speakers into one (target absorbs source).

    Both speakers need editor or better in the active tenant, must belong to the
    same tenant, and must be in the same file or have the same owner — a merge
    moves the source's transcript segments onto the target.

    Postgres is updated synchronously so the response is authoritative, and so are the
    clusters and profile the SOURCE belonged to: it is no longer their member, so
    ``speaker_cleanup`` rebuilds (or deletes, if emptied) them before the response
    (issue #1214). Everything downstream of the target — averaging the two voiceprints
    in OpenSearch, deleting the source document, rebuilding the target's clusters and
    profile, clearing the MinIO video cache and refreshing analytics for both files — is
    handed to ``process_speaker_merge_background`` (issue #284 A2.6).
    """
    from app.tasks.speaker_merge_task import process_speaker_merge_background

    source_speaker = require_speaker_access(
        db,
        speaker_uuid,
        current_user,
        organization_id=ctx.org_id,
        min_permission="editor",
        forbidden_detail="Requires editor permission",
        bypass=ctx.bypass,
    )
    target_speaker = require_speaker_access(
        db,
        target_speaker_uuid,
        current_user,
        organization_id=ctx.org_id,
        min_permission="editor",
        forbidden_detail="Requires editor permission",
        bypass=ctx.bypass,
    )
    if source_speaker.media_file.organization_id != target_speaker.media_file.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Speaker not found")
    if (
        source_speaker.media_file_id != target_speaker.media_file_id
        and source_speaker.user_id != target_speaker.user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Speakers must be in the same file or have the same owner",
        )

    # Store profile IDs for embedding updates
    source_profile_id = int(source_speaker.profile_id) if source_speaker.profile_id else None
    target_profile_id = int(target_speaker.profile_id) if target_speaker.profile_id else None
    source_speaker_id = source_speaker.id

    # Update all transcript segments from source to target
    db.query(TranscriptSegment).filter(TranscriptSegment.speaker_id == source_speaker.id).update(
        {"speaker_id": target_speaker.id}
    )

    # Get media file IDs that are affected
    affected_media_files = {int(source_speaker.media_file_id), int(target_speaker.media_file_id)}

    # Capture the source identity before the row disappears
    source_speaker_uuid = str(source_speaker.uuid)
    target_speaker_uuid_str = str(target_speaker.uuid)
    # The file whose segments just moved onto the target (the source's own).
    segments_file = source_speaker.media_file
    # ...and the clusters/profile it belonged to, which the CASCADE is about to forget.
    # Its voiceprint stays until the background task has folded it into the target.
    source_impact = capture_speaker_impact(db, [source_speaker_id], removed=False, reindex=False)
    # The source's voice now lives in the target, so a correction the user made on it
    # (not this profile, not that speaker) must outlive the CASCADE that drops its rows.
    carry_blacklist(db, int(source_speaker_id), int(target_speaker.id))

    # Delete the source speaker
    db.delete(source_speaker)
    db.commit()

    # A merge rewrites the speaker of every segment the source had, which is an edit of the
    # transcript: on a held file it is allowed and recorded (issue #1307). After the commit,
    # so a rolled-back merge leaves no event; nothing is written for a file nobody holds.
    from app.services.content_hold_service import record_edit_on_held_file

    record_edit_on_held_file(
        db,
        request,
        current_user,
        segments_file,
        kind="speaker_merge",
        speaker_uuids=[source_speaker_uuid, target_speaker_uuid_str],
        bypass=ctx.bypass,
    )

    # The source is no longer a member: its clusters and profile drop it NOW, not when the
    # background task gets to it (issue #1214). The target side waits for the averaged
    # voiceprint and is rebuilt by process_speaker_merge_background. A failure is raised
    # only after the rest of the merge has been dispatched.
    source_residual = reconcile_speaker_impact(db, source_impact)
    db.refresh(target_speaker)

    # Hand the OpenSearch / MinIO / analytics work to the background worker
    process_speaker_merge_background.delay(
        source_speaker_uuid=source_speaker_uuid,
        target_speaker_uuid=str(target_speaker.uuid),
        user_id=current_user.id,
        source_speaker_id=source_speaker_id,
        source_profile_id=source_profile_id,
        target_profile_id=target_profile_id,
        media_file_ids=sorted(affected_media_files),
    )
    logger.info(
        f"Queued background merge processing for {source_speaker_uuid} -> {target_speaker.uuid}"
    )

    # `_update_opensearch_speaker_merge` (in the background task above) only
    # touches the speaker/voiceprint index — every segment that used to belong
    # to the source speaker was just reassigned to the target in Postgres, but
    # the chunk plane's `speaker`/`speakers` snapshot and turn grouping for
    # both files kept the pre-merge attribution until a full reindex ran
    # (issue #666). One file when source and target share a recording, two
    # when they don't.
    from app.services.search.reindex_dispatch import dispatch_transcript_reindex

    for media_file_id in affected_media_files:
        merged_file = db.query(MediaFile).filter(MediaFile.id == media_file_id).first()
        if merged_file:
            dispatch_transcript_reindex(
                file_id=merged_file.id,
                file_uuid=str(merged_file.uuid),
                user_id=int(merged_file.user_id),
            )

    # Invalidate caches
    try:
        from app.services.redis_cache_service import redis_cache

        redis_cache.invalidate_speakers(current_user.id)
        redis_cache.invalidate_user_files(current_user.id)
    except Exception as e:
        logger.debug(f"Cache invalidation failed (non-critical): {e}")

    raise_if_incomplete(source_residual)

    # Add computed status fields
    redact_speaker_rows(db, [target_speaker])
    attach_speaker_provenance(db, [target_speaker], ctx)
    SpeakerStatusService.add_computed_status(target_speaker)

    return target_speaker


# --- Helper functions for merge_speakers ---


def _merge_speaker_embeddings(source_speaker_uuid: str, target: dict[str, Any]) -> None:
    """Merge and average speaker embeddings in OpenSearch.

    Takes the source speaker's UUID rather than the ORM row: this now runs in
    ``process_speaker_merge_background`` (issue #284 A2.6), by which time the source
    Speaker row has already been deleted from Postgres. Only its OpenSearch document
    is still needed, and that is removed later in the same task by
    ``_update_opensearch_speaker_merge`` — so the read here still precedes the delete.

    ``target`` is the surviving speaker as **plain data** (see
    ``speaker_merge_task._load_merge_plan``), not a ``Speaker``. Two OpenSearch reads
    and one write run here; a live ORM instance would lazy-load mid-flight and open a
    transaction underneath them, which is the leak this split exists to remove.
    """
    try:
        import numpy as np

        from app.services.opensearch_service import add_speaker_embedding
        from app.services.opensearch_service import get_speaker_embedding

        # Get embeddings for both speakers
        source_embedding = get_speaker_embedding(str(source_speaker_uuid))
        target_embedding = get_speaker_embedding(target["uuid"])

        if source_embedding and target_embedding:
            # Average the embeddings
            embeddings_array = np.array([source_embedding, target_embedding])
            averaged_embedding = np.mean(embeddings_array, axis=0).tolist()

            # Store the averaged embedding in OpenSearch
            add_speaker_embedding(
                speaker_uuid=target["uuid"],
                speaker_id=target["id"],
                user_id=target["user_id"],
                name=target["name"],
                embedding=averaged_embedding,
                profile_id=target["profile_id"],
                media_file_id=target["media_file_id"],
                display_name=target["display_name"],
                segment_count=2,  # Merged from 2 speakers
                # The write replaces the whole document: without the file's tenant an org
                # speaker's voiceprint would come back unstamped, i.e. personal.
                organization_id=target.get("organization_id"),
            )
            logger.info(f"Updated target speaker {target['id']} with averaged embedding")
        else:
            logger.warning("Could not retrieve embeddings for speaker merge")
    except Exception as e:
        logger.exception(f"Error averaging speaker embeddings during merge: {e}")


def _clear_speaker_video_cache(affected_media_files: set[int]) -> None:
    """Clear video cache for affected media files after speaker merge.

    **Takes no caller-owned ``Session``.** One short session per file (via
    ``_clear_video_cache_for_speaker``) rather than one transaction spanning the
    merge task, which previously stayed open across every object delete for both
    sides of the merge.
    """
    for media_file_id in sorted(affected_media_files):
        _clear_video_cache_for_speaker(media_file_id)


def _update_opensearch_speaker_merge(source_speaker_uuid: str, target_speaker_uuid: str) -> None:
    """Update OpenSearch index after speaker merge."""
    try:
        from app.core.embedding_models import get_speaker_index
        from app.services.opensearch_service import client as _os_client
        from app.services.opensearch_service import merge_speaker_embeddings

        # Preserve the target's current collection membership — merge_speaker_embeddings
        # overwrites collection_ids with whatever it's given, and the target's own
        # membership (not the source's, which is being deleted) is what must survive.
        target_collection_ids: list[int] = []
        try:
            if _os_client.opensearch_client is not None:
                existing = _os_client.opensearch_client.get(
                    index=get_speaker_index(), id=str(target_speaker_uuid)
                )
                target_collection_ids = existing.get("_source", {}).get("collection_ids") or []
        except Exception as lookup_exc:
            logger.debug(
                f"Could not read existing collection_ids for target speaker "
                f"{target_speaker_uuid}, defaulting to empty: {lookup_exc}"
            )

        merge_speaker_embeddings(source_speaker_uuid, target_speaker_uuid, target_collection_ids)
        logger.info(
            f"Merged speaker embeddings in OpenSearch: {source_speaker_uuid} -> {target_speaker_uuid}"
        )
    except Exception as e:
        logger.exception(f"Error merging speaker embeddings in OpenSearch: {e}")


def _refresh_analytics_after_merge(db: Session, affected_media_files: set[int]) -> None:
    """Recalculate analytics for affected media files after speaker merge."""
    try:
        from app.services.analytics_service import AnalyticsService

        for media_file_id in affected_media_files:
            if AnalyticsService.refresh_analytics(db, media_file_id):
                logger.info(
                    f"Refreshed analytics for media file {media_file_id} after speaker merge"
                )
            else:
                logger.warning(f"Failed to refresh analytics for media file {media_file_id}")
    except Exception as e:
        logger.exception(f"Error refreshing analytics after speaker merge: {e}")


def _reconcile_speakers_after_merge(
    db: Session, source_speaker_uuid: str, target_speaker_id: int
) -> list[dict[str, Any]]:
    """Finish a merge's aggregates once the target holds the averaged voiceprint.

    The source's own clusters and profile were rebuilt by the endpoint; what is left is
    the TARGET side — its clusters and profile now average a different voiceprint — and
    proof that the source's voiceprint document, deleted by
    ``_update_opensearch_speaker_merge``, is gone. One ``speaker_cleanup`` pass.

    Returns:
        The cleanup's residuals; empty when every aggregate was rebuilt or erased.
    """
    impact = capture_speaker_impact(db, [target_speaker_id], removed=False, reindex=False)
    impact.erase_speaker_uuids.add(source_speaker_uuid)
    return reconcile_speaker_impact(db, impact)
