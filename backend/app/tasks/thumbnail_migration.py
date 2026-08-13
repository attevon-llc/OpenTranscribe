"""
Celery task for migrating existing JPEG thumbnails to optimized WebP format.

This task runs on backend startup and migrates thumbnails for direct uploads only
(source_url is NULL). YouTube/URL downloads are skipped as they already have
optimized thumbnails from the source platform.

**Session-lifetime note** (``app/tasks/CLAUDE.md``): the batch runs in three
phases — read the candidate rows, re-render and re-upload each thumbnail with
**no** session open, then write the new paths back. One session used to wrap the
whole batch, so up to ``batch_size`` presigned reads and MinIO round trips ran
inside a single Postgres transaction holding ``ACCESS SHARE`` on ``media_file``.
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
        # Phase 1 — read (DB session open, Postgres only).
        candidates, has_more = _load_migration_batch(batch_size)
        summary["files_found"] = len(candidates)
        summary["has_more"] = has_more

        # Phase 2 — presign + ffmpeg-free thumbnail render + MinIO upload/delete.
        # NO DB session is held here: a WebP re-render streams the source media
        # over a presigned URL, which is unbounded on a large file.
        migrated: dict[int, str] = {}
        for row in candidates:
            try:
                result, new_path = _migrate_single_thumbnail(row)
                if result == "migrated" and new_path:
                    migrated[row["id"]] = new_path
                    summary["files_migrated"] += 1
                elif result == "skipped":
                    summary["files_skipped"] += 1
                else:
                    summary["files_failed"] += 1
            except Exception as e:
                logger.error(f"Error migrating thumbnail for file {row['id']}: {e}")
                summary["files_failed"] += 1

        # Phase 3 — write (DB session reopened, Postgres only).
        _store_thumbnail_paths(migrated)

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


def _load_migration_batch(batch_size: int) -> tuple[list[dict], bool]:
    """Read one batch of JPEG-thumbnail files, then release the DB session.

    Returns **plain data only** — no ORM instances — so the caller can run the
    presign/render/upload phase with no transaction open. An escaping instance
    would lazy-load during that phase and silently reopen one.

    Only files that (1) have a ``.jpg`` thumbnail, (2) were uploaded directly
    (``source_url IS NULL``) and (3) finished processing are selected.

    Args:
        batch_size: Maximum number of files in the returned batch.

    Returns:
        ``(batch, has_more)`` where each item is
        ``{"id", "thumbnail_path", "storage_path"}``.
    """
    with session_scope() as db:
        rows = (
            db.query(MediaFile.id, MediaFile.thumbnail_path, MediaFile.storage_path)
            .filter(
                MediaFile.thumbnail_path.like("%.jpg"),
                MediaFile.source_url.is_(None),  # Skip YouTube/URL downloads
                MediaFile.status == FileStatus.COMPLETED,
            )
            .limit(batch_size + 1)  # Get one extra to check if there's more
            .all()
        )

    has_more = len(rows) > batch_size
    batch = [
        {
            "id": int(row[0]),
            "thumbnail_path": str(row[1]) if row[1] else "",
            "storage_path": str(row[2]) if row[2] else "",
        }
        for row in rows[:batch_size]
    ]
    return batch, has_more


def _store_thumbnail_paths(new_paths: dict[int, str]) -> None:
    """Point the migrated rows at their new WebP objects in one short session.

    Args:
        new_paths: ``{media_file_id: new_thumbnail_path}``.
    """
    if not new_paths:
        return
    with session_scope() as db:
        for file_id, new_path in new_paths.items():
            db.query(MediaFile).filter(MediaFile.id == file_id).update(
                {"thumbnail_path": new_path}, synchronize_session=False
            )
        db.commit()


def _migrate_single_thumbnail(row: dict) -> tuple[str, str | None]:
    """
    Migrate a single thumbnail from JPEG to WebP.

    Takes plain data and touches no database session: the presigned read, the
    WebP render and the two MinIO calls all run outside any transaction. The
    new path is handed back for the caller's short write phase rather than
    assigned to an ORM instance here.

    Args:
        row: ``{"id", "thumbnail_path", "storage_path"}`` from
            :func:`_load_migration_batch`.

    Returns:
        ``(outcome, new_thumbnail_path)`` where outcome is "migrated",
        "skipped" or "failed"; the path is None unless the outcome is
        "migrated".
    """
    file_id = row["id"]
    old_thumbnail_path = row["thumbnail_path"]

    # Double-check it's a JPEG thumbnail
    if not old_thumbnail_path.endswith(".jpg"):
        return "skipped", None

    # Get the video storage path to generate thumbnail from
    video_path = row["storage_path"]
    if not video_path:
        logger.warning(f"File {file_id} has no storage path, skipping thumbnail migration")
        return "skipped", None

    try:
        # Get presigned URL for the video (valid for 5 minutes)
        presigned_url = get_file_url(video_path, expires=300)

        # Generate new WebP thumbnail from the video URL
        thumbnail_bytes = generate_thumbnail_from_url(presigned_url)

        if not thumbnail_bytes:
            logger.error(f"Failed to generate WebP thumbnail for file {file_id}")
            return "failed", None

        # Create new thumbnail path with .webp extension
        new_thumbnail_path = old_thumbnail_path.rsplit(".", 1)[0] + ".webp"

        # Upload new WebP thumbnail
        upload_file(
            file_content=io.BytesIO(thumbnail_bytes),
            file_size=len(thumbnail_bytes),
            object_name=new_thumbnail_path,
            content_type="image/webp",
        )

        # Delete old JPEG thumbnail
        try:
            delete_file(old_thumbnail_path)
        except Exception as e:
            # Log but don't fail - the migration succeeded even if cleanup failed
            logger.warning(f"Failed to delete old thumbnail {old_thumbnail_path}: {e}")

        logger.info(
            f"Migrated thumbnail for file {file_id}: {old_thumbnail_path} -> {new_thumbnail_path}"
        )
        return "migrated", new_thumbnail_path

    except Exception as e:
        logger.error(f"Error migrating thumbnail for file {file_id}: {e}")
        return "failed", None
