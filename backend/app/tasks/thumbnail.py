"""
Celery task for generating thumbnails from video files after upload completion.

Runs asynchronously in the background to avoid blocking the upload response.
"""

import io
import logging
from datetime import timedelta

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.services.minio_service import MinIOService
from app.services.minio_service import upload_file
from app.utils.thumbnail import generate_thumbnail_from_url

logger = logging.getLogger(__name__)


@celery_app.task(name="generate_thumbnail", bind=True, priority=CPUPriority.PIPELINE_CRITICAL)
def generate_thumbnail_task(self, file_id: int, user_id: int, storage_path: str) -> dict:
    """
    Generate and upload a WebP thumbnail for a video file.

    This task runs asynchronously after upload completion to avoid blocking
    the upload response. It generates a thumbnail from the first second of the
    video and stores it in MinIO.

    Args:
        file_id: MediaFile ID
        user_id: User ID (for logging/context)
        storage_path: Path to the uploaded video in MinIO

    Returns:
        Dictionary with generation status
    """
    result: dict[str, object] = {"file_id": file_id, "success": False, "thumbnail_path": None}

    try:
        from app.core.config import settings

        logger.info(f"Starting thumbnail generation for file {file_id}")

        # Get presigned URL for video access (includes auth credentials)
        try:
            # MinIO client is configured with MINIO_HOST (default: "minio" in Docker).
            # The presigned URL will use that hostname, which is correct for
            # container-to-container access (FFmpeg runs in the same container).
            presigned_url = MinIOService().client.presigned_get_object(
                bucket_name=settings.MEDIA_BUCKET_NAME,
                object_name=storage_path,
                expires=timedelta(seconds=300),  # 5 minutes
            )

            logger.debug(
                f"Generated presigned URL for thumbnail generation (host: {settings.MINIO_HOST})"
            )

        except Exception as e:
            logger.error(f"Failed to get presigned URL for file {file_id}: {e}")
            result["error"] = f"Presigned URL generation failed: {str(e)}"
            return result

        # Generate thumbnail using ffmpeg with range request support
        try:
            thumbnail_bytes = generate_thumbnail_from_url(
                presigned_url=presigned_url,
                timestamp=1.0,
            )

            if not thumbnail_bytes:
                logger.warning(f"Thumbnail generation returned no data for file {file_id}")
                result["error"] = "Thumbnail generation returned no data"
                return result

            # Upload thumbnail to storage
            thumbnail_storage_path = f"user_{user_id}/file_{file_id}/thumbnail.webp"

            upload_file(
                file_content=io.BytesIO(thumbnail_bytes),
                file_size=len(thumbnail_bytes),
                object_name=thumbnail_storage_path,
                content_type="image/webp",
            )

            logger.info(f"Thumbnail uploaded for file {file_id}: {len(thumbnail_bytes)} bytes")

            # Update MediaFile with thumbnail path
            with session_scope() as db:
                media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
                if media_file:
                    media_file.thumbnail_path = thumbnail_storage_path  # type: ignore[assignment]
                    db.commit()
                    logger.info(f"Updated MediaFile {file_id} with thumbnail path")
                else:
                    logger.warning(f"MediaFile {file_id} not found for thumbnail update")

            # Push the thumbnail to the gallery live: a file_updated event with a
            # presigned thumbnail_url so the card swaps the placeholder for the
            # real thumbnail mid-processing (no refresh). Matches the frontend
            # file_updated handler in routes/+page.svelte.
            try:
                from app.services.minio_service import get_file_url
                from app.services.notification_service import send_task_notification

                thumbnail_url = get_file_url(
                    thumbnail_storage_path, expires=settings.THUMBNAIL_URL_EXPIRE_SECONDS
                )
                send_task_notification(
                    user_id,
                    "file_updated",
                    file_id=file_id,
                    extra={"thumbnail_url": thumbnail_url},
                )
            except Exception as notify_err:
                logger.warning(
                    f"Thumbnail notify failed for file {file_id} (non-fatal): {notify_err}"
                )

            result["success"] = True
            result["thumbnail_path"] = thumbnail_storage_path
            logger.info(f"Successfully generated thumbnail for file {file_id}")

        except Exception as e:
            logger.error(f"Thumbnail generation failed for file {file_id}: {e}", exc_info=True)
            result["error"] = f"Thumbnail generation failed: {str(e)}"

    except Exception as e:
        logger.error(f"Unexpected error in thumbnail task for file {file_id}: {e}", exc_info=True)
        result["error"] = f"Task failed: {str(e)}"

    return result
