"""API endpoints for speaker clustering and global speaker management."""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.media import Speaker
from app.models.media import SpeakerCluster
from app.models.user import User
from app.schemas.speaker_cluster import BatchVerifyRequest
from app.schemas.speaker_cluster import ClusterPromoteRequest
from app.schemas.speaker_cluster import ClusterSplitRequest
from app.schemas.speaker_cluster import ClusterUnassignRequest
from app.schemas.speaker_cluster import ReclusterRequest
from app.schemas.speaker_cluster import SpeakerClusterUpdate
from app.services.speaker_clustering_service import SpeakerClusteringService

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Fixed-path routes MUST be defined before wildcard /{cluster_uuid} routes.
# FastAPI matches routes in declaration order, so a wildcard defined first
# would swallow paths like /recluster, /stats, /unverified/inbox, etc.
# ---------------------------------------------------------------------------


@router.get("", response_model=dict[str, Any])
def list_clusters(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    search: str | None = Query(None),
    has_label: bool | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List speaker clusters with pagination and filtering."""
    try:
        service = SpeakerClusteringService(db)
        return service.list_clusters(
            user_id=current_user.id,
            page=page,
            per_page=per_page,
            has_label=has_label,
            search=search,
        )
    except Exception as e:
        logger.error("Error listing clusters: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.post("/recluster", response_model=dict[str, Any])
def trigger_recluster(
    data: ReclusterRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Trigger full re-clustering of all speakers."""
    try:
        from app.tasks.speaker_clustering import recluster_all_speakers

        threshold = data.threshold if data and data.threshold is not None else None
        user_id = current_user.id
        task = recluster_all_speakers.delay(user_id, threshold)

        # Send immediate "queued" notification so the UI shows status while
        # the task waits for the GPU worker to pick it up.
        try:
            from app.tasks.speaker_clustering import _send_clustering_progress

            _send_clustering_progress(
                user_id,
                step=0,
                total_steps=0,
                message="Queued — waiting for GPU...",
                progress=0.0,
                running=True,
            )
        except Exception as e:
            logger.debug("Initial progress notification failed (non-critical): %s", e)

        return {
            "status": "started",
            "task_id": task.id,
            "message": "Re-clustering started in background",
        }
    except Exception as e:
        logger.error("Error triggering recluster: %s", e)
        raise HTTPException(status_code=500, detail="Failed to start re-clustering") from e


@router.get("/unverified/inbox", response_model=dict[str, Any])
def get_unverified_inbox(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get paginated list of unverified speakers for the inbox."""
    service = SpeakerClusteringService(db)
    return service.get_unverified_speakers(
        user_id=current_user.id,
        page=page,
        per_page=per_page,
    )


@router.post("/batch-verify", response_model=dict[str, Any])
def batch_verify(
    data: BatchVerifyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Batch verify multiple speakers."""
    service = SpeakerClusteringService(db)
    return service.batch_verify_speakers(
        speaker_uuids=[str(u) for u in data.speaker_uuids],
        user_id=current_user.id,
        action=data.action,
        profile_uuid=str(data.profile_uuid) if data.profile_uuid else None,
        display_name=data.display_name,
    )


@router.get("/speakers/{speaker_uuid}/media-preview")
def get_speaker_media_preview(
    speaker_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get presigned URL and segment timestamps for speaker media preview.

    Returns the source media file URL and the longest transcript segment
    for this speaker so the player can seek directly to their voice.
    """
    speaker = (
        db.query(Speaker)
        .filter(Speaker.uuid == speaker_uuid, Speaker.user_id == current_user.id)
        .first()
    )
    if not speaker:
        raise HTTPException(status_code=404, detail="Speaker not found")

    media_file = speaker.media_file
    if not media_file:
        raise HTTPException(status_code=404, detail="Media file not found")

    # Presigned URL for source media (1 hour TTL)
    from app.services.minio_service import get_file_url

    try:
        media_presigned_url = get_file_url(media_file.storage_path, expires=3600)
    except Exception as e:
        logger.warning("Failed to generate presigned URL for %s: %s", media_file.storage_path, e)
        media_presigned_url = None

    # Longest transcript segment for this speaker
    from app.models.media import TranscriptSegment

    best_seg = (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.speaker_id == speaker.id)
        .order_by((TranscriptSegment.end_time - TranscriptSegment.start_time).desc())
        .first()
    )
    seg_start = float(best_seg.start_time) if best_seg else 0.0
    seg_end = float(best_seg.end_time) if best_seg else 0.0

    return {
        "speaker_uuid": str(speaker.uuid),
        "speaker_name": speaker.display_name or speaker.name,
        "file_uuid": str(media_file.uuid),
        "file_name": media_file.filename,
        "content_type": media_file.content_type or "audio/unknown",
        "start_time": seg_start,
        "end_time": seg_end,
        "media_url": media_presigned_url,
    }


@router.get("/stats")
def get_clustering_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get aggregate speaker clustering statistics."""
    user_id = current_user.id
    total_speakers = db.query(Speaker).filter(Speaker.user_id == user_id).count()
    clustered = (
        db.query(Speaker)
        .filter(
            Speaker.user_id == user_id,
            Speaker.cluster_id.isnot(None),
        )
        .count()
    )
    total_clusters = (
        db.query(SpeakerCluster)
        .filter(
            SpeakerCluster.user_id == user_id,
        )
        .count()
    )

    return {
        "total_speakers": total_speakers,
        "clustered_speakers": clustered,
        "total_clusters": total_clusters,
        "coverage_pct": round(clustered / total_speakers * 100, 1) if total_speakers else 0,
    }


@router.post("/{cluster_uuid}/analyze-outliers")
async def analyze_outliers(
    cluster_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Analyze minority-gender speakers for potential outliers."""
    service = SpeakerClusteringService(db)
    try:
        result = service.analyze_gender_outliers(cluster_uuid, current_user.id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{cluster_uuid}/unassign")
async def unassign_speakers(
    cluster_uuid: str,
    request: ClusterUnassignRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Unassign speakers from a cluster with optional blacklisting."""
    service = SpeakerClusteringService(db)
    try:
        result = service.unassign_speakers(
            cluster_uuid,
            [str(u) for u in request.speaker_uuids],
            current_user.id,
            request.blacklist,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ---------------------------------------------------------------------------
# Wildcard routes -- these MUST come after all fixed-path routes above.
# ---------------------------------------------------------------------------


@router.get("/{cluster_uuid}", response_model=dict[str, Any])
def get_cluster_detail(
    cluster_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get cluster detail with all members."""
    service = SpeakerClusteringService(db)
    result = service.get_cluster_detail(cluster_uuid, current_user.id)
    if not result:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return result


@router.put("/{cluster_uuid}", response_model=dict[str, Any])
def update_cluster(
    cluster_uuid: str,
    data: SpeakerClusterUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Update cluster label and description."""
    try:
        cluster = (
            db.query(SpeakerCluster)
            .filter(
                SpeakerCluster.uuid == cluster_uuid,
                SpeakerCluster.user_id == current_user.id,
            )
            .first()
        )
        if not cluster:
            raise HTTPException(status_code=404, detail="Cluster not found")

        if data.label is not None:
            cluster.label = data.label if data.label else None  # type: ignore[assignment]
        if data.description is not None:
            cluster.description = data.description  # type: ignore[assignment]

        db.commit()
        db.refresh(cluster)

        return {
            "uuid": str(cluster.uuid),
            "label": cluster.label,
            "description": cluster.description,
            "member_count": int(cluster.member_count or 0),
            "updated_at": cluster.updated_at,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error updating cluster: %s", e)
        db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.delete("/{cluster_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_cluster(
    cluster_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Delete a cluster."""
    try:
        cluster = (
            db.query(SpeakerCluster)
            .filter(
                SpeakerCluster.uuid == cluster_uuid,
                SpeakerCluster.user_id == current_user.id,
            )
            .first()
        )
        if not cluster:
            raise HTTPException(status_code=404, detail="Cluster not found")

        # Clear speaker cluster_id references
        db.query(Speaker).filter(Speaker.cluster_id == cluster.id).update({"cluster_id": None})

        # Remove from OpenSearch
        try:
            from app.services.opensearch_service import delete_cluster_embedding

            delete_cluster_embedding(str(cluster.uuid))
        except Exception:
            logger.debug("Failed to remove cluster embedding from OpenSearch")

        db.delete(cluster)
        db.commit()

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error deleting cluster: %s", e)
        db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.post("/{cluster_uuid}/promote", response_model=dict[str, Any])
def promote_cluster(
    cluster_uuid: str,
    data: ClusterPromoteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Promote a cluster to a speaker profile."""
    service = SpeakerClusteringService(db)
    profile = service.promote_cluster_to_profile(
        cluster_uuid=cluster_uuid,
        name=data.name,
        user_id=current_user.id,
        description=data.description,
    )
    if not profile:
        raise HTTPException(status_code=400, detail="Failed to promote cluster")

    return {
        "profile_uuid": str(profile.uuid),
        "profile_name": profile.name,
        "message": f"Cluster promoted to profile '{profile.name}'",
    }


@router.post("/{source_uuid}/merge/{target_uuid}", response_model=dict[str, Any])
def merge_clusters(
    source_uuid: str,
    target_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Merge source cluster into target cluster."""
    if source_uuid == target_uuid:
        raise HTTPException(status_code=400, detail="Cannot merge cluster with itself")

    service = SpeakerClusteringService(db)
    result = service.merge_clusters(source_uuid, target_uuid, current_user.id)
    if not result:
        raise HTTPException(status_code=400, detail="Failed to merge clusters")

    return {
        "uuid": str(result.uuid),
        "member_count": int(result.member_count or 0),
        "message": "Clusters merged successfully",
    }


@router.post("/{cluster_uuid}/split", response_model=dict[str, Any])
def split_cluster(
    cluster_uuid: str,
    data: ClusterSplitRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Split speakers from a cluster into a new cluster."""
    service = SpeakerClusteringService(db)
    new_cluster = service.split_cluster(
        cluster_uuid=cluster_uuid,
        speaker_uuids=[str(u) for u in data.speaker_uuids],
        user_id=current_user.id,
    )
    if not new_cluster:
        raise HTTPException(status_code=400, detail="Failed to split cluster")

    return {
        "uuid": str(new_cluster.uuid),
        "member_count": int(new_cluster.member_count or 0),
        "message": "Cluster split successfully",
    }
