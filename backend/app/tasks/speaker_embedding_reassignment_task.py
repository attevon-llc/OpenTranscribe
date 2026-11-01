"""
Speaker embedding updates when a transcript segment is manually reassigned.

When a segment moves to a different speaker, the target's voiceprint takes the
segment's voice in by weighted average, the source's voiceprint gives it back by the
exact inverse, and every profile / cluster centroid averaging either one is rebuilt.
The voiceprint extraction run after transcription lives in ``speaker_embedding_task``.
"""

import logging

from app.core.celery import celery_app
from app.core.queues import GPUPriority
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.services.privacy.gates import voiceprints_allowed

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True, name="update_speaker_embedding_on_reassignment", priority=GPUPriority.INTERACTIVE
)
def update_speaker_embedding_on_reassignment(
    self,
    segment_uuid: str,
    media_file_uuid: str,
    target_speaker_uuid: str,
    source_speaker_uuid: str | None,
    user_id: int,
):
    """
    Update speaker embeddings after a segment is manually reassigned to a different speaker.

    Extracts the voice embedding from the reassigned segment's audio and incorporates
    it into the target speaker's embedding via weighted average. This enables iterative
    speaker profile refinement for difficult-to-match segments.

    Args:
        segment_uuid: UUID of the reassigned transcript segment
        media_file_uuid: UUID of the media file containing the segment
        target_speaker_uuid: UUID of the speaker that received the segment
        source_speaker_uuid: UUID of the speaker that lost the segment (or None if orphan-deleted)
        user_id: ID of the user who owns the data
    """
    import numpy as np

    from app.services.opensearch_service import add_speaker_embedding
    from app.services.opensearch_service import get_speaker_document

    try:
        # Phase 1 — read (DB only, short). Returns plain data; the ORM objects
        # must not outlive this call or their lazy loads would reopen a
        # transaction inside the download/inference phase below.
        plan = _load_reassignment_plan(segment_uuid, target_speaker_uuid, source_speaker_uuid)
        if "skip" in plan:
            return plan["skip"]

        # Phase 2 — MinIO download, ffmpeg conversion, embedding inference.
        # NO DB session is held here.
        logger.info(
            f"Extracting embedding for segment {segment_uuid} "
            f"(speaker {target_speaker_uuid}, {plan['duration']:.1f}s)"
        )
        embedding_result = _extract_segment_embedding(plan)
        if embedding_result is None:
            logger.warning(f"Failed to extract embedding for segment {segment_uuid}")
            return {"status": "error", "reason": "embedding_extraction_failed"}
        new_embedding = np.array(embedding_result)

        # Phase 3 — OpenSearch only; needs no DB session at all.
        target = plan["target_speaker"]
        existing_doc = get_speaker_document(target_speaker_uuid)

        if existing_doc is None:
            # New speaker with no existing embedding — store directly
            logger.info(
                f"Storing initial embedding for speaker {target_speaker_uuid} "
                f"from segment {segment_uuid}"
            )
            embedding_to_store = new_embedding
            segment_count = 1
        else:
            # Weighted average: (old * count + new) / (count + 1), then L2 normalize
            old_embedding = np.array(existing_doc["embedding"])
            old_count = existing_doc["segment_count"]
            segment_count = old_count + 1

            weighted = (old_embedding * old_count + new_embedding) / segment_count
            norm = np.linalg.norm(weighted)
            if norm > 0:
                weighted = weighted / norm
            embedding_to_store = weighted

            logger.info(
                f"Updating speaker {target_speaker_uuid} embedding: "
                f"segment_count {old_count} -> {segment_count}"
            )

        add_speaker_embedding(
            speaker_id=target["id"],
            speaker_uuid=target_speaker_uuid,
            user_id=user_id,
            name=target["name"],
            embedding=embedding_to_store.tolist(),
            profile_id=target["profile_id"],
            profile_uuid=target["profile_uuid"],
            media_file_id=target["media_file_id"],
            segment_count=segment_count,
            display_name=target["display_name"],
            # The write replaces the whole document: without the file's tenant an org
            # speaker's voiceprint would come back unstamped, i.e. personal.
            organization_id=plan["organization_id"],
        )

        # The segment is no longer the source's (issue #1233): its voice leaves the source's
        # voiceprint by the exact inverse of the weighted add above, so the source's
        # clusters and profile stop averaging audio the user moved to someone else.
        if source_speaker_uuid and plan["source_speaker_exists"]:
            source_doc = get_speaker_document(source_speaker_uuid)
            if source_doc and source_doc["segment_count"] > 1:
                _remove_segment_from_voiceprint(
                    source_speaker_uuid,
                    plan["source_speaker"],
                    source_doc,
                    new_embedding,
                    user_id,
                    organization_id=plan["organization_id"],
                )
                logger.info(f"Removed the moved segment from speaker {source_speaker_uuid}")

        # Phase 4 — every aggregate averaging the shifted voiceprint, in its own session.
        residual = _rebuild_affected_aggregates(plan)
        if residual:
            return {
                "status": "partial",
                "target_speaker_uuid": target_speaker_uuid,
                "cleanup_residual_stages": sorted({str(r["stage"]) for r in residual}),
            }

        logger.info(
            f"Successfully updated embeddings after segment {segment_uuid} "
            f"reassignment to speaker {target_speaker_uuid}"
        )
        return {"status": "success", "target_speaker_uuid": target_speaker_uuid}

    except Exception as e:
        logger.error(f"Error updating speaker embedding on reassignment: {type(e).__name__}: {e}")
        logger.error("Full traceback:", exc_info=True)
        return {"status": "error", "message": str(e)}


def _remove_segment_from_voiceprint(
    speaker_uuid: str,
    source: dict,
    document: dict,
    segment_embedding,
    user_id: int,
    *,
    organization_id: int | None,
) -> None:
    """Take one segment's embedding back out of a voiceprint: ``(old * n - e) / (n - 1)``.

    The inverse of the weighted add the target receives, under the same model (the stored
    vector is the mean of ``segment_count`` segment embeddings). A result too close to
    zero means the voiceprint was little more than that segment; it is left unchanged and
    only the count moves, rather than storing noise as a voice.
    """
    import numpy as np

    from app.services.opensearch_service import add_speaker_embedding
    from app.services.opensearch_service import update_speaker_segment_count

    count = int(document["segment_count"])
    remaining = (np.array(document["embedding"]) * count - np.array(segment_embedding)) / (
        count - 1
    )
    norm = float(np.linalg.norm(remaining))
    if norm < 1e-6:
        update_speaker_segment_count(speaker_uuid, count - 1)
        return
    add_speaker_embedding(
        speaker_id=source["id"],
        speaker_uuid=speaker_uuid,
        user_id=user_id,
        name=source["name"],
        embedding=(remaining / norm).tolist(),
        profile_id=source["profile_id"],
        profile_uuid=source["profile_uuid"],
        media_file_id=source["media_file_id"],
        segment_count=count - 1,
        display_name=source["display_name"],
        organization_id=organization_id,
    )


def _load_reassignment_plan(
    segment_uuid: str, target_speaker_uuid: str, source_speaker_uuid: str | None
) -> dict:
    """Read phase for ``update_speaker_embedding_on_reassignment``.

    Returns plain data only. A ``skip`` key carries the early-return result for
    the guard paths (missing rows, races, too-short segments).
    """
    from app.services.uuid_helpers import get_by_uuid

    with session_scope() as db:
        segment = get_by_uuid(db, TranscriptSegment, segment_uuid)
        if not segment:
            logger.warning(f"Segment {segment_uuid} not found, skipping embedding update")
            return {"skip": {"status": "skipped", "reason": "segment_not_found"}}

        target_speaker = get_by_uuid(db, Speaker, target_speaker_uuid)
        if not target_speaker:
            logger.warning(
                f"Target speaker {target_speaker_uuid} not found, skipping embedding update"
            )
            return {"skip": {"status": "skipped", "reason": "target_speaker_not_found"}}

        # Guard against race conditions: verify segment still belongs to target speaker
        if segment.speaker_id != target_speaker.id:
            logger.info(
                f"Segment {segment_uuid} no longer belongs to speaker {target_speaker_uuid} "
                f"(race condition), skipping"
            )
            return {"skip": {"status": "skipped", "reason": "segment_reassigned"}}

        # Skip segments shorter than 0.5s (unreliable embeddings)
        duration = float(segment.end_time) - float(segment.start_time)
        if duration < 0.5:
            logger.info(
                f"Segment {segment_uuid} too short ({duration:.2f}s), skipping embedding update"
            )
            return {"skip": {"status": "skipped", "reason": "segment_too_short"}}

        media_file = db.query(MediaFile).filter(MediaFile.id == segment.media_file_id).first()
        if not media_file:
            logger.error(f"Media file not found for segment {segment_uuid}")
            return {"skip": {"status": "error", "reason": "media_file_not_found"}}

        # Moving a segment shifts a stored voiceprint, and taking one back out of the source is
        # a voiceprint write too: with the policy off for the file's owner and organization
        # neither happens (the DB reassignment itself has already committed).
        if not voiceprints_allowed(int(media_file.id), db):
            logger.info(
                f"Voiceprints are off by policy for file {media_file.id}; "
                f"skipping the embedding update for segment {segment_uuid}"
            )
            return {"skip": {"status": "skipped", "reason": "voiceprints_disabled_by_policy"}}

        from app.services.speaker_cleanup import clusters_holding_speakers

        affected_profile_ids: set[int] = set()
        if target_speaker.profile_id:
            affected_profile_ids.add(int(target_speaker.profile_id))
        affected_speaker_ids = {int(target_speaker.id)}

        source_speaker_exists = False
        source: dict | None = None
        if source_speaker_uuid:
            source_speaker = get_by_uuid(db, Speaker, source_speaker_uuid)
            if source_speaker:
                source_speaker_exists = True
                affected_speaker_ids.add(int(source_speaker.id))
                if source_speaker.profile_id:
                    affected_profile_ids.add(int(source_speaker.profile_id))
                source = {
                    "id": int(source_speaker.id),
                    "name": str(source_speaker.name),
                    "profile_id": (
                        int(source_speaker.profile_id) if source_speaker.profile_id else None
                    ),
                    "profile_uuid": (
                        str(source_speaker.profile.uuid) if source_speaker.profile else None
                    ),
                    "media_file_id": int(source_speaker.media_file_id),
                    "display_name": (
                        str(source_speaker.display_name) if source_speaker.display_name else None
                    ),
                }

        return {
            "duration": duration,
            "segment_start": float(segment.start_time),
            "segment_end": float(segment.end_time),
            "storage_path": str(media_file.storage_path),
            "content_type": str(media_file.content_type),
            "filename": str(media_file.filename),
            "target_speaker": {
                "id": int(target_speaker.id),
                "name": str(target_speaker.name),
                "profile_id": (
                    int(target_speaker.profile_id) if target_speaker.profile_id else None
                ),
                "profile_uuid": (
                    str(target_speaker.profile.uuid) if target_speaker.profile else None
                ),
                "media_file_id": int(target_speaker.media_file_id),
                "display_name": (
                    str(target_speaker.display_name) if target_speaker.display_name else None
                ),
            },
            "source_speaker_exists": source_speaker_exists,
            "source_speaker": source,
            "affected_profile_ids": affected_profile_ids,
            # The target's voiceprint shifts below, and every centroid that averages it
            # shifts with it (issue #1233); read here, while the rows are at hand.
            "affected_cluster_uuids": clusters_holding_speakers(db, affected_speaker_ids),
            "owner_id": int(target_speaker.user_id),
            # Both speakers are of the segment's file, so of its tenant.
            "organization_id": (
                int(media_file.organization_id) if media_file.organization_id is not None else None
            ),
        }


def _extract_segment_embedding(plan: dict):
    """Download the media, convert it, and extract one segment embedding.

    Runs with **no** DB session open — this is the multi-minute phase (a full
    MinIO download of the original media plus an ffmpeg transcode) that used to
    sit inside the task's transaction.
    """
    import os
    import tempfile

    from app.services.minio_service import download_file
    from app.services.speaker_embedding_service import get_cached_embedding_service
    from app.tasks.transcription.audio_processor import get_audio_file_extension
    from app.tasks.transcription.audio_processor import prepare_audio_for_transcription

    file_data, _, _ = download_file(plan["storage_path"])
    file_ext = get_audio_file_extension(plan["content_type"], plan["filename"])

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_file_path = os.path.join(temp_dir, f"input{file_ext}")
        with open(temp_file_path, "wb") as f:
            f.write(file_data.read())

        audio_file_path = prepare_audio_for_transcription(
            temp_file_path, plan["content_type"], temp_dir
        )

        # Use cached embedding service for warm model reuse
        embedding_service = get_cached_embedding_service()
        return embedding_service.extract_embedding_from_file(
            audio_file_path,
            {"start": plan["segment_start"], "end": plan["segment_end"]},
        )


def _rebuild_affected_aggregates(plan: dict) -> list[dict]:
    """Rebuild every centroid and profile embedding that averages a shifted voiceprint.

    A reassignment moves the target's voiceprint (drift, not removal): its profile's
    embedding and the centroid of every cluster holding it, or the source, still carry
    the old vector until rebuilt (issue #1233). ``speaker_cleanup`` is the one rebuild
    path, and it returns, rather than swallows, what it could not rebuild (it also logs
    it at ERROR).

    Takes plain data and opens its own short session rather than borrowing the
    caller's, so it cannot extend a transaction that spans slow work.
    """
    from app.services.speaker_cleanup import SpeakerImpact
    from app.services.speaker_cleanup import reconcile_speaker_impact

    impact = SpeakerImpact(
        owner_ids={int(plan["owner_id"])},
        cluster_uuids=set(plan["affected_cluster_uuids"]),
        profile_ids=set(plan["affected_profile_ids"]),
    )
    if impact.is_empty():
        return []
    with session_scope() as db:
        return reconcile_speaker_impact(db, impact)
