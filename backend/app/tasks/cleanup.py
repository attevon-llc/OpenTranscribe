"""
Celery tasks for file cleanup and system maintenance.
"""

import logging
import math
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.celery import celery_app
from app.core.constants import UtilityPriority
from app.db.session_utils import session_scope
from app.services.file_cleanup_service import cleanup_service

logger = logging.getLogger(__name__)

#: Slowest upload rate the orphan sweeper assumes a real client can sustain
#: (~1.1 Mbit/s). A PENDING row younger than ``file_size`` at this rate may still
#: be an upload in flight, so it is left alone: the browser multipart path accepts
#: objects up to 15 GB, which no fixed 30-minute window can cover.
_MIN_UPLOAD_THROUGHPUT_BYTES_PER_MINUTE = 8 * 1024 * 1024

#: Hard cap on that derived window, so a wrong ``file_size`` cannot make a row
#: permanently un-sweepable.
_MAX_UPLOAD_GRACE_MINUTES = 48 * 60

#: Hour used when ``files.retention_run_time`` cannot be parsed. Matches the
#: ``"02:00"`` default in ``system_settings_service.get_retention_config``.
_DEFAULT_RETENTION_HOUR = 2


@celery_app.task(bind=True, name="cleanup.run_periodic_cleanup", priority=UtilityPriority.ROUTINE)
def run_periodic_cleanup(self):
    """
    Periodic task to clean up stuck files and maintain system health.

    This task should be run regularly (e.g., every 30 minutes) to:
    - Detect and recover stuck files
    - Mark orphaned files for cleanup
    - Generate system health reports
    """
    try:
        logger.info("Starting periodic cleanup cycle")

        # Run the cleanup cycle
        results = cleanup_service.run_cleanup_cycle()

        # Log results
        logger.info(
            f"Periodic cleanup completed: "
            f"checked {results['stuck_files_checked']} files, "
            f"recovered {results['files_recovered']}, "
            f"marked {results['files_marked_orphaned']} as orphaned"
        )

        if results["cleanup_errors"]:
            logger.warning(
                f"Cleanup had {len(results['cleanup_errors'])} errors: {results['cleanup_errors']}"
            )

        if results["recommendations"]:
            logger.info(f"System health recommendations: {results['recommendations']}")

        return results

    except Exception as e:
        logger.error(f"Critical error in periodic cleanup: {e}")
        # Don't retry automatically to avoid infinite loops
        raise self.retry(countdown=3600, max_retries=3) from e  # Retry in 1 hour


@celery_app.task(bind=True, name="cleanup.deep_cleanup", priority=UtilityPriority.BACKGROUND)
def run_deep_cleanup(self, dry_run: bool = False):
    """
    Deep cleanup task for removing orphaned files (admin-triggered).

    Args:
        dry_run: If True, only preview what would be cleaned up
    """
    try:
        logger.info(f"Starting deep cleanup (dry_run={dry_run})")

        with session_scope() as db:
            # Force cleanup of orphaned files
            results = cleanup_service.force_cleanup_orphaned_files(db, dry_run=dry_run)

            logger.info(
                f"Deep cleanup completed: "
                f"eligible: {results['eligible_for_deletion']}, "
                f"deleted: {results['successfully_deleted']}, "
                f"errors: {len(results['deletion_errors'])}"
            )

            if results["deletion_errors"]:
                logger.error(f"Deep cleanup errors: {results['deletion_errors']}")

            return results

    except Exception as e:
        logger.error(f"Critical error in deep cleanup: {e}")
        raise


@celery_app.task(bind=True, name="cleanup.health_check", priority=UtilityPriority.OPERATIONAL)
def system_health_check(self):
    """
    Generate a system health report.
    """
    try:
        logger.info("Running system health check")

        with session_scope() as db:
            stats = cleanup_service.get_cleanup_statistics(db)

            logger.info(
                f"System health check completed: "
                f"health_score={stats['health_score']}, "
                f"stuck_files={stats['stuck_files_detected']}, "
                f"cleanup_eligible={stats['files_eligible_for_cleanup']}"
            )

            # Log warnings for poor health
            if stats["health_score"] in ["poor", "fair"]:
                logger.warning(
                    f"System health is {stats['health_score']}. "
                    f"Consider running manual cleanup or investigating issues."
                )

            return stats

    except Exception as e:
        logger.error(f"Error in system health check: {e}")
        raise


@celery_app.task(bind=True, name="cleanup.emergency_recovery", priority=UtilityPriority.EMERGENCY)
def emergency_file_recovery(self, file_uuids: list):
    """
    Emergency recovery task for specific files (admin-triggered).

    Args:
        file_uuids: List of file UUIDs to attempt recovery on
    """
    from app.utils.uuid_helpers import get_file_by_uuid

    try:
        logger.info(f"Starting emergency recovery for files: {file_uuids}")

        results: dict[str, Any] = {
            "files_processed": len(file_uuids),
            "recovered": 0,
            "failed": 0,
            "errors": [],
        }

        with session_scope() as db:
            from app.utils.task_utils import recover_stuck_file

            for file_uuid in file_uuids:
                try:
                    # Convert UUID to internal ID
                    media_file = get_file_by_uuid(db, file_uuid)
                    file_id = int(media_file.id)

                    success = recover_stuck_file(db, file_id)
                    if success:
                        results["recovered"] += 1
                        logger.info(f"Successfully recovered file {file_id}")
                    else:
                        results["failed"] += 1
                        logger.warning(f"Failed to recover file {file_id}")
                except Exception as e:
                    results["failed"] += 1
                    error_msg = f"Error recovering file {file_uuid}: {str(e)}"
                    results["errors"].append(error_msg)
                    logger.error(error_msg)

            logger.info(
                f"Emergency recovery completed: "
                f"processed {results['files_processed']}, "
                f"recovered {results['recovered']}, "
                f"failed {results['failed']}"
            )

            return results

    except Exception as e:
        logger.error(f"Critical error in emergency recovery: {e}")
        raise


def _minutes_since(timestamp: datetime | None) -> float | None:
    """Age of ``timestamp`` in minutes, treating a naive value as UTC.

    Args:
        timestamp: Instant to measure from.

    Returns:
        Age in minutes, or None when ``timestamp`` is None.
    """
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return (datetime.now(UTC) - timestamp).total_seconds() / 60.0


def _upload_grace_minutes(file_size: int | None, base_minutes: int) -> int:
    """How long a PENDING row of this size must be left alone.

    Args:
        file_size: Declared upload size in bytes, if known.
        base_minutes: The sweeper's floor (``max_age_minutes``).

    Returns:
        The larger of the floor and the time this many bytes needs at
        :data:`_MIN_UPLOAD_THROUGHPUT_BYTES_PER_MINUTE`, capped at
        :data:`_MAX_UPLOAD_GRACE_MINUTES`.
    """
    if not file_size or file_size <= 0:
        return base_minutes
    needed = math.ceil(file_size / _MIN_UPLOAD_THROUGHPUT_BYTES_PER_MINUTE)
    return min(max(base_minutes, needed), _MAX_UPLOAD_GRACE_MINUTES)


@celery_app.task(bind=True, name="cleanup.orphan_upload_sweeper", priority=UtilityPriority.ROUTINE)
def orphan_upload_sweeper(self, max_age_minutes: int = 30) -> dict[str, int]:
    """Delete PENDING MediaFile rows abandoned before MinIO finished storing.

    A client disconnect mid-upload (or a prepare_upload call that never
    got a complete_upload) leaves a PENDING row that will never make
    progress. This sweeper looks for PENDING rows older than
    ``max_age_minutes`` and, per row:

    - **skips it while the upload could still be in flight.** A browser
      multipart upload may legitimately run for hours (the ceiling is 15 GB),
      and it holds its row at PENDING the whole time, so the window is derived
      from the declared ``file_size`` — see :func:`_upload_grace_minutes`. A
      fixed 30 minutes deleted the row, and the object, out from under a
      running upload.
    - **deletes the DB row FIRST, re-checking that it is still PENDING**, and
      only then the MinIO object. The old order deleted the object first, so a
      row that completed between the scan and the delete — or a row whose own
      delete failed — was left pointing at storage that no longer existed: a
      file the user still sees and can never open. The status re-check is the
      race guard; ``SELECT … FOR UPDATE SKIP LOCKED`` steps aside from a
      concurrent ``complete_upload``.

    Scheduled every 15 minutes via ``celery_app.conf.beat_schedule``.

    Args:
        max_age_minutes: Floor for the per-row window. Raise for noisy test
            environments; lower for tight dedup requirements.

    Returns:
        ``deleted_rows``, ``deleted_objects``, ``skipped_in_progress`` and
        ``errors``.
    """
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.services.minio_service import delete_file

    base_minutes = max(1, int(max_age_minutes))
    cutoff = datetime.now(UTC) - timedelta(minutes=base_minutes)
    deleted_rows = 0
    deleted_objects = 0
    skipped_in_progress = 0
    errors = 0

    try:
        with session_scope() as db:
            stale: list[MediaFile] = (
                db.query(MediaFile)
                .filter(
                    MediaFile.status == FileStatus.PENDING,
                    MediaFile.upload_time < cutoff,
                )
                .all()
            )

            for media_file in stale:
                file_id = int(media_file.id)
                storage_path = media_file.storage_path or ""
                age = _minutes_since(media_file.upload_time)
                grace = _upload_grace_minutes(media_file.file_size, base_minutes)
                if age is not None and age < grace:
                    skipped_in_progress += 1
                    logger.debug(
                        f"orphan_upload_sweeper: file {file_id} is {age:.0f} min old and its "
                        f"size allows {grace} min — could still be uploading, skipping"
                    )
                    continue

                try:
                    # Re-read under a row lock and confirm the row is STILL pending;
                    # complete_upload may have finished it since the scan above.
                    pending = (
                        db.query(MediaFile)
                        .filter(
                            MediaFile.id == file_id,
                            MediaFile.status == FileStatus.PENDING,
                        )
                        .with_for_update(skip_locked=True)
                        .first()
                    )
                    if pending is None:
                        skipped_in_progress += 1
                        logger.info(
                            f"orphan_upload_sweeper: file {file_id} is no longer pending "
                            "(or is locked by an active upload), leaving it alone"
                        )
                        continue

                    db.delete(pending)
                    db.commit()
                    deleted_rows += 1
                except Exception as row_err:
                    errors += 1
                    db.rollback()
                    logger.warning(f"orphan_upload_sweeper failed on file {file_id}: {row_err}")
                    continue

                # The row is gone; the object is now unreachable garbage. A missing
                # object is the common case — the client never made it to the PUT.
                if storage_path:
                    try:
                        delete_file(storage_path)
                        deleted_objects += 1
                    except Exception as obj_err:
                        logger.debug(
                            f"orphan_upload_sweeper: {storage_path} not deletable "
                            f"({obj_err}); its row is already gone"
                        )
    except Exception as e:
        logger.error(f"orphan_upload_sweeper error: {e}")
        errors += 1

    if deleted_rows or deleted_objects or errors or skipped_in_progress:
        logger.info(
            f"orphan_upload_sweeper: removed {deleted_rows} row(s), "
            f"{deleted_objects} object(s), skipped {skipped_in_progress} in-progress, "
            f"{errors} error(s) (cutoff={cutoff.isoformat()})"
        )
    return {
        "deleted_rows": deleted_rows,
        "deleted_objects": deleted_objects,
        "skipped_in_progress": skipped_in_progress,
        "errors": errors,
    }


@celery_app.task(bind=True, name="cleanup.scratch_janitor", priority=UtilityPriority.ROUTINE)
def scratch_janitor(self, ttl_seconds: int | None = None) -> dict[str, int]:
    """Purge stale per-file directories from the shared scratch volume.

    A crashed pipeline (OOM, worker restart, SIGKILL) can leave the
    ``audio.wav`` in scratch even though nothing will ever read it.
    This janitor sweeps any ``{file_uuid}/`` directory older than the
    TTL — well past the longest typical pipeline wall-clock — so the
    volume can't fill up over time.

    Scheduled hourly via ``celery_app.conf.beat_schedule``.
    """
    from app.utils.scratch_volume import DEFAULT_TTL_SECONDS
    from app.utils.scratch_volume import sweep_expired

    ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_TTL_SECONDS
    removed, errors = sweep_expired(ttl)
    if removed or errors:
        logger.info(
            f"scratch_janitor: removed {removed} stale dir(s), {errors} error(s) (ttl={ttl}s)"
        )
    return {"removed": removed, "errors": errors, "ttl_seconds": ttl}


def _scheduled_retention_hour(run_time: Any) -> int:
    """Parse ``files.retention_run_time`` (``"HH:MM"``) into an hour.

    Args:
        run_time: The configured value, whatever type it came back as.

    Returns:
        The hour 0-23, or :data:`_DEFAULT_RETENTION_HOUR` when the value cannot
        be parsed. A malformed value used to raise out of the guard block into
        the task's catch-all handler, which Celery recorded as SUCCESS — so one
        bad settings row silently stopped retention for good.
    """
    try:
        hour = int(str(run_time).split(":")[0])
    except (TypeError, ValueError):
        hour = -1
    if not 0 <= hour <= 23:
        logger.error(
            f"cleanup_expired_files: files.retention_run_time is {run_time!r}, which is not "
            f"HH:MM — falling back to {_DEFAULT_RETENTION_HOUR:02d}:00. Fix the setting."
        )
        return _DEFAULT_RETENTION_HOUR
    return hour


def _select_expired_files(
    db,
    config: dict,
    cutoff: datetime,
    query_cutoff: datetime,
    resolve_retention_days,
) -> list[tuple[int, str]]:
    """Return ``(file_id, file_uuid)`` for every file past its effective retention.

    Plain tuples, never ``MediaFile`` instances: the deletion phase runs with no
    session open, and an escaping instance would lazy-load (reopening a
    transaction) the moment ``purge_media_file`` touched it.
    """
    from app.models.media import FileStatus
    from app.models.media import MediaFile

    eligible_statuses = [FileStatus.COMPLETED.value]
    if config["delete_error_files"]:
        eligible_statuses.append(FileStatus.ERROR.value)

    candidates = (
        db.query(
            MediaFile.id,
            MediaFile.uuid,
            MediaFile.organization_id,
            MediaFile.completed_at,
            MediaFile.upload_time,
        )
        .filter(
            MediaFile.status.in_(eligible_statuses),
            ((MediaFile.completed_at.isnot(None)) & (MediaFile.completed_at < query_cutoff))
            | ((MediaFile.completed_at.is_(None)) & (MediaFile.upload_time < query_cutoff)),
        )
        .all()
    )
    logger.info(f"cleanup_expired_files: found {len(candidates)} candidate file(s)")

    expired: list[tuple[int, str]] = []
    for file_id, file_uuid, organization_id, completed_at, upload_time in candidates:
        # Per-file expiry against the file's EFFECTIVE retention: the per-org
        # override when present (cloud), else the global cutoff. Lets
        # longer-retention tenants keep files past the global window and
        # free-tier tenants expire them sooner — all from one candidate query.
        override = resolve_retention_days(organization_id)
        file_cutoff = cutoff if override is None else datetime.now(UTC) - timedelta(days=override)
        ref = completed_at or upload_time
        if ref is not None and ref < file_cutoff:
            expired.append((int(file_id), str(file_uuid)))
    return expired


def _purge_expired_files(expired: list[tuple[int, str]]) -> tuple[int, int]:
    """Delete each expired file in its **own short session**. Returns (deleted, failed).

    ``purge_media_file`` deletes from object storage and OpenSearch before it
    commits the row, so it needs a session — but it needs it for ONE file. The
    previous shape held a single transaction across the whole pass, i.e. across
    every one of those round trips for every expired file at once.
    """
    from app.models.media import MediaFile
    from app.services.file_cleanup_service import auto_delete_media_file

    deleted = 0
    failed = 0
    for file_id, file_uuid in expired:
        try:
            with session_scope() as db:
                # Eager-load speakers to avoid N+1 queries when
                # auto_delete_media_file iterates file.speakers.
                from sqlalchemy.orm import selectinload

                media_file = (
                    db.query(MediaFile)
                    .options(selectinload(MediaFile.speakers))
                    .filter(MediaFile.id == file_id)
                    .first()
                )
                if media_file is None:
                    continue
                result = auto_delete_media_file(db, media_file)
        except Exception as e:  # noqa: BLE001 - one bad file must not abort the pass
            failed += 1
            logger.error(
                f"cleanup_expired_files: failed to delete file id={file_id} uuid={file_uuid}: {e}"
            )
            continue

        if result["deleted"]:
            deleted += 1
            logger.info(f"cleanup_expired_files: deleted file id={file_id} uuid={file_uuid}")
        else:
            failed += 1
            logger.error(
                f"cleanup_expired_files: failed to delete file id={file_id} "
                f"uuid={file_uuid}: {result.get('error')}"
            )
    return deleted, failed


@celery_app.task(name="cleanup_expired_files", priority=UtilityPriority.ROUTINE)
def cleanup_expired_files(force: bool = False):
    """
    Delete media files that have exceeded the configured retention window.

    Reads retention configuration from system settings, checks whether the
    task is scheduled to run in the current hour (unless force=True), and
    deletes all eligible completed (and optionally error-status) files whose
    age exceeds the configured retention_days threshold.

    Args:
        force: When True, skip the enabled/hour/already-ran-today guards and
               execute the deletion pass unconditionally. A forced run does
               **not** stamp ``files.retention_last_run``: that field is what the
               already-ran-today guard reads, so an admin pressing "run now"
               used to cancel the day's scheduled pass.

    Returns:
        A dict with one of the following shapes:
        - ``{"status": "disabled"}`` – retention is turned off and force is False.
        - ``{"status": "not_scheduled_now"}`` – current hour does not match the
          configured run_time hour and force is False.
        - ``{"status": "already_ran_today"}`` – the task already completed
          successfully today in the configured timezone and force is False.
        - ``{"status": "completed", "deleted": int, "failed": int}`` – the
          deletion pass finished; deleted/failed counts reflect file outcomes.

    Raises:
        Exception: Any unexpected error propagates so Celery records the task as
            FAILURE. It used to be swallowed into ``{"status": "error"}``, which
            Celery records as SUCCESS — a permanently broken retention job then
            looked healthy on every one of its hourly runs.
    """
    from app.services.system_settings_service import get_retention_config
    from app.services.system_settings_service import set_setting

    try:
        # Phase 1 — read (short session, Postgres only). Everything that leaves
        # is plain data: the deletions below are MinIO + OpenSearch round trips
        # and must not run with this session open.
        with session_scope() as db:
            config = get_retention_config(db)

            if not force:
                # Guard 1: retention must be enabled
                if not config["retention_enabled"]:
                    logger.debug("cleanup_expired_files: retention disabled, skipping")
                    return {"status": "disabled"}

                # Guard 2: current hour must match the scheduled run hour
                tz = ZoneInfo(config["timezone"])
                now_local = datetime.now(tz)
                scheduled_hour = _scheduled_retention_hour(config["run_time"])
                if now_local.hour != scheduled_hour:
                    logger.debug(
                        f"cleanup_expired_files: not scheduled hour "
                        f"(now={now_local.hour}, scheduled={scheduled_hour}), skipping"
                    )
                    return {"status": "not_scheduled_now"}

                # Guard 3: must not have already run today in this timezone
                last_run_str = config["last_run"]
                if last_run_str is not None:
                    try:
                        last_run_utc = datetime.fromisoformat(last_run_str)
                        last_run_local = last_run_utc.astimezone(tz)
                        if last_run_local.date() == now_local.date():
                            logger.debug("cleanup_expired_files: already ran today, skipping")
                            return {"status": "already_ran_today"}
                    except (ValueError, TypeError) as parse_err:
                        logger.warning(
                            f"cleanup_expired_files: could not parse last_run "
                            f"'{last_run_str}': {parse_err}; proceeding with run"
                        )

            global_retention_days = config["retention_days"]
            # Build the age cutoff in UTC
            cutoff = datetime.now(UTC) - timedelta(days=global_retention_days)

            # Cloud-edition seam: a per-org retention OVERRIDE may keep files
            # longer (premium tier) or shorter (free tier) than the global
            # window. The candidate query must include every file that ANY
            # effective retention could expire, i.e. everything older than the
            # SHORTEST retention in effect; the per-file check below then
            # applies each file's own effective retention (longer-retention
            # tenants' files are simply kept). Community: no resolver -> min is
            # None -> unchanged global window.
            from app.core.tenant_limits import min_retention_override_days
            from app.core.tenant_limits import resolve_retention_days

            min_override = min_retention_override_days()
            query_days = (
                min(global_retention_days, min_override)
                if min_override is not None
                else global_retention_days
            )
            query_cutoff = datetime.now(UTC) - timedelta(days=query_days)

            expired = _select_expired_files(
                db, config, cutoff, query_cutoff, resolve_retention_days
            )

        logger.info(
            f"cleanup_expired_files: {len(expired)} file(s) past their effective retention "
            f"(query_cutoff={query_cutoff.isoformat()}, global_days={global_retention_days})"
        )

        # Phase 2 — delete. One SHORT session PER FILE, so a pass over hundreds
        # of files no longer holds a single transaction across hundreds of MinIO
        # and OpenSearch round trips.
        deleted, failed = _purge_expired_files(expired)

        # Phase 3 — write (short session, Postgres only).
        # Persist run metadata to system settings — store with explicit UTC offset
        # so the already_ran_today guard parses correctly on any server timezone.
        # A FORCED run deliberately does not claim the day: retention_last_run is
        # the already-ran-today guard's input, so stamping it from a manual run
        # suppressed that day's scheduled pass, which is the one that respects the
        # configured window.
        with session_scope() as db:
            if not force:
                run_timestamp = datetime.now(UTC).isoformat()
                set_setting(
                    db,
                    "files.retention_last_run",
                    run_timestamp,
                    "ISO UTC timestamp of the last retention cleanup run",
                )
            set_setting(
                db,
                "files.retention_last_run_deleted",
                deleted,
                "Number of files deleted in the most recent retention cleanup run",
            )

        logger.info(f"cleanup_expired_files: completed — deleted={deleted}, failed={failed}")
        return {"status": "completed", "deleted": deleted, "failed": failed}

    except Exception as exc:
        # Re-raise: an hourly task that deletes user media must report a failure AS a
        # failure. Returning an error dict made Celery record SUCCESS, so a retention
        # job broken by a bad setting or an unreachable MinIO was invisible.
        logger.error(f"cleanup_expired_files: unexpected error: {exc}", exc_info=True)
        raise
