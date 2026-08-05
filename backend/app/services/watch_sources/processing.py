"""Core watch-source import pipeline.

``import_single_file`` takes a discovered ``RemoteFileInfo`` and runs it through
the same ingest path a manual upload uses: validate → fingerprint → 3-layer
dedup → MinIO upload → ``MediaFile`` row (owned by the source's user) →
collections/tags → transcription pipeline (thumbnail + waveform + transcribe via
the shared ``dispatch_upload_pipeline`` tail). Idempotent on
``(watch_source_id, remote_path)``; originals on remote sources are never
deleted, local originals only when ``delete_after_import`` is set.
"""

from __future__ import annotations

import contextlib
import logging
import mimetypes
import os
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.watch_source import WatchSourceFile
from app.services.imohash_service import compute_from_path
from app.services.watch_sources.base import RemoteFileInfo
from app.utils.file_hash import check_duplicate_by_imohash
from app.utils.file_validation import validate_uploaded_file
from app.utils.filename import get_safe_storage_filename
from app.utils.filename import sanitize_filename

if TYPE_CHECKING:
    from app.models.watch_source import WatchSource
    from app.services.watch_sources.base import BaseWatchSourceClient

logger = logging.getLogger(__name__)

# Terminal tracking statuses — a file in one of these is not re-imported.
_TERMINAL_STATUSES = {
    "imported",
    "skipped_duplicate",
    "skipped_old",
    "skipped_invalid",
    "stitched_part",
}

# Fallback MIME types for media extensions that mimetypes may not know.
_MEDIA_MIME_FALLBACK = {
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".opus": "audio/opus",
    ".oga": "audio/ogg",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".m4v": "video/mp4",
    ".ts": "video/mp2t",
    ".wav": "audio/wav",
}


def guess_media_mime(filename: str) -> str | None:
    """Best-effort audio/video MIME from a filename; None if not media."""
    guessed, _ = mimetypes.guess_type(filename)
    if guessed and guessed.startswith(("audio/", "video/")):
        return guessed
    return _MEDIA_MIME_FALLBACK.get(Path(filename).suffix.lower())


def _get_or_create_tracking_row(
    db: Session, source: WatchSource, file_info: RemoteFileInfo
) -> WatchSourceFile | None:
    """Return the tracking row for this path, or None if already terminal.

    Creates a new ``importing`` row when unseen; reuses an existing non-terminal
    row (error/pending) for retry; returns None when the path is already in a
    terminal state (so the caller skips it).
    """
    existing: WatchSourceFile | None = (
        db.query(WatchSourceFile)
        .filter(
            WatchSourceFile.watch_source_id == source.id,
            WatchSourceFile.remote_path == file_info.path,
        )
        .first()
    )
    if existing:
        if existing.status in _TERMINAL_STATUSES:
            return None
        return existing

    row = WatchSourceFile(
        uuid=uuid_pkg.uuid4(),
        watch_source_id=source.id,
        remote_path=file_info.path,
        filename=file_info.name,
        file_size=file_info.size,
        file_modified_at=file_info.modified_time,
        status="importing",
    )
    db.add(row)
    db.flush()
    return row


def _mark_skipped(db: Session, row: WatchSourceFile, reason: str, media_file_id: int | None = None):
    row.status = "skipped_duplicate" if reason.startswith("duplicate") else f"skipped_{reason}"
    # Normalize: too_old → skipped_old, invalid → skipped_invalid
    if reason == "too_old":
        row.status = "skipped_old"
    elif reason in ("invalid_type", "validation_failed"):
        row.status = "skipped_invalid"
    row.skip_reason = reason
    if media_file_id:
        row.media_file_id = media_file_id
    row.processed_at = datetime.now(UTC)
    db.flush()


def ingest_prepared_file(
    db: Session,
    source: WatchSource,
    local_path: str,
    *,
    filename: str,
    row: WatchSourceFile,
    size: int | None = None,
) -> WatchSourceFile:
    """Run a materialized local file through validate → dedup → MinIO → MediaFile.

    Shared by both the single-file importer (after download) and the multi-part
    stitcher (after concat). Commits on skip/success; raises on hard failure so
    the caller can roll back and record the error. ``row`` is the tracking row
    to finalize (status ``importing``/``downloading``).
    """
    owner_id = int(source.user_id)
    file_size = int(size) if size is not None else os.path.getsize(local_path)

    # 1. Magic-byte validation.
    declared_mime = guess_media_mime(filename)
    if not declared_mime:
        _mark_skipped(db, row, "invalid_type")
        db.commit()
        return row
    with open(local_path, "rb") as fp:
        is_valid, detected = validate_uploaded_file(fp, declared_mime, filename)
    if not is_valid:
        logger.info("Watch import rejected %s: %s", filename, detected)
        _mark_skipped(db, row, "validation_failed")
        db.commit()
        return row
    content_type = detected

    # 2. Fingerprint.
    imohash = compute_from_path(local_path)
    row.imohash = imohash

    # 3. Three-layer dedup (other-source + cross-pipeline).
    if imohash:
        other = (
            db.query(WatchSourceFile)
            .filter(
                WatchSourceFile.imohash == imohash,
                WatchSourceFile.watch_source_id != source.id,
                WatchSourceFile.status == "imported",
            )
            .first()
        )
        if other:
            _mark_skipped(db, row, "duplicate_other_source", other.media_file_id)
            db.commit()
            return row
        existing_media = check_duplicate_by_imohash(db, imohash)
        if existing_media:
            _mark_skipped(db, row, "duplicate_existing", existing_media.id)
            db.commit()
            return row

    # 4. Create the MediaFile row (owned by the source user). Tenant scope was
    # captured on the source at CREATION time from the creating request's
    # context (v372 backfilled pre-existing rows) — imports never guess the
    # org from the owner's memberships (issue #262c). NULL = personal, always
    # NULL in the community edition.
    organization_id = int(source.organization_id) if source.organization_id else None
    db_file = MediaFile(
        filename=sanitize_filename(filename),
        title=Path(filename).stem,
        user_id=owner_id,
        organization_id=organization_id,
        storage_path="",
        file_size=file_size,
        content_type=content_type,
        status=FileStatus.PENDING,
        is_public=False,
        imohash=imohash,
        file_hash=None,
    )
    db.add(db_file)
    db.flush()
    db.refresh(db_file)

    # 5. Upload to MinIO under the standard path.
    storage_path = get_safe_storage_filename(filename, owner_id, int(db_file.id))
    from app.services.minio_service import upload_file_tuned

    with open(local_path, "rb") as fp:
        upload_file_tuned(
            file_content=fp,
            file_size=file_size,
            object_name=storage_path,
            content_type=content_type,
        )
    db_file.storage_path = storage_path

    # 6. Collections + tags (owned by / shared with the source user).
    from app.api.endpoints.files.prepare_upload import add_file_to_collections

    if source.collection_ids:
        add_file_to_collections(db, int(db_file.id), owner_id, source.collection_ids)
    if source.tag_names:
        _add_tags(db, int(db_file.id), source.tag_names, owner_id)

    # 7. Finalize tracking row.
    row.status = "imported"
    row.media_file_id = int(db_file.id)
    row.processed_at = datetime.now(UTC)
    source.total_files_imported = (source.total_files_imported or 0) + 1

    db.commit()
    db.refresh(db_file)

    # 8. Post-commit: notify gallery + dispatch pipeline.
    _notify_file_created(owner_id, db_file)
    if source.auto_transcribe:
        _dispatch_pipeline(db_file, owner_id, source)

    return row


def import_single_file(
    db: Session,
    source: WatchSource,
    file_info: RemoteFileInfo,
    client: BaseWatchSourceClient,
) -> WatchSourceFile | None:
    """Import one discovered file. Returns the tracking row (None if skipped early).

    Materializes the bytes locally (no-op for local sources), then delegates to
    :func:`ingest_prepared_file`. Idempotent on ``(source, remote_path)``.
    """
    # Path-level dedup (within source).
    row = _get_or_create_tracking_row(db, source, file_info)
    if row is None:
        return None

    temp_path: str | None = None

    try:
        # Materialize the bytes locally (no-op for local sources).
        if source.source_type == "local":
            local_path = file_info.path
        else:
            temp_dir = settings.watch_temp_dir
            temp_dir.mkdir(parents=True, exist_ok=True)
            ext = Path(file_info.name).suffix
            temp_path = str(temp_dir / f"watch_{source.id}_{uuid_pkg.uuid4().hex}{ext}")
            row.status = "downloading"
            db.flush()
            written = client.download_file(file_info.path, temp_path)
            if written <= 0 or not os.path.exists(temp_path):
                raise RuntimeError("download produced no bytes")
            local_path = temp_path

        result = ingest_prepared_file(
            db, source, local_path, filename=file_info.name, row=row, size=file_info.size
        )

        # Delete local original if configured (remote sources never deleted).
        if (
            result.status == "imported"
            and source.source_type == "local"
            and source.delete_after_import
        ):
            try:
                client.delete_file(file_info.path)  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                logger.warning("delete_after_import failed for %s: %s", file_info.path, e)

        return result

    except Exception as e:  # noqa: BLE001 - one bad file must not abort the whole scan
        logger.error("Watch import failed for %s: %s", file_info.path, e, exc_info=True)
        db.rollback()
        _record_error(source.id, file_info.path, str(e))
        return None
    finally:
        if temp_path and os.path.exists(temp_path):
            with contextlib.suppress(OSError):
                os.remove(temp_path)


def _add_tags(db: Session, file_id: int, tag_names: list[str], user_id: int) -> None:
    """Apply a watch source's tags to an imported file, owned by ``user_id``."""
    from app.api.endpoints.files.prepare_upload import add_tags_to_file

    add_tags_to_file(db, file_id, tag_names, user_id)


def _notify_file_created(user_id: int, media_file: MediaFile) -> None:
    """Send the live gallery ``file_created`` WS event (best-effort)."""
    try:
        from app.services.formatting_service import FormattingService
        from app.utils.websocket_notify import send_ws_event

        file_data = {
            "id": str(media_file.uuid),
            "filename": str(media_file.filename),
            "status": media_file.status.value if media_file.status else "processing",
            "display_status": FormattingService.format_status(media_file.status)
            if media_file.status
            else "Processing",
            "content_type": str(media_file.content_type),
            "file_size": int(media_file.file_size) if media_file.file_size else 0,
            "title": str(media_file.title) if media_file.title else None,
            "upload_time": media_file.upload_time.isoformat() if media_file.upload_time else None,
        }
        send_ws_event(user_id, "file_created", {"file_id": str(media_file.uuid), "file": file_data})
    except Exception as e:  # noqa: BLE001
        logger.warning("file_created WS notify failed for %s: %s", media_file.id, e)


def _dispatch_pipeline(media_file: MediaFile, user_id: int, source: WatchSource) -> None:
    """Fire the shared upload tail (thumbnail + transcription pipeline)."""
    try:
        from app.api.endpoints.files.upload import dispatch_upload_pipeline

        dispatch_upload_pipeline(
            media_file,
            user_id=user_id,
            whisper_model=None,
            min_speakers=source.min_speakers,
            max_speakers=source.max_speakers,
            num_speakers=None,
            task_id=None,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Pipeline dispatch failed for watch import %s: %s", media_file.id, e)


def _record_error(source_id: int, remote_path: str, message: str) -> None:
    """Persist an error status on the tracking row in a fresh transaction."""
    from app.db.session_utils import session_scope

    try:
        with session_scope() as db:
            row = (
                db.query(WatchSourceFile)
                .filter(
                    WatchSourceFile.watch_source_id == source_id,
                    WatchSourceFile.remote_path == remote_path,
                )
                .first()
            )
            if row:
                row.status = "error"
                row.error_message = message[:2000]
                row.retry_count = (row.retry_count or 0) + 1
                row.processed_at = datetime.now(UTC)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to record watch import error for %s: %s", remote_path, e)
