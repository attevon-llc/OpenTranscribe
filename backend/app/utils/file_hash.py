"""
Utility module for file duplicate detection

This module provides functions for checking if files with the same hash
already exist in the database.

The primary hash calculation happens client-side (frontend) for efficiency.
"""

import logging

logger = logging.getLogger(__name__)

# Hashing is now done client-side in the frontend


def check_duplicate_by_hash(db_session, file_hash: str, user_id: int | None = None) -> str | None:
    """
    Check if a file with the same hash exists in the database.
    Only considers files that are not in failed states and have actually been uploaded.

    Synchronous by design: the body is one blocking SQLAlchemy query. It used to be an
    ``async def`` with no ``await``, so awaiting it from the upload handlers never
    yielded and the query ran on the event loop (issue #320). Coroutine callers offload
    it with ``run_in_threadpool``.

    Args:
        db_session: SQLAlchemy database session
        file_hash: Hash of the file to check (with or without 0x prefix)
        user_id: Optional user ID to restrict the search to a specific user

    Returns:
        The UUID of the duplicate file if found, None otherwise
    """
    from sqlalchemy import and_
    from sqlalchemy import or_

    from app.models.media import FileStatus
    from app.models.media import MediaFile

    # Strip 0x prefix if present to maintain compatibility with database
    if file_hash and file_hash.startswith("0x"):
        file_hash = file_hash[2:]

    query = db_session.query(MediaFile).filter(MediaFile.file_hash == file_hash)

    if user_id is not None:
        query = query.filter(MediaFile.user_id == user_id)

    # Only consider files that:
    # 1. Are not in failed/error states
    # 2. Have actually been uploaded (have a storage_path) OR are actively processing
    # This prevents failed/incomplete uploads from blocking re-uploads of the same file
    query = query.filter(
        MediaFile.status.notin_([FileStatus.ERROR, FileStatus.CANCELLED, FileStatus.ORPHANED])
    )

    # Also exclude PENDING files that have no storage_path (incomplete uploads)
    query = query.filter(
        or_(
            MediaFile.status != FileStatus.PENDING,
            and_(
                MediaFile.status == FileStatus.PENDING,
                MediaFile.storage_path.isnot(None),
                MediaFile.storage_path != "",
            ),
        )
    )

    duplicate = query.first()

    if duplicate:
        return str(duplicate.uuid)  # Return UUID string for frontend

    return None


def check_duplicate_by_imohash(db_session, imohash: str, exclude_file_id: int | None = None):
    """Check whether a file with the same imohash fingerprint already exists.

    This is the server-side, cross-pipeline dedup layer (manual upload, URL
    import, prior watch-source import). It mirrors the status filtering of
    :func:`check_duplicate_by_hash` so failed/incomplete uploads never block a
    re-import, but returns the full ``MediaFile`` ORM object (not just a UUID)
    so callers can link ``media_file_id`` and show the user where the content
    already lives.

    Args:
        db_session: SQLAlchemy database session.
        imohash: The imohash fingerprint to look up.
        exclude_file_id: Optional MediaFile id to exclude (e.g. the row being
            recomputed) from the match.

    Returns:
        The matching ``MediaFile`` if found, else ``None``.
    """
    from sqlalchemy import and_
    from sqlalchemy import or_

    from app.models.media import FileStatus
    from app.models.media import MediaFile

    if not imohash:
        return None

    query = db_session.query(MediaFile).filter(MediaFile.imohash == imohash)

    if exclude_file_id is not None:
        query = query.filter(MediaFile.id != exclude_file_id)

    # Same filtering as check_duplicate_by_hash: ignore failed/cancelled/orphaned
    # rows and incomplete PENDING uploads (no storage_path) so they never block
    # a fresh import of the same content.
    query = query.filter(
        MediaFile.status.notin_([FileStatus.ERROR, FileStatus.CANCELLED, FileStatus.ORPHANED])
    )
    query = query.filter(
        or_(
            MediaFile.status != FileStatus.PENDING,
            and_(
                MediaFile.status == FileStatus.PENDING,
                MediaFile.storage_path.isnot(None),
                MediaFile.storage_path != "",
            ),
        )
    )

    return query.first()


def cleanup_failed_duplicates(db_session, file_hash: str, user_id: int) -> int:
    """
    Clean up any failed or incomplete files with the same hash.
    This includes:
    - ERROR, CANCELLED, ORPHANED status files
    - PENDING files that have no storage_path (incomplete uploads)

    This allows users to re-upload files that previously failed.

    Synchronous by design: blocking SQLAlchemy plus one object-storage delete per
    orphaned row. It used to be an ``async def`` with no ``await`` (issue #320), so the
    whole loop — including every MinIO round trip — ran on the event loop. Coroutine
    callers offload it with ``run_in_threadpool``.

    Args:
        db_session: SQLAlchemy database session
        file_hash: Hash of the file to clean up
        user_id: User ID to restrict cleanup to specific user

    Returns:
        Number of files cleaned up
    """
    from sqlalchemy import and_
    from sqlalchemy import or_

    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.services.minio_service import delete_file

    # Strip 0x prefix if present
    if file_hash and file_hash.startswith("0x"):
        file_hash = file_hash[2:]

    # Find failed files OR incomplete pending files with the same hash for this user
    failed_files = (
        db_session.query(MediaFile)
        .filter(
            MediaFile.file_hash == file_hash,
            MediaFile.user_id == user_id,
            or_(
                # Failed status files
                MediaFile.status.in_([FileStatus.ERROR, FileStatus.CANCELLED, FileStatus.ORPHANED]),
                # Incomplete PENDING files (no storage_path means upload never completed)
                and_(
                    MediaFile.status == FileStatus.PENDING,
                    or_(
                        MediaFile.storage_path.is_(None),
                        MediaFile.storage_path == "",
                    ),
                ),
            ),
        )
        .all()
    )

    cleanup_count = 0
    for file in failed_files:
        try:
            # Delete from storage if exists
            if file.storage_path:
                try:
                    delete_file(file.storage_path)
                    logger.info(f"Cleaned up failed file storage: {file.storage_path}")
                except Exception as e:
                    logger.warning(f"Could not delete storage for failed file {file.id}: {e}")

            # Delete from database (cascade will handle related records)
            db_session.delete(file)
            cleanup_count += 1
            logger.info(f"Cleaned up failed duplicate file {file.id} ({file.filename})")

        except Exception as e:
            logger.error(f"Error cleaning up failed file {file.id}: {e}")
            # Continue with other files even if one fails

    if cleanup_count > 0:
        db_session.commit()
        logger.info(f"Cleaned up {cleanup_count} failed duplicate files with hash {file_hash}")

    return cleanup_count
