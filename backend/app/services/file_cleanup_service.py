"""
File cleanup service for recovering stuck files and maintaining system health.
"""

import contextlib
import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.utils.task_utils import check_for_stuck_files
from app.utils.task_utils import recover_stuck_file

logger = logging.getLogger(__name__)


class FileCleanupService:
    """Service for automated file cleanup and recovery operations."""

    def __init__(self):
        self.stuck_threshold_hours = 2
        self.orphan_threshold_hours = 12
        self.max_recovery_attempts = 3

    def run_cleanup_cycle(self) -> dict[str, Any]:
        """
        Run a complete cleanup cycle.

        Returns:
            Dictionary with cleanup results and statistics
        """
        results: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "stuck_files_checked": 0,
            "files_recovered": 0,
            "files_marked_orphaned": 0,
            "cleanup_errors": [],
            "recommendations": [],
        }

        try:
            with session_scope() as db:
                # Step 1: Check for stuck files
                stuck_file_ids = check_for_stuck_files(db, self.stuck_threshold_hours)
                results["stuck_files_checked"] = len(stuck_file_ids)

                if stuck_file_ids:
                    logger.info(f"Found {len(stuck_file_ids)} stuck files for cleanup")

                    # Step 2: Attempt recovery
                    for file_id in stuck_file_ids:
                        try:
                            success = self._attempt_file_recovery(db, file_id)
                            if success:
                                results["files_recovered"] += 1
                            else:
                                results["files_marked_orphaned"] += 1
                        except Exception as e:
                            error_msg = f"Error processing stuck file {file_id}: {str(e)}"
                            logger.error(error_msg)
                            results["cleanup_errors"].append(error_msg)

                # Step 3: Handle very old orphaned files
                old_orphaned_count = self._handle_old_orphaned_files(db)
                if old_orphaned_count > 0:
                    results["recommendations"].append(
                        f"Found {old_orphaned_count} old orphaned files that may need admin attention"
                    )

                # Step 4: Generate health recommendations
                health_recommendations = self._generate_health_recommendations(db)
                results["recommendations"].extend(health_recommendations)

        except Exception as e:
            error_msg = f"Critical error in cleanup cycle: {str(e)}"
            logger.error(error_msg)
            results["cleanup_errors"].append(error_msg)

        logger.info(f"Cleanup cycle completed: {results}")
        return results

    def _attempt_file_recovery(self, db: Session, file_id: int) -> bool:
        """
        Attempt to recover a single stuck file.

        Args:
            db: Database session
            file_id: ID of the file to recover

        Returns:
            True if recovery was successful, False if marked as orphaned
        """
        media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
        if not media_file:
            return False

        # Check if we've already tried recovery too many times
        if (media_file.recovery_attempts or 0) >= self.max_recovery_attempts:
            logger.warning(
                f"File {file_id} has exceeded max recovery attempts ({self.max_recovery_attempts})"
            )
            # Mark as permanently orphaned
            media_file.status = FileStatus.ORPHANED  # type: ignore[assignment]
            media_file.force_delete_eligible = True  # type: ignore[assignment]
            db.commit()
            return False

        # Attempt recovery
        return recover_stuck_file(db, file_id)

    def _handle_old_orphaned_files(self, db: Session) -> int:
        """
        Handle files that have been orphaned for a long time.

        Args:
            db: Database session

        Returns:
            Number of old orphaned files found
        """
        threshold_time = datetime.now(UTC) - timedelta(hours=self.orphan_threshold_hours)

        old_orphaned_files = (
            db.query(MediaFile)
            .filter(
                MediaFile.status == FileStatus.ORPHANED,
                MediaFile.last_recovery_attempt < threshold_time,
            )
            .all()
        )

        for file in old_orphaned_files:
            # Mark as eligible for force deletion
            file.force_delete_eligible = True  # type: ignore[assignment]
            logger.warning(
                f"File {file.id} has been orphaned for over {self.orphan_threshold_hours} hours"
            )

        if old_orphaned_files:
            db.commit()

        return len(old_orphaned_files)

    def _generate_health_recommendations(self, db: Session) -> list[str]:
        """
        Generate system health recommendations based on file states.

        Args:
            db: Database session

        Returns:
            List of recommendation strings
        """
        recommendations = []

        # Count files by status in a single query (replaces N separate queries)
        from sqlalchemy import func

        status_rows = (
            db.query(MediaFile.status, func.count().label("cnt")).group_by(MediaFile.status).all()
        )
        status_counts = {
            r.status.value if hasattr(r.status, "value") else r.status: r.cnt for r in status_rows
        }

        # Check for concerning patterns
        error_rate = status_counts.get("error", 0) / max(sum(status_counts.values()), 1)
        if error_rate > 0.1:  # More than 10% error rate
            recommendations.append(
                f"High error rate detected: {error_rate:.1%} of files are in error state. "
                "Consider investigating processing pipeline health."
            )

        orphaned_count = status_counts.get("orphaned", 0)
        if orphaned_count > 0:
            recommendations.append(
                f"Found {orphaned_count} orphaned file(s). "
                "Consider manual review or cleanup of these files."
            )

        processing_count = status_counts.get("processing", 0)
        if processing_count > 50:  # Arbitrary threshold
            recommendations.append(
                f"Large number of files currently processing ({processing_count}). "
                "Monitor worker capacity and queue health."
            )

        return recommendations

    def force_cleanup_orphaned_files(self, db: Session, dry_run: bool = False) -> dict[str, Any]:
        """
        Force cleanup of orphaned files (admin operation).

        Args:
            db: Database session
            dry_run: If True, only preview what would be cleaned up

        Returns:
            Dictionary with cleanup results
        """
        results: dict[str, Any] = {
            "dry_run": dry_run,
            "eligible_for_deletion": 0,
            "successfully_deleted": 0,
            "deletion_errors": [],
            "files_processed": [],
        }

        # Find files eligible for force deletion
        eligible_files = (
            db.query(MediaFile)
            .filter(
                MediaFile.force_delete_eligible.is_(True),
                MediaFile.status.in_([FileStatus.ORPHANED, FileStatus.ERROR]),
            )
            .all()
        )

        results["eligible_for_deletion"] = len(eligible_files)

        if not dry_run:
            from app.api.endpoints.files.crud import delete_media_file
            from app.models.user import User

            # Create a system user for cleanup operations
            system_user = User(role="admin", email="system@cleanup")

            for file in eligible_files:
                try:
                    # Force delete the file
                    delete_media_file(db, str(file.id), system_user, force=True)
                    results["successfully_deleted"] += 1
                    results["files_processed"].append(
                        {"id": int(file.id), "filename": file.filename, "status": "deleted"}
                    )
                except Exception as e:
                    error_msg = f"Failed to delete file {file.id}: {str(e)}"
                    results["deletion_errors"].append(error_msg)
                    results["files_processed"].append(
                        {
                            "id": int(file.id),
                            "filename": file.filename,
                            "status": "error",
                            "error": str(e),
                        }
                    )
        else:
            # Dry run - just record what would be deleted
            for file in eligible_files:
                results["files_processed"].append(
                    {
                        "id": int(file.id),
                        "filename": file.filename,
                        "status": "would_delete",
                        "current_status": file.status,
                    }
                )

        return results

    def get_cleanup_statistics(self, db: Session) -> dict[str, Any]:
        """
        Get current cleanup statistics and system health metrics.

        Args:
            db: Database session

        Returns:
            Dictionary with statistics
        """
        stats: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "file_counts_by_status": {},
            "stuck_files_detected": 0,
            "files_eligible_for_cleanup": 0,
            "avg_processing_time_hours": 0,
            "health_score": "unknown",
        }

        # Count files by status in a single query (replaces N separate queries)
        from sqlalchemy import extract
        from sqlalchemy import func

        status_rows = (
            db.query(MediaFile.status, func.count().label("cnt")).group_by(MediaFile.status).all()
        )
        for r in status_rows:
            key = r.status.value if hasattr(r.status, "value") else r.status
            stats["file_counts_by_status"][key] = r.cnt

        # Count stuck files
        stuck_files = check_for_stuck_files(db, self.stuck_threshold_hours)
        stats["stuck_files_detected"] = len(stuck_files)

        # Count files eligible for cleanup
        eligible_count = (
            db.query(MediaFile).filter(MediaFile.force_delete_eligible.is_(True)).count()
        )
        stats["files_eligible_for_cleanup"] = eligible_count

        # Calculate average processing time via SQL aggregation (avoids full ORM hydration)
        avg_row = (
            db.query(
                func.avg(
                    extract("epoch", MediaFile.completed_at - MediaFile.task_started_at) / 3600.0
                ).label("avg_hours")
            )
            .filter(
                MediaFile.status == FileStatus.COMPLETED,
                MediaFile.task_started_at.isnot(None),
                MediaFile.completed_at.isnot(None),
            )
            .one()
        )
        if avg_row.avg_hours is not None:
            stats["avg_processing_time_hours"] = float(avg_row.avg_hours)

        # Calculate health score
        total_files = sum(stats["file_counts_by_status"].values())
        if total_files > 0:
            error_rate = stats["file_counts_by_status"].get("error", 0) / total_files
            orphaned_rate = stats["file_counts_by_status"].get("orphaned", 0) / total_files

            if error_rate < 0.05 and orphaned_rate < 0.02:
                stats["health_score"] = "healthy"
            elif error_rate < 0.1 and orphaned_rate < 0.05:
                stats["health_score"] = "fair"
            else:
                stats["health_score"] = "poor"
        else:
            stats["health_score"] = "empty"

        return stats


# Global service instance
cleanup_service = FileCleanupService()


def _cleanup_opensearch_for_file(file: "MediaFile", file_uuid: str) -> None:
    """Remove all OpenSearch documents associated with a media file.

    Covers: speakers (v3), speakers_v4, transcripts, transcript_chunks,
    transcript_summaries. Each step is non-fatal.
    """
    # Speaker embeddings (v3)
    with contextlib.suppress(Exception):
        from app.services.opensearch_service import remove_speaker_embedding

        for speaker in list(file.speakers):
            remove_speaker_embedding(str(speaker.uuid))

    # Speaker embeddings (v4)
    with contextlib.suppress(Exception):
        from app.core.constants import get_speaker_index_v4
        from app.services.opensearch_service import opensearch_client

        if opensearch_client:
            v4_index = get_speaker_index_v4()
            if opensearch_client.indices.exists(index=v4_index):
                for speaker in list(file.speakers):
                    with contextlib.suppress(Exception):
                        opensearch_client.delete(index=v4_index, id=str(speaker.uuid))

    # Transcript document
    with contextlib.suppress(Exception):
        from app.services.opensearch_service import opensearch_client
        from app.services.opensearch_service import settings as os_settings

        if opensearch_client:
            opensearch_client.delete(index=os_settings.OPENSEARCH_TRANSCRIPT_INDEX, id=file_uuid)

    # Transcript chunks
    with contextlib.suppress(Exception):
        from app.services.search.indexing_service import TranscriptIndexingService

        TranscriptIndexingService().delete_transcript_chunks(file_uuid)

    # Transcript summaries
    with contextlib.suppress(Exception):
        from app.services.opensearch_service import opensearch_client
        from app.services.opensearch_service import settings as os_settings

        if opensearch_client:
            summary_index = os_settings.OPENSEARCH_SUMMARY_INDEX
            if opensearch_client.indices.exists(index=summary_index):
                opensearch_client.delete_by_query(
                    index=summary_index,
                    body={"query": {"term": {"file_id": int(file.id)}}},
                    refresh=True,
                )


def delete_file_storage_artifacts(db: Session, file: MediaFile) -> bool:
    """Delete every object-storage artifact for a media file.

    Covers the original, its thumbnail, and the regenerable derived cache
    (subtitle-embedded videos + extracted audio under ``processed-videos/derived/``).
    Single source of truth shared by the interactive delete endpoint and the
    retention/auto-delete path so neither can orphan storage. Best-effort per
    artifact — a missing object never blocks the rest.

    Returns:
        True if the original (``storage_path``) object was deleted.
    """
    from app.services.minio_service import delete_file

    original_deleted = False
    for path_attr in ("storage_path", "thumbnail_path"):
        path = getattr(file, path_attr, None)
        if not path:
            continue
        try:
            delete_file(str(path))
            if path_attr == "storage_path":
                original_deleted = True
            logger.info(f"Deleted MinIO object {path}")
        except Exception as minio_err:
            logger.warning(f"Could not delete MinIO object {path} (non-fatal): {minio_err}")

    # Regenerable derived cache — duplicates that must not outlive the original.
    try:
        from app.services.minio_service import MinIOService
        from app.services.video_processing_service import VideoProcessingService

        VideoProcessingService(MinIOService()).clear_cache_for_media_file(db, int(file.id))
    except Exception as cache_err:
        logger.debug(f"Derived-cache cleanup failed (non-fatal): {cache_err}")

    return original_deleted


def _cleanup_empty_clusters(db: Session, owner_id: int) -> None:
    """Remove non-promoted speaker clusters left empty after a file's speakers are deleted.

    CASCADE removes cluster-membership rows but not the (now empty) cluster itself, so
    we check actual remaining members via a NOT EXISTS subquery. Best-effort.
    """
    try:
        from sqlalchemy import exists

        from app.models.media import SpeakerCluster
        from app.models.media import SpeakerClusterMember

        has_members = (
            exists()
            .where(SpeakerClusterMember.cluster_id == SpeakerCluster.id)
            .correlate(SpeakerCluster)
        )
        empty_clusters = (
            db.query(SpeakerCluster)
            .filter(
                SpeakerCluster.user_id == owner_id,
                ~has_members,
                SpeakerCluster.promoted_to_profile_id.is_(None),
            )
            .all()
        )
        if not empty_clusters:
            return
        for cluster in empty_clusters:
            with contextlib.suppress(Exception):
                from app.services.opensearch_service import delete_cluster_embedding

                delete_cluster_embedding(str(cluster.uuid))
            db.delete(cluster)
        db.commit()
        logger.info(f"Cleaned up {len(empty_clusters)} empty cluster(s) after file deletion")
    except Exception as e:
        db.rollback()
        logger.warning(f"Failed to clean up empty clusters: {e}")


def purge_media_file(db: Session, file: MediaFile) -> dict:
    """Canonical destroy for a MediaFile and ALL associated data.

    Single source of truth shared by every delete path — interactive single/force
    delete, bulk delete, N-day retention, and orphan cleanup — so a change here applies
    everywhere and no path can drift or leak. Steps (each best-effort, DB delete is the
    commit point):

    1. Object storage: original + thumbnail + regenerable derived cache.
    2. OpenSearch: speaker embeddings (v3+v4), transcript doc, transcript chunks, summaries.
    3. Database row (CASCADE removes child rows).
    4. Redis caches for the owner.
    5. Empty non-promoted speaker clusters orphaned by the CASCADE.

    SpeakerProfile records and their profile embeddings are intentionally preserved.

    Returns ``{"deleted": bool, "file_uuid": str, "error": str | None}``. Never raises.
    """
    file_uuid = str(file.uuid)
    try:
        owner_id = int(file.user_id)

        delete_file_storage_artifacts(db, file)
        _cleanup_opensearch_for_file(file, file_uuid)

        db.delete(file)
        db.commit()
        logger.info(f"purge_media_file: deleted file {file_uuid} from database")

        try:
            from app.services.redis_cache_service import redis_cache

            redis_cache.invalidate_all_for_user(owner_id)
        except Exception as cache_err:
            logger.debug(f"purge_media_file: cache invalidation failed (non-critical): {cache_err}")

        _cleanup_empty_clusters(db, owner_id)

        return {"deleted": True, "file_uuid": file_uuid, "error": None}

    except Exception as e:
        db.rollback()
        logger.error(f"purge_media_file: failed to delete file {file_uuid}: {e}")
        return {"deleted": False, "file_uuid": file_uuid, "error": str(e)}


def auto_delete_media_file(db: Session, file: MediaFile) -> dict:
    """Backwards-compatible alias for the canonical :func:`purge_media_file`."""
    return purge_media_file(db, file)
