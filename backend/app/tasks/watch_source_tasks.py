"""Celery tasks + beat orchestration for Watch Sources (issue #26).

Flow:
  ``watch_source.scan_all`` (beat, every minute, utility queue)
      → dispatches ``watch_source.scan_single`` for each enabled source that is
        *due* per its ``polling_interval_minutes`` (DB-driven, no restart).
  ``watch_source.scan_single`` (download queue)
      → lists candidates, records age-skips, imports standalone files inline
        (bounded per scan), dispatches ``stitch_and_import`` for complete
        multi-part groups, tracks incomplete groups, updates scan status, and
        fires ``send_notification`` when email links exist.
  ``watch_source.stitch_and_import`` (cpu queue)  → ffmpeg-concat then ingest.
  ``watch_source.send_notification`` (utility)    → scan-summary email.
  ``watch_source.cleanup_temp`` (utility, hourly) → reap stale temp files.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile
from app.services import watch_settings_service
from app.services.watch_sources import create_client
from app.services.watch_sources import import_single_file
from app.services.watch_sources import ingest_prepared_file
from app.services.watch_sources.base import RemoteFileInfo
from app.services.watch_sources.base import parse_extensions
from app.utils.task_lock import task_lock_manager

logger = logging.getLogger(__name__)

# Tracking statuses that mean "already handled — don't re-list/import".
_TERMINAL = {
    "imported",
    "skipped_duplicate",
    "skipped_old",
    "skipped_invalid",
    "stitched_part",
}


def _file_to_dict(fi: RemoteFileInfo) -> dict:
    return {
        "path": fi.path,
        "name": fi.name,
        "size": fi.size,
        "modified": fi.modified_time.isoformat() if fi.modified_time else None,
    }


def _file_from_dict(d: dict) -> RemoteFileInfo:
    modified = datetime.fromisoformat(d["modified"]) if d.get("modified") else None
    return RemoteFileInfo(path=d["path"], name=d["name"], size=d["size"], modified_time=modified)


# --------------------------------------------------------------------------- #
# Beat orchestrator
# --------------------------------------------------------------------------- #
@celery_app.task(name="watch_source.scan_all", bind=True, priority=CPUPriority.SYSTEM)
def scan_all(self) -> dict:
    """Dispatch a scan for every enabled source that is due. Runs every minute."""
    summary = {"dispatched": 0, "skipped_not_due": 0}
    with task_lock_manager.acquire_lock("watch_source:scan_all", timeout=55) as acquired:
        if not acquired:
            return {"skipped": True, "reason": "scan_all already running"}

        with session_scope() as db:
            if not watch_settings_service.is_enabled(db):
                return {"skipped": True, "reason": "watch sources disabled"}

            now = datetime.now(UTC)
            sources = db.query(WatchSource).filter(WatchSource.is_enabled.is_(True)).all()
            due_ids: list[int] = []
            for src in sources:
                interval = timedelta(minutes=max(1, src.polling_interval_minutes or 15))
                last = src.last_scan_at
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                if last is None or (now - last) >= interval:
                    due_ids.append(int(src.id))
                else:
                    summary["skipped_not_due"] += 1

        for sid in due_ids:
            scan_single.delay(sid)
            summary["dispatched"] += 1
    return summary


# --------------------------------------------------------------------------- #
# Per-source scan
# --------------------------------------------------------------------------- #
@celery_app.task(name="watch_source.scan_single", bind=True, priority=CPUPriority.USER_TRIGGERED)
def scan_single(self, source_id: int) -> dict:
    """Scan one source: list, age-skip, import standalone, stitch multi-part."""
    summary = {"found": 0, "imported": 0, "skipped": 0, "errors": 0, "stitch_groups": 0}
    lock_key = f"watch_source:scan:{source_id}"

    with task_lock_manager.acquire_lock(lock_key, timeout=3600) as acquired:
        if not acquired:
            return {"skipped": True, "reason": "scan already running for this source"}

        scan_started = datetime.now(UTC)
        start_perf = time.perf_counter()
        with session_scope() as db:
            source = db.query(WatchSource).filter(WatchSource.id == source_id).first()
            if not source or not source.is_enabled:
                return {"skipped": True, "reason": "source missing or disabled"}

            source.last_scan_status = "running"
            db.flush()

            try:
                _perform_scan(db, source, summary, scan_started)
                source.last_scan_status = "success"
                source.last_scan_message = (
                    f"Found {summary['found']}, imported {summary['imported']}, "
                    f"skipped {summary['skipped']}"
                )
            except Exception as e:  # noqa: BLE001 - record and continue
                logger.error("Watch scan failed for source %s: %s", source_id, e, exc_info=True)
                source.last_scan_status = "error"
                source.last_scan_message = str(e)[:1000]
                summary["errors"] += 1

            source.last_scan_at = scan_started
            source.last_scan_files_found = summary["found"]
            source.last_scan_files_imported = summary["imported"]
            source.last_scan_files_skipped = summary["skipped"]
            source.last_scan_duration_seconds = round(time.perf_counter() - start_perf, 2)
            db.commit()

            _notify_scan_complete(int(source.user_id), source, summary)
            has_email = bool(source.email_links)

        if has_email:
            send_notification.apply_async(args=[source_id, summary], countdown=30)
    return summary


def _perform_scan(db, source: WatchSource, summary: dict, scan_started: datetime) -> None:
    """List the source, apply filters/dedup, import standalone, stitch groups.

    Mutates ``summary`` in place. Raises on connection/list failure so the
    caller records ``last_scan_status='error'``.
    """
    max_imports = watch_settings_service.max_concurrent_imports(db)
    extensions = parse_extensions(source.file_extensions)
    age_cutoff = None
    if source.skip_files_older_than_days is not None:
        age_cutoff = scan_started - timedelta(days=source.skip_files_older_than_days)

    with create_client(source) as client:
        files = client.list_files(extensions=extensions, recursive=bool(source.recursive))
        summary["found"] = len(files)

        # Drop files already in a terminal tracking state.
        terminal_paths = {
            r.remote_path
            for r in db.query(WatchSourceFile.remote_path, WatchSourceFile.status)
            .filter(
                WatchSourceFile.watch_source_id == source.id,
                WatchSourceFile.status.in_(_TERMINAL),
            )
            .all()
        }
        candidates = [f for f in files if f.path not in terminal_paths]

        # Record age-skips (so the user can see them), then drop them.
        if age_cutoff is not None:
            kept = []
            for f in candidates:
                if f.modified_time and f.modified_time < age_cutoff:
                    _record_age_skip(db, source, f)
                    summary["skipped"] += 1
                else:
                    kept.append(f)
            candidates = kept

        # Multi-part grouping (optional).
        standalone = candidates
        if source.multipart_enabled:
            from app.services.watch_sources import multipart

            groups, standalone = multipart.detect_groups(
                candidates, source.multipart_regex, source.multipart_time_window_hours
            )
            for group in groups:
                if _handle_group(db, source, group, max_imports):
                    summary["stitch_groups"] += 1

        # Import standalone files inline, bounded per scan.
        for fi in standalone[:max_imports]:
            row = import_single_file(db, source, fi, client)
            if row is None:
                continue
            if row.status == "imported":
                summary["imported"] += 1
            elif row.status.startswith("skipped"):
                summary["skipped"] += 1
            elif row.status == "error":
                summary["errors"] += 1


def _record_age_skip(db, source: WatchSource, fi: RemoteFileInfo) -> None:
    """Persist a one-time ``skipped_old`` row for a too-old file."""
    exists = (
        db.query(WatchSourceFile.id)
        .filter(
            WatchSourceFile.watch_source_id == source.id,
            WatchSourceFile.remote_path == fi.path,
        )
        .first()
    )
    if exists:
        return
    db.add(
        WatchSourceFile(
            uuid=uuid_pkg.uuid4(),
            watch_source_id=source.id,
            remote_path=fi.path,
            filename=fi.name,
            file_size=fi.size,
            file_modified_at=fi.modified_time,
            status="skipped_old",
            skip_reason="too_old",
            processed_at=datetime.now(UTC),
        )
    )
    db.flush()


def _handle_group(db, source: WatchSource, group, max_imports: int) -> bool:
    """Dispatch stitch for a complete group, or age the wait counter for an incomplete one.

    Returns True if a stitch was dispatched this scan.
    """
    # Upsert waiting rows for each part and track how many scans we've waited.
    waited = 0
    for part_num, fi in group.parts:
        row = (
            db.query(WatchSourceFile)
            .filter(
                WatchSourceFile.watch_source_id == source.id,
                WatchSourceFile.remote_path == fi.path,
            )
            .first()
        )
        if row is None:
            row = WatchSourceFile(
                uuid=uuid_pkg.uuid4(),
                watch_source_id=source.id,
                remote_path=fi.path,
                filename=fi.name,
                file_size=fi.size,
                file_modified_at=fi.modified_time,
                status="waiting_for_parts",
                part_group=group.base_name,
                part_number=part_num,
                retry_count=0,
            )
            db.add(row)
        elif row.status in _TERMINAL:
            continue
        else:
            row.status = "waiting_for_parts"
            row.part_group = group.base_name
            row.part_number = part_num
            row.retry_count = (row.retry_count or 0) + 1
        waited = max(waited, row.retry_count or 0)
    db.flush()

    ready = group.is_complete or (waited + 1) >= source.multipart_wait_scans
    if not ready:
        return False

    stitch_and_import.delay(
        source.id,
        group.base_name,
        group.extension,
        [_file_to_dict(fi) for fi in group.ordered_files],
    )
    return True


# --------------------------------------------------------------------------- #
# Multi-part stitch + import
# --------------------------------------------------------------------------- #
@celery_app.task(
    name="watch_source.stitch_and_import", bind=True, priority=CPUPriority.USER_TRIGGERED
)
def stitch_and_import(
    self, source_id: int, base_name: str, extension: str, parts: list[dict]
) -> dict:
    """Download the parts, ffmpeg-concat them, and import the single result."""
    from app.services.watch_sources import multipart

    temp_dir = settings.watch_temp_dir
    temp_dir.mkdir(parents=True, exist_ok=True)
    local_parts: list[str] = []
    stitched_path: str | None = None

    try:
        with session_scope() as db:
            source = db.query(WatchSource).filter(WatchSource.id == source_id).first()
            if not source:
                return {"skipped": True, "reason": "source missing"}

            file_infos = [_file_from_dict(d) for d in parts]
            stitched_name = multipart.generate_stitched_filename(base_name, extension)

            with create_client(source) as client:
                # Download every part into temp (ordered).
                for fi in file_infos:
                    dest = str(temp_dir / f"part_{source_id}_{uuid_pkg.uuid4().hex}{extension}")
                    if source.source_type == "local":
                        # Local parts are read in place.
                        local_parts.append(fi.path)
                    else:
                        client.download_file(fi.path, dest)
                        local_parts.append(dest)

                stitched_path = str(
                    temp_dir / f"stitched_{source_id}_{uuid_pkg.uuid4().hex}{extension}"
                )
                if not multipart.stitch_files(local_parts, stitched_path):
                    raise RuntimeError("ffmpeg stitch failed")

                # Tracking row for the stitched output (unique synthetic path).
                first_dir = os.path.dirname(file_infos[0].path)
                stitched_remote = (first_dir + "/" if first_dir else "") + stitched_name
                row = WatchSourceFile(
                    uuid=uuid_pkg.uuid4(),
                    watch_source_id=source.id,
                    remote_path=stitched_remote,
                    filename=stitched_name,
                    file_size=os.path.getsize(stitched_path),
                    status="importing",
                    part_group=base_name,
                )
                db.add(row)
                db.flush()

                result = ingest_prepared_file(
                    db, source, stitched_path, filename=stitched_name, row=row
                )

                stitched_media_id = result.media_file_id

                # Mark each part as a consumed stitched_part linked to the result.
                for fi in file_infos:
                    part_row = (
                        db.query(WatchSourceFile)
                        .filter(
                            WatchSourceFile.watch_source_id == source.id,
                            WatchSourceFile.remote_path == fi.path,
                        )
                        .first()
                    )
                    if part_row is None:
                        part_row = WatchSourceFile(
                            uuid=uuid_pkg.uuid4(),
                            watch_source_id=source.id,
                            remote_path=fi.path,
                            filename=fi.name,
                            file_size=fi.size,
                            part_group=base_name,
                        )
                        db.add(part_row)
                    part_row.status = "stitched_part"
                    part_row.media_file_id = stitched_media_id
                    part_row.processed_at = datetime.now(UTC)
                db.commit()

                # Optionally write the stitched file back to the source.
                if source.upload_stitched_to_source and source.source_type != "local":
                    try:
                        client.upload_file(stitched_path, stitched_remote)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("upload_stitched_to_source failed: %s", e)

            return {"status": result.status, "stitched": stitched_name}
    except Exception as e:  # noqa: BLE001
        logger.error("stitch_and_import failed for source %s group %s: %s", source_id, base_name, e)
        return {"status": "error", "error": str(e)}
    finally:
        if stitched_path and os.path.exists(stitched_path):
            with contextlib.suppress(OSError):
                os.remove(stitched_path)
        # Remove downloaded part temps (never the in-place local originals).
        for p in local_parts:
            if p.startswith(str(temp_dir)) and os.path.exists(p):
                with contextlib.suppress(OSError):
                    os.remove(p)


# --------------------------------------------------------------------------- #
# Email notification (scan summary)
# --------------------------------------------------------------------------- #
@celery_app.task(name="watch_source.send_notification", bind=True, priority=CPUPriority.MAINTENANCE)
def send_notification(self, source_id: int, summary: dict) -> dict:
    """Send a scan-summary email via each linked, enabled email config."""
    from app.services.watch_email_service import build_scan_summary_html
    from app.services.watch_email_service import send_email

    sent = 0
    with session_scope() as db:
        source = db.query(WatchSource).filter(WatchSource.id == source_id).first()
        if not source:
            return {"skipped": True}
        had_error = summary.get("errors", 0) > 0
        for link in source.email_links:
            cfg = link.email_config
            if not cfg or not cfg.is_enabled:
                continue
            if had_error and not link.notify_on_error:
                continue
            if not had_error and not link.notify_on_success:
                continue
            recipients = _merge_recipients(cfg.default_recipients, link.additional_recipients)
            if not recipients:
                continue
            subject = f"OpenTranscribe — watch source '{source.name}' scan complete"
            html = build_scan_summary_html(source.name, summary)
            ok, _msg = send_email(cfg, recipients, subject, html)
            if ok:
                sent += 1
    return {"sent": sent}


def _merge_recipients(default_csv: str | None, extra_csv: str | None) -> list[str]:
    out: list[str] = []
    for csv in (default_csv, extra_csv):
        if csv:
            out.extend(addr.strip() for addr in csv.split(",") if addr.strip())
    # De-dup, preserve order.
    seen: set[str] = set()
    deduped: list[str] = []
    for addr in out:
        if addr not in seen:
            seen.add(addr)
            deduped.append(addr)
    return deduped


# --------------------------------------------------------------------------- #
# Temp cleanup
# --------------------------------------------------------------------------- #
@celery_app.task(name="watch_source.cleanup_temp", bind=True, priority=CPUPriority.MAINTENANCE)
def cleanup_temp(self, max_age_hours: int = 2) -> dict:
    """Delete watch temp files older than ``max_age_hours``."""
    temp_dir = settings.watch_temp_dir
    if not temp_dir.exists():
        return {"removed": 0}
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for p in temp_dir.glob("*"):
        try:
            if (
                p.is_file()
                and p.name.startswith(("watch_", "part_", "stitched_"))
                and p.stat().st_mtime < cutoff
            ):
                p.unlink()
                removed += 1
        except OSError as e:
            logger.debug("cleanup_temp skip %s: %s", p, e)
    return {"removed": removed}


def _notify_scan_complete(user_id: int, source: WatchSource, summary: dict) -> None:
    """Best-effort WS event so the UI refreshes scan status live."""
    try:
        from app.utils.websocket_notify import send_ws_event

        send_ws_event(
            user_id,
            "watch_source_scan",
            {
                "source_uuid": str(source.uuid),
                "status": source.last_scan_status,
                "summary": summary,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("watch_source_scan WS notify failed: %s", e)
