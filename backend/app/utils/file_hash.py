"""
Utility module for file duplicate detection

This module provides functions for checking whether a file with the same content
fingerprint already exists in the database.

Two columns hold a fingerprint, for two different reasons:

* ``MediaFile.imohash`` — **server-computed** from the stored object
  (``services/imohash_service.py``). Authoritative, present on every ingest path
  (manual upload, presigned completion, URL import, watch source) and backfilled
  across the whole library by ``tasks/imohash_recompute.py``.
* ``MediaFile.file_hash`` — **client-declared** fingerprint of the source the user
  selected. For a plain upload that is the file itself; for client-extracted audio
  it is the *source video*, which is the only way a re-extraction can be detected
  (ffmpeg does not produce byte-identical audio twice). Historically SHA-256;
  since issue #342 the browser sends the same imohash the server computes, because
  whole-file SHA-256 died with ``NotReadableError`` above ~4 GB and the failure was
  swallowed, silently disabling duplicate detection on the largest uploads.

Because the two columns can legitimately hold fingerprints of *different bytes*,
the pre-upload gate checks both — see :func:`check_duplicate_by_fingerprint`.
Old rows carry a SHA-256 in ``file_hash`` and a valid imohash in ``imohash``, so
matching either keeps existing libraries deduplicating across the change.

imohash is a sampling fingerprint and is NOT collision-resistant — never use these
helpers for security-sensitive equality.
"""

import logging

logger = logging.getLogger(__name__)


def _only_live_files(query):
    """Restrict a ``MediaFile`` query to rows a duplicate may legitimately point at.

    Excludes failed/cancelled/orphaned rows and PENDING rows with no
    ``storage_path`` (uploads that never completed), so a previous failure can
    never block a re-upload of the same content. Shared by every helper here so
    "a file that already exists" has exactly one definition.
    """
    from sqlalchemy import and_
    from sqlalchemy import or_

    from app.models.media import FileStatus
    from app.models.media import MediaFile

    query = query.filter(
        MediaFile.status.notin_([FileStatus.ERROR, FileStatus.CANCELLED, FileStatus.ORPHANED])
    )
    return query.filter(
        or_(
            MediaFile.status != FileStatus.PENDING,
            and_(
                MediaFile.status == FileStatus.PENDING,
                MediaFile.storage_path.isnot(None),
                MediaFile.storage_path != "",
            ),
        )
    )


def check_duplicate_by_fingerprint(
    db_session, fingerprint: str, user_id: int | None = None
) -> str | None:
    """
    Check whether a file with this content fingerprint already exists.

    Matches ``file_hash`` (client-declared) **or** ``imohash`` (server-computed);
    see the module docstring for why both are needed. Callers pass the value the
    client sent, whatever its vintage — a legacy SHA-256 matches ``file_hash``,
    a modern imohash matches either.

    Synchronous by design: the body is one blocking SQLAlchemy query. It used to be an
    ``async def`` with no ``await``, so awaiting it from the upload handlers never
    yielded and the query ran on the event loop (issue #320). Coroutine callers offload
    it with ``run_in_threadpool``.

    Args:
        db_session: SQLAlchemy database session
        fingerprint: Fingerprint of the file to check (with or without 0x prefix)
        user_id: Optional user ID to restrict the search to a specific user. The
            upload gate always passes it — without it a caller could probe another
            tenant's library by fingerprint and be handed its file UUID.

    Returns:
        The UUID of the duplicate file if found, None otherwise
    """
    from sqlalchemy import or_

    from app.models.media import MediaFile

    # Strip 0x prefix if present to maintain compatibility with database
    if fingerprint and fingerprint.startswith("0x"):
        fingerprint = fingerprint[2:]
    if not fingerprint:
        return None

    query = db_session.query(MediaFile).filter(
        or_(MediaFile.file_hash == fingerprint, MediaFile.imohash == fingerprint)
    )

    if user_id is not None:
        query = query.filter(MediaFile.user_id == user_id)

    duplicate = _only_live_files(query).first()

    if duplicate:
        return str(duplicate.uuid)  # Return UUID string for frontend

    return None


def check_duplicate_by_imohash(db_session, imohash: str, exclude_file_id: int | None = None):
    """Check whether a file with the same imohash fingerprint already exists.

    This is the server-side, cross-pipeline dedup layer (manual upload, URL
    import, prior watch-source import). It mirrors the status filtering of
    :func:`check_duplicate_by_fingerprint` so failed/incomplete uploads never block a
    re-import, but returns the full ``MediaFile`` ORM object (not just a UUID)
    so callers can link ``media_file_id`` and show the user where the content
    already lives.

    Distinct from :func:`check_duplicate_by_fingerprint`: this one reads only the
    server-computed column (never the client-declared ``file_hash``), is
    deliberately **cross-user** so a watch source can link content another account
    already imported, and returns the ORM row rather than a UUID.

    Args:
        db_session: SQLAlchemy database session.
        imohash: The imohash fingerprint to look up.
        exclude_file_id: Optional MediaFile id to exclude (e.g. the row being
            recomputed) from the match.

    Returns:
        The matching ``MediaFile`` if found, else ``None``.
    """
    from app.models.media import MediaFile

    if not imohash:
        return None

    query = db_session.query(MediaFile).filter(MediaFile.imohash == imohash)

    if exclude_file_id is not None:
        query = query.filter(MediaFile.id != exclude_file_id)

    return _only_live_files(query).first()


def cleanup_failed_duplicates(db_session, fingerprint: str, user_id: int) -> int:
    """
    Clean up any failed or incomplete files with the same content fingerprint.
    This includes:
    - ERROR, CANCELLED, ORPHANED status files
    - PENDING files that have no storage_path (incomplete uploads)

    This allows users to re-upload files that previously failed. Matches the same
    two columns as :func:`check_duplicate_by_fingerprint` so a row it would have
    reported as a duplicate is exactly a row this can clear.

    Synchronous by design: blocking SQLAlchemy plus one object-storage delete per
    orphaned row. It used to be an ``async def`` with no ``await`` (issue #320), so the
    whole loop — including every MinIO round trip — ran on the event loop. Coroutine
    callers offload it with ``run_in_threadpool``.

    Args:
        db_session: SQLAlchemy database session
        fingerprint: Fingerprint of the file to clean up
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
    if fingerprint and fingerprint.startswith("0x"):
        fingerprint = fingerprint[2:]
    if not fingerprint:
        return 0

    # Find failed files OR incomplete pending files with the same fingerprint for this user
    failed_files = (
        db_session.query(MediaFile)
        .filter(
            or_(MediaFile.file_hash == fingerprint, MediaFile.imohash == fingerprint),
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
        logger.info(
            f"Cleaned up {cleanup_count} failed duplicate files with fingerprint {fingerprint}"
        )

    return cleanup_count
