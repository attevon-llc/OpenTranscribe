"""Celery tasks for speaker clustering and audio clip extraction."""

import contextlib
import logging

from app.core.celery import celery_app
from app.core.constants import NOTIFICATION_TYPE_CLUSTERING_COMPLETE
from app.core.constants import NOTIFICATION_TYPE_CLUSTERING_FILE_COMPLETE
from app.core.constants import NOTIFICATION_TYPE_CLUSTERING_PROGRESS
from app.core.constants import CPUPriority
from app.core.constants import GPUPriority
from app.db.session_utils import session_scope
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)


def _send_clustering_progress(
    user_id: int,
    step: int,
    total_steps: int,
    message: str,
    progress: float,
    running: bool = True,
    eta_seconds: float | None = None,
):
    """Send clustering progress via WebSocket notification."""
    data: dict = {
        "step": step,
        "total_steps": total_steps,
        "message": message,
        "progress": progress,
        "running": running,
    }
    if eta_seconds is not None:
        data["eta_seconds"] = eta_seconds
    send_ws_event(user_id, NOTIFICATION_TYPE_CLUSTERING_PROGRESS, data)


def _send_clustering_complete(user_id: int, result: dict):
    """Send clustering complete notification via WebSocket."""
    send_ws_event(user_id, NOTIFICATION_TYPE_CLUSTERING_COMPLETE, result)


def _send_clustering_file_complete(user_id: int, file_uuid: str, clusters_assigned: int):
    """Send per-file clustering complete notification via WebSocket."""
    send_ws_event(
        user_id,
        NOTIFICATION_TYPE_CLUSTERING_FILE_COMPLETE,
        {
            "file_uuid": file_uuid,
            "clusters_assigned": clusters_assigned,
        },
    )


def _send_clustering_error(user_id: int, error_message: str):
    """Send clustering error notification via WebSocket."""
    send_ws_event(
        user_id,
        NOTIFICATION_TYPE_CLUSTERING_COMPLETE,
        {
            "status": "error",
            "error": error_message,
            "running": False,
        },
    )


@celery_app.task(
    name="speaker.cluster_for_file",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    priority=CPUPriority.PIPELINE_CRITICAL,
    acks_late=True,
    reject_on_worker_lost=True,
)
def cluster_speakers_for_file(self, file_uuid: str, user_id: int):
    """Cluster speakers in a media file after transcription.

    Called as part of the post-transcription pipeline.

    Args:
        file_uuid: UUID of the media file.
        user_id: Owner user ID.
    """
    task_id = self.request.id

    with session_scope() as db:
        try:
            from app.models.media import MediaFile
            from app.services.speaker_clustering_service import SpeakerClusteringService
            from app.utils.task_utils import create_task_record
            from app.utils.task_utils import update_task_status

            media_file = db.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()
            if not media_file:
                logger.warning(f"Media file {file_uuid} not found for clustering")
                return {"status": "skipped", "reason": "file_not_found"}

            # Tracked via active_task_id so `is_file_safe_to_delete` (task_utils.py)
            # can see this task is live and refuse a concurrent delete — clustering
            # writes to the same speaker-embedding OpenSearch plane a file delete
            # cleans up, so an unguarded delete mid-cluster can leave orphaned
            # embeddings the same way an untracked search_indexing run left orphaned
            # transcript chunks.
            create_task_record(db, task_id, user_id, int(media_file.id), "speaker_clustering")
            update_task_status(db, task_id, "in_progress", progress=0.1)

            service = SpeakerClusteringService(db)
            clusters = service.cluster_speakers_for_file(int(media_file.id), user_id)

            result = {
                "status": "completed",
                "file_uuid": file_uuid,
                "clusters_assigned": len(clusters),
            }
            logger.info(
                f"Clustered speakers for file {file_uuid}: {len(clusters)} cluster assignments"
            )

            update_task_status(db, task_id, "completed", progress=1.0, completed=True)

            _send_clustering_file_complete(user_id, file_uuid, len(clusters))

            # Notify enrichment tracker that clustering is done
            send_ws_event(
                user_id,
                "enrichment_task_complete",
                {"file_id": file_uuid, "task": "speaker_clustering"},
            )

            return result

        except Exception as e:
            logger.error(f"Error clustering speakers for file {file_uuid}: {e}")
            try:
                from app.utils.task_utils import update_task_status

                update_task_status(db, task_id, "failed", error_message=str(e), completed=True)
            except Exception as status_err:  # noqa: BLE001
                logger.debug(f"Could not record clustering failure status: {status_err}")
            raise self.retry(exc=e) from e


@celery_app.task(
    name="speaker.recluster_all",
    bind=True,
    max_retries=1,
    default_retry_delay=60,
    priority=GPUPriority.USER_RECLUSTER,
    acks_late=True,
    reject_on_worker_lost=True,
)
def recluster_all_speakers(self, user_id: int, threshold: float | None = None):
    """Full re-clustering of all speakers for a user.

    Triggered manually from the UI. Uses a per-user Redis lock to prevent
    concurrent re-clustering runs.

    Args:
        user_id: Owner user ID.
        threshold: Optional clustering threshold override.
    """
    from app.utils.task_lock import task_lock_manager

    lock_key = f"recluster_speakers_user_{user_id}"

    with task_lock_manager.acquire_lock(lock_key, timeout=600) as acquired:
        if not acquired:
            logger.info("Recluster lock held for user %d — retrying in 60s", user_id)
            raise self.retry(countdown=60, max_retries=30)

        with session_scope() as db:
            try:
                from app.services.speaker_clustering_service import SpeakerClusteringService

                service = SpeakerClusteringService(db)

                # Initialize progress tracker for ETA
                from app.services.progress_tracker import ProgressTracker

                clustering_tracker = ProgressTracker(
                    task_type="clustering", user_id=user_id, total=1
                )
                clustering_tracker.start(message="Starting speaker clustering...")

                def progress_cb(step: int, total: int, message: str, progress: float):
                    # Update tracker total on first call (maps step/total_steps)
                    if clustering_tracker.total != total:
                        clustering_tracker.total = total
                    state = clustering_tracker.update(step, message=message)
                    eta_seconds = state.eta_seconds if state else None
                    _send_clustering_progress(
                        user_id, step, total, message, progress, eta_seconds=eta_seconds
                    )

                kwargs: dict = {"progress_callback": progress_cb}
                if threshold is not None:
                    kwargs["threshold"] = threshold

                result = service.batch_recluster(user_id, **kwargs)

                clustering_tracker.complete(message="Clustering complete")

                logger.info(
                    "Re-clustering complete for user %d: status=%s, "
                    "clusters_created=%s, speakers_assigned=%s, singletons=%s",
                    user_id,
                    result.get("status"),
                    result.get("clusters_created"),
                    result.get("speakers_assigned"),
                    result.get("singletons"),
                )

                _send_clustering_complete(user_id, result)

                return result

            except Exception as e:
                logger.error(f"Error re-clustering speakers for user {user_id}: {e}")
                with contextlib.suppress(Exception):
                    clustering_tracker.fail(message=f"Clustering failed: {e}")
                if self.request.retries >= self.max_retries:
                    _send_clustering_error(user_id, str(e))
                    raise
                raise self.retry(exc=e) from e
            finally:
                # Free intermediate CUDA tensors for follow-on tasks
                from app.tasks.migration_pipeline import cleanup_gpu_memory

                cleanup_gpu_memory()
