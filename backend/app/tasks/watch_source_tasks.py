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
    """Scan one source: list, age-skip, import standalone, stitch multi-part.

    **Phased so that no DB session is open across the scan itself.** Previously
    one ``session_scope`` wrapped ``_perform_scan``, which runs
    ``client.list_files()`` against a remote share and then imports up to
    ``watch.max_imports_per_scan`` files *serially inline* — a download AND a
    MinIO upload each. Postgres spent all of it "idle in transaction": ACCESS
    SHARE held (so any ``ALTER TABLE``, i.e. an Alembic upgrade, queues behind
    it), the vacuum horizon pinned, and a pool connection consumed. This was
    larger than the ``stitch_and_import`` leak already fixed below, and an AST
    body-scan missed it because the slow calls are one frame down.
    """
    summary = {"found": 0, "imported": 0, "skipped": 0, "errors": 0, "stitch_groups": 0}
    lock_key = f"watch_source:scan:{source_id}"

    with task_lock_manager.acquire_lock(lock_key, timeout=3600) as acquired:
        if not acquired:
            return {"skipped": True, "reason": "scan already running for this source"}

        scan_started = datetime.now(UTC)
        start_perf = time.perf_counter()

        # Phase 1 — read + claim (short session). ``create_client`` happens here and
        # can refuse (unknown type, blocked private endpoint, missing optional
        # dependency); that used to land inside ``_perform_scan``'s try block and be
        # recorded as a scan error, so it still must be — otherwise the source is
        # left reading "running" forever.
        try:
            plan = _load_scan_plan(source_id)
        except Exception as e:  # noqa: BLE001 - record and continue
            logger.error("Watch scan setup failed for source %s: %s", source_id, e, exc_info=True)
            summary["errors"] += 1
            _record_scan_result(
                source_id,
                summary,
                scan_started,
                round(time.perf_counter() - start_perf, 2),
                "error",
                str(e)[:1000],
            )
            return summary

        if plan is None:
            return {"skipped": True, "reason": "source missing or disabled"}

        # Phase 2 — the scan. NO DB session is held across it; each DB touch
        # inside opens its own short scope.
        try:
            with plan["client"] as client:
                _perform_scan(source_id, plan, client, summary, scan_started)
            status = "success"
            message = (
                f"Found {summary['found']}, imported {summary['imported']}, "
                f"skipped {summary['skipped']}"
            )
        except Exception as e:  # noqa: BLE001 - record and continue
            logger.error("Watch scan failed for source %s: %s", source_id, e, exc_info=True)
            status = "error"
            message = str(e)[:1000]
            summary["errors"] += 1

        # Phase 3 — write (short session).
        outcome = _record_scan_result(
            source_id,
            summary,
            scan_started,
            round(time.perf_counter() - start_perf, 2),
            status,
            message,
        )
        if outcome is None:
            return summary

        _notify_scan_complete(outcome["user_id"], outcome["source_uuid"], status, summary)
        if outcome["has_email"]:
            send_notification.apply_async(args=[source_id, summary], countdown=30)
    return summary


def _load_scan_plan(source_id: int) -> dict | None:
    """Phase 1 — snapshot the scan's inputs and mark the source ``running``.

    Returns plain scalars plus the client. The ``WatchSource`` row is
    ``expunge``d rather than merely referenced: ``LocalWatchClient`` keeps a
    reference to it (it reads ``resolved_local_path``/``recursive``), and a
    detached instance with its columns already loaded outlives the session while
    turning any stray RELATIONSHIP load into a loud ``DetachedInstanceError``
    instead of a silent second transaction mid-scan.
    """
    with session_scope() as db:
        source = db.query(WatchSource).filter(WatchSource.id == source_id).first()
        if not source or not source.is_enabled:
            return None

        source.last_scan_status = "running"
        plan = {
            "extensions": parse_extensions(source.file_extensions),
            "recursive": bool(source.recursive),
            "skip_files_older_than_days": source.skip_files_older_than_days,
            "multipart_enabled": bool(source.multipart_enabled),
            "multipart_regex": source.multipart_regex,
            "multipart_time_window_hours": source.multipart_time_window_hours,
            "multipart_wait_scans": source.multipart_wait_scans,
            "max_imports": watch_settings_service.max_imports_per_scan(db),
            "client": create_client(source),
        }
        # Flush BEFORE expunging: expunging a dirty instance would discard the
        # pending ``last_scan_status`` update.
        db.flush()
        db.expunge(source)
    return plan


def _record_scan_result(
    source_id: int,
    summary: dict,
    scan_started: datetime,
    duration_seconds: float,
    status: str,
    message: str,
) -> dict | None:
    """Phase 3 — persist scan status/counters and report what to notify."""
    with session_scope() as db:
        source = db.query(WatchSource).filter(WatchSource.id == source_id).first()
        if source is None:
            return None
        source.last_scan_status = status
        source.last_scan_message = message
        source.last_scan_at = scan_started
        source.last_scan_files_found = summary["found"]
        source.last_scan_files_imported = summary["imported"]
        source.last_scan_files_skipped = summary["skipped"]
        source.last_scan_duration_seconds = duration_seconds
        return {
            "user_id": int(source.user_id),
            "source_uuid": str(source.uuid),
            "has_email": bool(source.email_links),
        }


def _load_terminal_paths(source_id: int) -> set[str]:
    """Remote paths already in a terminal tracking state (short session)."""
    with session_scope() as db:
        return {
            row[0]
            for row in db.query(WatchSourceFile.remote_path)
            .filter(
                WatchSourceFile.watch_source_id == source_id,
                WatchSourceFile.status.in_(_TERMINAL),
            )
            .all()
        }


def _perform_scan(
    source_id: int,
    plan: dict,
    client,
    summary: dict,
    scan_started: datetime,
) -> None:
    """List the source, apply filters/dedup, import standalone, stitch groups.

    Mutates ``summary`` in place. Raises on connection/list failure so the
    caller records ``last_scan_status='error'``.

    **No DB session is open on entry or across the slow calls here.** Every DB
    touch below opens its own short scope; ``client.list_files()`` and the
    per-file ``import_single_file`` transfers run with none.
    """
    age_cutoff = None
    if plan["skip_files_older_than_days"] is not None:
        age_cutoff = scan_started - timedelta(days=plan["skip_files_older_than_days"])

    files = client.list_files(extensions=plan["extensions"], recursive=plan["recursive"])
    summary["found"] = len(files)

    # Drop files already in a terminal tracking state.
    terminal_paths = _load_terminal_paths(source_id)
    candidates = [f for f in files if f.path not in terminal_paths]

    # Record age-skips (so the user can see them), then drop them.
    if age_cutoff is not None:
        too_old = [f for f in candidates if f.modified_time and f.modified_time < age_cutoff]
        if too_old:
            _record_age_skips(source_id, too_old)
            summary["skipped"] += len(too_old)
        candidates = [
            f for f in candidates if not (f.modified_time and f.modified_time < age_cutoff)
        ]

    # Multi-part grouping (optional).
    standalone = candidates
    if plan["multipart_enabled"]:
        from app.services.watch_sources import multipart

        groups, standalone = multipart.detect_groups(
            candidates, plan["multipart_regex"], plan["multipart_time_window_hours"]
        )
        for group in groups:
            if _handle_group(source_id, group, plan["multipart_wait_scans"]):
                summary["stitch_groups"] += 1

    # Import standalone files inline, bounded per scan.
    for fi in standalone[: plan["max_imports"]]:
        status = import_single_file(source_id, fi, client)
        if status is None:
            continue
        if status == "imported":
            summary["imported"] += 1
        elif status.startswith("skipped"):
            summary["skipped"] += 1
        elif status == "error":
            summary["errors"] += 1


def _record_age_skips(source_id: int, files: list[RemoteFileInfo]) -> None:
    """Persist one-time ``skipped_old`` rows for too-old files (one short session)."""
    with session_scope() as db:
        for fi in files:
            exists = (
                db.query(WatchSourceFile.id)
                .filter(
                    WatchSourceFile.watch_source_id == source_id,
                    WatchSourceFile.remote_path == fi.path,
                )
                .first()
            )
            if exists:
                continue
            db.add(
                WatchSourceFile(
                    uuid=uuid_pkg.uuid4(),
                    watch_source_id=source_id,
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


def _handle_group(source_id: int, group, wait_scans: int) -> bool:
    """Dispatch stitch for a complete group, or age the wait counter for an incomplete one.

    Returns True if a stitch was dispatched this scan. The tracking upserts run
    in a short session; the dispatch happens after it closes.
    """
    # Upsert waiting rows for each part and track how many scans we've waited.
    waited = 0
    with session_scope() as db:
        for part_num, fi in group.parts:
            row = (
                db.query(WatchSourceFile)
                .filter(
                    WatchSourceFile.watch_source_id == source_id,
                    WatchSourceFile.remote_path == fi.path,
                )
                .first()
            )
            if row is None:
                row = WatchSourceFile(
                    uuid=uuid_pkg.uuid4(),
                    watch_source_id=source_id,
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
                # ``retry_count`` means two different things by status: failed import
                # ATTEMPTS while the row is standalone (``_record_error`` increments
                # it), and SCANS WAITED once the row is part of a group. A row joining
                # the group carrying prior failures used to inherit them as waiting,
                # so a part that had errored twice made ``(waited + 1) >= wait_scans``
                # true on the very first grouping scan and an incomplete recording was
                # stitched — silently truncated, then transcribed as if whole. Reset on
                # ENTRY only; a row already waiting must keep ageing or the
                # missing-parts timeout would never fire.
                if row.status != "waiting_for_parts":
                    row.retry_count = 0
                row.status = "waiting_for_parts"
                row.part_group = group.base_name
                row.part_number = part_num
                row.retry_count = (row.retry_count or 0) + 1
            waited = max(waited, row.retry_count or 0)
        db.flush()

    ready = group.is_complete or (waited + 1) >= wait_scans
    if not ready:
        return False

    stitch_and_import.delay(
        source_id,
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
    """Download the parts, ffmpeg-concat them, and import the single result.

    Phased so that **no DB session is open during the part downloads or the
    ffmpeg concat**. Previously one ``session_scope`` wrapped the whole body:
    a multi-part SMB/S3 group is gigabytes of transfer plus a full ffmpeg
    concat, and Postgres spent all of it "idle in transaction" — blocking
    ``ALTER TABLE`` and pinning the cluster-wide vacuum horizon.
    """
    from app.services.watch_sources import multipart

    temp_dir = settings.watch_temp_dir
    temp_dir.mkdir(parents=True, exist_ok=True)
    local_parts: list[str] = []
    stitched_path: str | None = None

    try:
        # Phase 1 — read (short). Snapshot plain scalars and build the client.
        with session_scope() as db:
            source = db.query(WatchSource).filter(WatchSource.id == source_id).first()
            if not source:
                return {"skipped": True, "reason": "source missing"}
            source_type = str(source.source_type)
            upload_stitched = bool(source.upload_stitched_to_source)
            # Local parts are read in place and are never written back, so the
            # local client is not needed here — and it is the one client that
            # keeps a reference to the WatchSource ORM row, which must not
            # outlive this session. The S3/SMB clients capture decrypted
            # credentials as plain values.
            client = create_client(source) if source_type != "local" else None

        file_infos = [_file_from_dict(d) for d in parts]
        stitched_name = multipart.generate_stitched_filename(base_name, extension)
        first_dir = os.path.dirname(file_infos[0].path)
        stitched_remote = (first_dir + "/" if first_dir else "") + stitched_name

        with contextlib.ExitStack() as stack:
            if client is not None:
                stack.enter_context(client)

            # Phase 2 — part downloads + ffmpeg concat. NO DB session held.
            for fi in file_infos:
                dest = str(temp_dir / f"part_{source_id}_{uuid_pkg.uuid4().hex}{extension}")
                if client is None:
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

            # Phase 3 — write (short): tracking rows + ingest of the one
            # stitched artifact.
            with session_scope() as db:
                source = db.query(WatchSource).filter(WatchSource.id == source_id).first()
                if not source:
                    return {"skipped": True, "reason": "source missing"}

                # Tracking row for the stitched output (unique synthetic path).
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
                result_status = str(result.status)

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

            # Phase 4 — optional write-back to the source. NO DB session held.
            if upload_stitched and client is not None:
                try:
                    client.upload_file(stitched_path, stitched_remote)
                except Exception as e:  # noqa: BLE001
                    logger.warning("upload_stitched_to_source failed: %s", e)

        return {"status": result_status, "stitched": stitched_name}
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
    """Send a scan-summary email via each linked, enabled email config.

    The session is closed before any mail is sent: ``send_email`` is an SMTP or
    Microsoft-Graph round trip with a 30 s timeout *per config*, and holding the
    task's transaction across it pinned the cluster-wide vacuum horizon for the
    duration with nothing to show for it.
    """
    from app.models.email_notification_config import EmailNotificationConfig
    from app.services.watch_email_service import build_scan_summary_html
    from app.services.watch_email_service import send_email

    had_error = summary.get("errors", 0) > 0

    # Phase 1 — read (short).
    with session_scope() as db:
        source = db.query(WatchSource).filter(WatchSource.id == source_id).first()
        if not source:
            return {"skipped": True}
        source_name = str(source.name)

        deliveries: list[tuple[EmailNotificationConfig, list[str]]] = []
        for link in source.email_links:
            cfg = link.email_config
            # A link can be present, enabled and flagged, and still deliver nothing.
            # Both skips below used to be silent, so an admin who never received mail
            # had no signal anywhere — not in the UI, not in the API, not in the log.
            # WARNING (not DEBUG): the operator asked for this notification, and not
            # sending it is a departure from what they configured.
            if not cfg:
                logger.warning(
                    "Watch source %r: an email link points at a config that no longer "
                    "exists; nothing sent for this link.",
                    source_name,
                )
                continue
            if not cfg.is_enabled:
                logger.warning(
                    "Watch source %r: email config %r is disabled, so no notification "
                    "was sent through it. Enable the config to resume delivery.",
                    source_name,
                    cfg.name,
                )
                continue
            if had_error and not link.notify_on_error:
                continue
            if not had_error and not link.notify_on_success:
                continue
            recipients = _merge_recipients(cfg.default_recipients, link.additional_recipients)
            if not recipients:
                logger.warning(
                    "Watch source %r: email config %r resolved to no recipients "
                    "(the config has no default recipients and the link adds none), "
                    "so nothing was sent.",
                    source_name,
                    cfg.name,
                )
                continue
            # ``send_email`` needs the config OBJECT (it reads ~10 columns and
            # decrypts a stored secret). Expunge detaches it with its column
            # values already loaded, so it outlives the session; and because it
            # is detached rather than merely closed-over, an accidental lazy
            # load raises DetachedInstanceError loudly instead of silently
            # opening a second transaction mid-send.
            db.expunge(cfg)
            deliveries.append((cfg, recipients))

    # Phase 2 — send. NO DB session held.
    subject = f"OpenTranscribe — watch source '{source_name}' scan complete"
    html = build_scan_summary_html(source_name, summary)

    sent = 0
    for cfg, recipients in deliveries:
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


def _notify_scan_complete(user_id: int, source_uuid: str, status: str, summary: dict) -> None:
    """Best-effort WS event so the UI refreshes scan status live.

    Takes plain scalars, not the ``WatchSource`` row: an ORM instance reaching
    here would lazy-load after its session closed.
    """
    try:
        from app.utils.websocket_notify import send_ws_event

        send_ws_event(
            user_id,
            "watch_source_scan",
            {
                "source_uuid": source_uuid,
                "status": status,
                "summary": summary,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("watch_source_scan WS notify failed: %s", e)
