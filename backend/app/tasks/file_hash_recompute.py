"""One-off backfill of ``document.file_hash`` for rows written before it existed.

``Document.file_hash`` (the server-side imohash fingerprint) is now written at
document creation — both the manual-upload endpoint
(``api/endpoints/documents.py``, via ``imohash_service.compute_from_stream``
on the not-yet-uploaded spooled file) and watch-source ingest
(``services/watch_sources/document_ingest.py``, via ``compute_from_path`` on
the local temp file) — but every ``document`` row created before that landed
still holds ``file_hash IS NULL``. This task fills exactly those rows, using
``imohash_service.compute_from_minio`` (ranged reads, ~3xsample-window bytes
per document — fast even for a large PDF).

**Not a per-row migration, deliberately.** Populating this column needs a real
read of each document's stored bytes via MinIO, and a schema migration that
reaches out to object storage risks failing (or hanging) the startup
migration runner on a MinIO outage — which ``SystemExit(1)``-aborts the whole
backend rather than serving a half-migrated schema (``app/db/CLAUDE.md``).
``alembic/versions/v397_backfill_document_tenancy_and_hash.py`` names this
exact deferral in its own docstring, following the identical precedent this
module mirrors: ``app/tasks/imohash_recompute.py``, written for the same
class of backfill on ``media_file.imohash``. The two differ in one respect —
imohash_recompute invalidates and recomputes EVERY row (the fingerprint
*algorithm* changed under already-populated values), while this task only
fills the gap (``file_hash IS NULL``); a row already carrying a fingerprint
was written correctly by the current code path and needs no second pass.

Exposed as ``file_hash_recompute.backfill_document_file_hashes``; dispatching
it (on startup, from an admin action, or as a one-off invocation) is left to
whichever caller owns that decision — this module only defines the task and
its completion bookkeeping, mirroring ``imohash_recompute.py``'s shape so a
future caller can wire it up the same way that module's docstring describes.
"""

from __future__ import annotations

import logging

from app.core.celery import celery_app
from app.core.constants import CeleryQueues
from app.core.constants import CPUPriority
from app.core.enums import FileStatus
from app.db.session_utils import session_scope
from app.models.document import Document
from app.services.imohash_service import compute_from_minio
from app.services.migration_progress_service import MigrationProgressService
from app.services.system_settings_service import set_setting

logger = logging.getLogger(__name__)

# Separate progress namespace so it never collides with the media-file imohash
# recompute or any other one-time migration.
backfill_progress = MigrationProgressService(key_prefix="document_file_hash_backfill")

# System-settings flag that marks the one-time backfill as finished.
BACKFILL_FLAG_KEY = "document_file_hash_backfill_complete"

# Rows that can never produce a fingerprint are skipped — same conservative
# set imohash_recompute.py uses for media_file, even though not every value
# is currently reachable for a document (the shared FileStatus vocabulary is
# read from app.core.enums either way, so this stays correct if that changes).
_SKIP_STATUSES = (FileStatus.ERROR, FileStatus.CANCELLED, FileStatus.ORPHANED)


def _count_eligible(db) -> int:
    """Count rows with storage that still need a fingerprint."""
    return int(
        db.query(Document.id)
        .filter(
            Document.file_hash.is_(None),
            Document.storage_path.isnot(None),
            Document.storage_path != "",
            Document.status.notin_(_SKIP_STATUSES),
        )
        .count()
    )


def _load_backfill_batch(after_id: int, batch_size: int) -> tuple[list[dict], bool]:
    """Read one batch of rows to fingerprint, then release the DB session.

    Returns **plain data only** — no ORM instances — so the caller can run the
    MinIO ranged reads with no transaction open. An escaping instance would
    lazy-load during the slow phase and silently reopen one (app/tasks/CLAUDE.md's
    session-lifetime rule).

    Args:
        after_id: Stable id cursor; only rows with ``id > after_id`` are read.
        batch_size: Maximum number of rows in the returned batch.

    Returns:
        ``(batch, has_more)`` where each batch item is
        ``{"id", "uuid", "storage_path", "file_size"}``.
    """
    with session_scope() as db:
        rows = (
            db.query(
                Document.id,
                Document.uuid,
                Document.storage_path,
                Document.file_size,
            )
            .filter(
                Document.file_hash.is_(None),
                Document.storage_path.isnot(None),
                Document.storage_path != "",
                Document.status.notin_(_SKIP_STATUSES),
            )
            .order_by(Document.id.asc())
            .limit(batch_size + 1)  # one extra to detect more
            .all()
        )

    has_more = len(rows) > batch_size
    batch = [
        {
            "id": int(row[0]),
            "uuid": str(row[1]),
            "storage_path": str(row[2]),
            "file_size": row[3],
        }
        for row in rows[:batch_size]
    ]
    return batch, has_more


def _store_fingerprints(fingerprints: dict[int, str]) -> None:
    """Write the computed fingerprints back in one short session.

    Args:
        fingerprints: ``{document_id: file_hash}`` for the rows that produced one.
    """
    if not fingerprints:
        return
    with session_scope() as db:
        for document_id, fingerprint in fingerprints.items():
            db.query(Document).filter(Document.id == document_id).update(
                {"file_hash": fingerprint}, synchronize_session=False
            )
        db.commit()


@celery_app.task(
    name="file_hash_recompute.backfill_document_file_hashes",
    bind=True,
    priority=CPUPriority.ADMIN_BATCH,
)
def backfill_document_file_hashes(self, batch_size: int = 100, after_id: int = 0) -> dict:
    """Backfill ``document.file_hash`` for every eligible row still NULL, batch by batch.

    Args:
        batch_size: Number of rows to backfill per batch (default 100).
        after_id: Stable id cursor — only rows with ``id > after_id`` are
            processed. Used for self-rescheduling; callers leave it at 0.

    Runs in three phases per batch, and the split is load-bearing: the DB
    session is open only for the two short DB-only phases (read the batch,
    write the fingerprints) and is **closed** across the middle phase, which
    does one MinIO ranged read per document. Holding the batch session across
    those reads kept a Postgres transaction — and therefore ``ACCESS SHARE``
    on ``document`` — open for the whole batch, queueing any ``ALTER TABLE``
    (i.e. an Alembic upgrade) behind it. See ``app/tasks/CLAUDE.md``.

    Returns:
        Per-batch statistics dict.
    """
    summary = {
        "documents_found": 0,
        "documents_backfilled": 0,
        "documents_skipped": 0,
        "documents_failed": 0,
        "has_more": False,
        "last_id": after_id,
    }

    try:
        # On the first batch, initialise progress tracking with the total count.
        if after_id == 0:
            with session_scope() as db:
                total = _count_eligible(db)
            backfill_progress.start_migration(total_files=total)
            logger.info("document file_hash backfill starting: %d eligible documents", total)

        # Phase 1 — read (DB session open, Postgres only).
        batch, has_more = _load_backfill_batch(after_id, batch_size)
        summary["has_more"] = has_more
        summary["documents_found"] = len(batch)

        # Phase 2 — MinIO ranged reads. NO DB session is held here.
        fingerprints: dict[int, str] = {}
        for row in batch:
            summary["last_id"] = row["id"]
            try:
                fingerprint = compute_from_minio(row["storage_path"], size=row["file_size"])
                if fingerprint:
                    fingerprints[row["id"]] = fingerprint
                    summary["documents_backfilled"] += 1
                    backfill_progress.increment_processed(success=True)
                else:
                    summary["documents_skipped"] += 1
                    backfill_progress.increment_processed(success=False, file_uuid=row["uuid"])
            except Exception as e:  # noqa: BLE001 - one bad document must not abort the batch
                logger.warning(
                    "document file_hash backfill failed for document %s: %s", row["id"], e
                )
                summary["documents_failed"] += 1
                backfill_progress.increment_processed(success=False, file_uuid=row["uuid"])

        # Phase 3 — write (DB session reopened, Postgres only).
        _store_fingerprints(fingerprints)

        if summary["has_more"]:
            logger.info(
                "document file_hash backfill batch done (through id=%s): %d backfilled, "
                "%d skipped, %d failed — scheduling next batch",
                summary["last_id"],
                summary["documents_backfilled"],
                summary["documents_skipped"],
                summary["documents_failed"],
            )
            backfill_document_file_hashes.apply_async(
                kwargs={"batch_size": batch_size, "after_id": summary["last_id"]},
                queue=CeleryQueues.UTILITY,
            )
        else:
            _finalize_backfill()

    except Exception as e:
        logger.error("document file_hash backfill task error: %s", e)
        summary["error"] = str(e)  # type: ignore[assignment]

    return summary


def _finalize_backfill() -> None:
    """Mark the backfill complete and set the one-time system-settings flag."""
    backfill_progress.complete_migration(success=True)
    with session_scope() as db:
        set_setting(
            db,
            BACKFILL_FLAG_KEY,
            "true",
            "One-time backfill of document.file_hash for rows written before "
            "the column was populated at ingest time. Completed.",
        )
    logger.info("document file_hash backfill complete — flag set, will not run again")
