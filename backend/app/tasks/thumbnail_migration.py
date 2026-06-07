"""
Celery task for migrating existing JPEG thumbnails to optimized WebP format.

This task runs on backend startup and migrates thumbnails for direct uploads only
(source_url is NULL). YouTube/URL downloads are skipped as they already have
optimized thumbnails from the source platform.
"""

import io
import logging

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services.minio_service import delete_file
from app.services.minio_service import get_file_url
from app.services.minio_service import upload_file
from app.utils.thumbnail import generate_thumbnail_from_url

logger = logging.getLogger(__name__)


@celery_app.task(name="migrate_thumbnails_to_webp", bind=True, priority=CPUPriority.ADMIN_BATCH)
def migrate_thumbnails_to_webp(self, batch_size: int = 20) -> dict:
    """
    Migrate existing JPEG thumbnails to optimized WebP format.

    Only migrates direct uploads (source_url is NULL).
    Skips YouTube/URL downloads which already have optimized thumbnails.
    Uses presigned URLs for efficient streaming - no full video download needed.

    Args:
        batch_size: Number of files to process per batch (default: 20)

    Returns:
        Dictionary with migration statistics
    """
    summary = {
        "files_found": 0,
        "files_migrated": 0,
        "files_skipped": 0,
        "files_failed": 0,
        "has_more": False,
    }

    try:
        with session_scope() as db:
            # Only migrate files that:
            # 1. Have JPEG thumbnails (.jpg extension)
            # 2. Were directly uploaded (source_url is NULL)
            # 3. Have completed processing
            from sqlalchemy.orm import defer

            files_to_migrate = (
                db.query(MediaFile)
                .options(
                    defer(MediaFile.summary_data),
                    defer(MediaFile.metadata_raw),
                    defer(MediaFile.waveform_data),
                )
                .filter(
                    MediaFile.thumbnail_path.like("%.jpg"),
                    MediaFile.source_url.is_(None),  # Skip YouTube/URL downloads
                    MediaFile.status == FileStatus.COMPLETED,
                )
                .limit(batch_size + 1)  # Get one extra to check if there's more
                .all()
            )

            summary["files_found"] = min(len(files_to_migrate), batch_size)
            summary["has_more"] = len(files_to_migrate) > batch_size

            # Process only batch_size files
            for media_file in files_to_migrate[:batch_size]:
                try:
                    result = _migrate_single_thumbnail(db, media_file)
                    if result == "migrated":
                        summary["files_migrated"] += 1
                    elif result == "skipped":
                        summary["files_skipped"] += 1
                    else:
                        summary["files_failed"] += 1
                except Exception as e:
                    logger.error(f"Error migrating thumbnail for file {media_file.id}: {e}")
                    summary["files_failed"] += 1

            db.commit()

        # If there are more files, schedule another batch
        if summary["has_more"]:
            logger.info(
                f"Thumbnail migration batch completed: {summary['files_migrated']} migrated, "
                f"{summary['files_skipped']} skipped, {summary['files_failed']} failed. "
                "Scheduling next batch..."
            )
            migrate_thumbnails_to_webp.delay(batch_size=batch_size)
        else:
            logger.info(
                f"Thumbnail migration completed: {summary['files_migrated']} migrated, "
                f"{summary['files_skipped']} skipped, {summary['files_failed']} failed."
            )

    except Exception as e:
        logger.error(f"Error in thumbnail migration task: {e}")
        summary["error"] = str(e)  # type: ignore[assignment]

    return summary


def _migrate_single_thumbnail(db, media_file: MediaFile) -> str:
    """
    Migrate a single thumbnail from JPEG to WebP.

    Args:
        db: Database session
        media_file: MediaFile object to migrate

    Returns:
        "migrated" if successful, "skipped" if not needed, "failed" if error
    """
    old_thumbnail_path = str(media_file.thumbnail_path)

    # Double-check it's a JPEG thumbnail
    if not old_thumbnail_path.endswith(".jpg"):
        return "skipped"

    # Get the video storage path to generate thumbnail from
    video_path = str(media_file.storage_path)
    if not video_path:
        logger.warning(f"File {media_file.id} has no storage path, skipping thumbnail migration")
        return "skipped"

    try:
        # Get presigned URL for the video (valid for 5 minutes)
        presigned_url = get_file_url(video_path, expires=300)

        # Generate new WebP thumbnail from the video URL
        thumbnail_bytes = generate_thumbnail_from_url(presigned_url)

        if not thumbnail_bytes:
            logger.error(f"Failed to generate WebP thumbnail for file {media_file.id}")
            return "failed"

        # Create new thumbnail path with .webp extension
        new_thumbnail_path = old_thumbnail_path.rsplit(".", 1)[0] + ".webp"

        # Upload new WebP thumbnail
        upload_file(
            file_content=io.BytesIO(thumbnail_bytes),
            file_size=len(thumbnail_bytes),
            object_name=new_thumbnail_path,
            content_type="image/webp",
        )

        # Update database with new path
        media_file.thumbnail_path = new_thumbnail_path  # type: ignore[assignment]

        # Delete old JPEG thumbnail
        try:
            delete_file(old_thumbnail_path)
        except Exception as e:
            # Log but don't fail - the migration succeeded even if cleanup failed
            logger.warning(f"Failed to delete old thumbnail {old_thumbnail_path}: {e}")

        logger.info(
            f"Migrated thumbnail for file {media_file.id}: "
            f"{old_thumbnail_path} -> {new_thumbnail_path}"
        )
        return "migrated"

    except Exception as e:
        logger.error(f"Error migrating thumbnail for file {media_file.id}: {e}")
        return "failed"
