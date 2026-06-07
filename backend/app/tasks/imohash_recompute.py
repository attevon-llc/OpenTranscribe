"""One-time recompute of every ``media_file.imohash`` after the package switch.

The server-side fingerprint switched from a hand-rolled blake2b stand-in to the
real ``imohash`` package (see ``services/imohash_service.py``), which changes
**every** existing fingerprint value. The old values are now wrong and must be
discarded, so this task overwrites ``media_file.imohash`` for all rows that have
storage, using ``compute_from_minio()`` (ranged reads, ~3×sample-window bytes
per file — fast even for very large files).

It is dispatched automatically on first startup after the upgrade by
``_run_imohash_recompute()`` in ``main.py`` (gated by the
``imohash_package_recompute_complete`` system-settings flag, same pattern as the
thumbnail / embedding-normalization migrations) and is also exposed as an admin
"Recompute File Fingerprints" button. It self-reschedules batch-by-batch using a
stable id cursor and sets the completion flag only when the whole library has
been processed.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import defer

from app.core.celery import celery_app
from app.core.constants import CeleryQueues
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services.imohash_service import compute_from_minio
from app.services.migration_progress_service import MigrationProgressService
from app.services.system_settings_service import set_setting

logger = logging.getLogger(__name__)

# Separate progress namespace so it never collides with the embedding migration.
recompute_progress = MigrationProgressService(key_prefix="imohash_recompute")

# System-settings flag that marks the one-time recompute as finished.
RECOMPUTE_FLAG_KEY = "imohash_package_recompute_complete"

# Rows that can never produce a fingerprint are skipped.
_SKIP_STATUSES = (FileStatus.ERROR, FileStatus.CANCELLED, FileStatus.ORPHANED)


def _count_eligible(db) -> int:
    """Count rows that have storage and are not in a terminal-failed state."""
    return int(
        db.query(MediaFile.id)
        .filter(
            MediaFile.storage_path.isnot(None),
            MediaFile.storage_path != "",
            MediaFile.status.notin_(_SKIP_STATUSES),
        )
        .count()
    )


@celery_app.task(
    name="imohash_recompute.recompute_all", bind=True, priority=CPUPriority.ADMIN_BATCH
)
def recompute_all(self, batch_size: int = 100, after_id: int = 0) -> dict:
    """Recompute ``media_file.imohash`` for all eligible rows, batch by batch.

    Args:
        batch_size: Number of rows to recompute per batch (default 100).
        after_id: Stable id cursor — only rows with ``id > after_id`` are
            processed. Used for self-rescheduling; callers leave it at 0.

    Returns:
        Per-batch statistics dict.
    """
    summary = {
        "files_found": 0,
        "files_recomputed": 0,
        "files_skipped": 0,
        "files_failed": 0,
        "has_more": False,
        "last_id": after_id,
    }

    try:
        # On the first batch, initialise progress tracking with the total count.
        if after_id == 0:
            with session_scope() as db:
                total = _count_eligible(db)
            recompute_progress.start_migration(total_files=total)
            logger.info("imohash recompute starting: %d eligible files", total)

        with session_scope() as db:
            rows = (
                db.query(MediaFile)
                .options(
                    defer(MediaFile.summary_data),
                    defer(MediaFile.metadata_raw),
                    defer(MediaFile.waveform_data),
                )
                .filter(
                    MediaFile.id > after_id,
                    MediaFile.storage_path.isnot(None),
                    MediaFile.storage_path != "",
                    MediaFile.status.notin_(_SKIP_STATUSES),
                )
                .order_by(MediaFile.id.asc())
                .limit(batch_size + 1)  # one extra to detect more
                .all()
            )

            summary["has_more"] = len(rows) > batch_size
            batch = rows[:batch_size]
            summary["files_found"] = len(batch)

            for media_file in batch:
                summary["last_id"] = media_file.id
                try:
                    fingerprint = compute_from_minio(
                        str(media_file.storage_path), size=media_file.file_size
                    )
                    if fingerprint:
                        media_file.imohash = fingerprint
                        summary["files_recomputed"] += 1
                        recompute_progress.increment_processed(success=True)
                    else:
                        summary["files_skipped"] += 1
                        recompute_progress.increment_processed(
                            success=False, file_uuid=str(media_file.uuid)
                        )
                except Exception as e:  # noqa: BLE001 - one bad file must not abort the batch
                    logger.warning("imohash recompute failed for file %s: %s", media_file.id, e)
                    summary["files_failed"] += 1
                    recompute_progress.increment_processed(
                        success=False, file_uuid=str(media_file.uuid)
                    )
            db.commit()

        if summary["has_more"]:
            logger.info(
                "imohash recompute batch done (through id=%s): %d recomputed, "
                "%d skipped, %d failed — scheduling next batch",
                summary["last_id"],
                summary["files_recomputed"],
                summary["files_skipped"],
                summary["files_failed"],
            )
            recompute_all.apply_async(
                kwargs={"batch_size": batch_size, "after_id": summary["last_id"]},
                queue=CeleryQueues.UTILITY,
            )
        else:
            _finalize_recompute()

    except Exception as e:
        logger.error("imohash recompute task error: %s", e)
        summary["error"] = str(e)  # type: ignore[assignment]

    return summary


def _finalize_recompute() -> None:
    """Mark the recompute complete and set the one-time system-settings flag."""
    recompute_progress.complete_migration(success=True)
    with session_scope() as db:
        set_setting(
            db,
            RECOMPUTE_FLAG_KEY,
            "true",
            "One-time imohash package recompute of all media_file.imohash completed",
        )
    logger.info("imohash recompute complete — flag set, will not run again")
