import logging

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.services.analytics_service import AnalyticsService
from app.utils.websocket_notify import send_ws_event_for_file

# Setup logging
logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True, name="analytics.analyze_transcript", priority=CPUPriority.USER_TRIGGERED
)
def analyze_transcript_task(self, file_uuid: str):
    """
    Analyze a transcript to extract comprehensive analytics:
    - Speaker talk time and statistics
    - Turn-taking analysis
    - Interruption detection
    - Question analysis
    - Overall metrics

    Args:
        file_uuid: UUID of the MediaFile to analyze
    """
    from app.utils.uuid_helpers import get_file_by_uuid

    task_id = self.request.id
    file_id = None

    with session_scope() as db:
        try:
            # Get media file from database
            media_file = get_file_by_uuid(db, file_uuid)
            if not media_file:
                raise ValueError(f"Media file with UUID {file_uuid} not found")

            file_id = int(media_file.id)  # Get internal ID for database operations

            # Create task record
            from app.utils.task_utils import create_task_record
            from app.utils.task_utils import update_task_status

            create_task_record(db, task_id, int(media_file.user_id), file_id, "analytics")

            # Update task status
            update_task_status(db, task_id, "in_progress", progress=0.1)

            # Compute comprehensive analytics using the new service
            success = AnalyticsService.compute_and_save_analytics(db, file_id)

            if not success:
                raise ValueError(f"Failed to compute analytics for file {file_id}")

            # Update task progress - analytics computation complete
            update_task_status(db, task_id, "in_progress", progress=0.6)

            # Analytics computation complete - update final progress

            # Update task as completed
            update_task_status(db, task_id, "completed", progress=1.0, completed=True)

            logger.info(
                f"Successfully analyzed file {media_file.filename} with comprehensive analytics"
            )

            # Notify enrichment tracker
            send_ws_event_for_file(
                int(media_file.user_id),
                "enrichment_task_complete",
                {"file_id": file_uuid, "task": "analytics"},
                file_id=file_id,
            )

            return {"status": "success", "file_id": file_id}

        except Exception as e:
            # Handle errors
            logger.error(f"Error analyzing file {file_id}: {str(e)}")
            from app.utils.task_utils import update_task_status

            update_task_status(db, task_id, "failed", error_message=str(e), completed=True)
            return {"status": "error", "message": str(e)}
