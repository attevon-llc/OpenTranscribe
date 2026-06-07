"""Scheduled database backup — DB-backed settings, due-check, pg_dump, GFS pruning.

Feature C (in-app scheduled backups for homelab users). Everything is configured in
the admin UI and stored in ``SystemSettings``; the only env is the physical mount
(``BACKUP_HOST_PATH`` → container ``/backups`` via ``docker-compose.backup.yml``).
The existing ``celery-beat`` service fires a lightweight check every few minutes; when
the cron schedule is due, the real backup task runs ``pg_dump`` directly from the worker
(the backend image ships ``postgresql-client``), writes a custom-format ``.dump`` to the
destination, optionally gpg-encrypts it, then prunes old backups by a grandfather-father-son
(daily/weekly/monthly) policy.

Coded ``DEFAULT_BACKUP_*`` constants are the single source of truth — no ``.env`` vars.
The whole feature degrades gracefully when ``/backups`` isn't mounted: the status endpoint
flags it and the task no-ops with a logged warning instead of raising.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess  # noqa: S404 - pg_dump/gpg invoked with a fixed argv list, no shell
import time
from collections.abc import Iterator
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core import constants as C  # noqa: N812
from app.core.config import settings
from app.services import system_settings_service as sss

logger = logging.getLogger(__name__)

# --- SystemSettings keys ------------------------------------------------------
KEY_ENABLED = "backup.enabled"
KEY_SCHEDULE = "backup.schedule"
KEY_DESTINATION = "backup.destination"
KEY_RETENTION_DAILY = "backup.retention_daily"
KEY_RETENTION_WEEKLY = "backup.retention_weekly"
KEY_RETENTION_MONTHLY = "backup.retention_monthly"
KEY_ENCRYPT = "backup.encrypt"
KEY_PASSPHRASE_FILE = "backup.passphrase_file"  # noqa: S105  # nosec B105 - settings key name, not a secret
KEY_INCLUDE_OPENSEARCH = "backup.include_opensearch"
KEY_LAST_RUN_AT = "backup.last_run_at"
KEY_LAST_RESULT = "backup.last_result"

# Filenames we own. ``-Fc`` custom-format dumps + their optional gpg envelope.
_DUMP_PREFIX = "opentranscribe-"
_DUMP_SUFFIX = ".dump"
_GPG_SUFFIX = ".dump.gpg"
# opentranscribe-YYYYMMDD-HHMMSS.dump[.gpg]
_TS_RE = re.compile(r"opentranscribe-(\d{8})-(\d{6})\.dump(?:\.gpg)?$")


@contextlib.contextmanager
def _session(db: Session | None) -> Iterator[Session]:
    """Yield the passed session, or a short-lived one closed after use."""
    if db is not None:
        yield db
        return
    from app.db.base import SessionLocal

    own = SessionLocal()
    try:
        yield own
    finally:
        own.close()


# =============================================================================
# Settings round-trip
# =============================================================================
def get_settings(db: Session | None = None) -> dict[str, Any]:
    """Return all backup settings as a plain dict (coded defaults for unset keys)."""
    with _session(db) as s:
        last_result_raw = sss.get_setting(s, KEY_LAST_RESULT)
        last_result: dict[str, Any] | None = None
        if last_result_raw:
            try:
                last_result = json.loads(last_result_raw)
            except (ValueError, TypeError):
                last_result = None
        return {
            "enabled": sss.get_setting_bool(s, KEY_ENABLED, C.DEFAULT_BACKUP_ENABLED),
            "schedule": sss.get_setting(s, KEY_SCHEDULE) or C.DEFAULT_BACKUP_SCHEDULE,
            "destination": sss.get_setting(s, KEY_DESTINATION) or C.DEFAULT_BACKUP_DESTINATION,
            "retention_daily": sss.get_setting_int(
                s, KEY_RETENTION_DAILY, C.DEFAULT_BACKUP_RETENTION_DAILY
            ),
            "retention_weekly": sss.get_setting_int(
                s, KEY_RETENTION_WEEKLY, C.DEFAULT_BACKUP_RETENTION_WEEKLY
            ),
            "retention_monthly": sss.get_setting_int(
                s, KEY_RETENTION_MONTHLY, C.DEFAULT_BACKUP_RETENTION_MONTHLY
            ),
            "encrypt": sss.get_setting_bool(s, KEY_ENCRYPT, C.DEFAULT_BACKUP_ENCRYPT),
            "passphrase_file": sss.get_setting(s, KEY_PASSPHRASE_FILE)
            or C.DEFAULT_BACKUP_PASSPHRASE_FILE,
            "include_opensearch": sss.get_setting_bool(
                s, KEY_INCLUDE_OPENSEARCH, C.DEFAULT_BACKUP_INCLUDE_OPENSEARCH
            ),
            "last_run_at": sss.get_setting(s, KEY_LAST_RUN_AT),
            "last_result": last_result,
        }


def update_settings(
    db: Session,
    *,
    enabled: bool | None = None,
    schedule: str | None = None,
    destination: str | None = None,
    retention_daily: int | None = None,
    retention_weekly: int | None = None,
    retention_monthly: int | None = None,
    encrypt: bool | None = None,
    passphrase_file: str | None = None,
    include_opensearch: bool | None = None,
) -> dict[str, Any]:
    """Persist any provided backup settings; return the full current set."""
    if enabled is not None:
        sss.set_setting(db, KEY_ENABLED, enabled, "Scheduled database backups master toggle")
    if schedule is not None:
        if not is_valid_cron(schedule):
            raise ValueError(f"Invalid cron schedule: {schedule!r}")
        sss.set_setting(db, KEY_SCHEDULE, schedule, "Backup cron schedule (5-field, UTC)")
    if destination is not None:
        sss.set_setting(db, KEY_DESTINATION, destination, "Backup destination directory (mounted)")
    if retention_daily is not None:
        sss.set_setting(db, KEY_RETENTION_DAILY, int(retention_daily), "GFS: daily backups to keep")
    if retention_weekly is not None:
        sss.set_setting(
            db, KEY_RETENTION_WEEKLY, int(retention_weekly), "GFS: weekly backups to keep"
        )
    if retention_monthly is not None:
        sss.set_setting(
            db, KEY_RETENTION_MONTHLY, int(retention_monthly), "GFS: monthly backups to keep"
        )
    if encrypt is not None:
        sss.set_setting(db, KEY_ENCRYPT, encrypt, "Encrypt backups with gpg AES-256")
    if passphrase_file is not None:
        sss.set_setting(
            db, KEY_PASSPHRASE_FILE, passphrase_file, "In-container path to gpg passphrase file"
        )
    if include_opensearch is not None:
        sss.set_setting(
            db,
            KEY_INCLUDE_OPENSEARCH,
            include_opensearch,
            "Also snapshot OpenSearch (not yet implemented — pg only)",
        )
    return get_settings(db)


def update_settings_last_run(db: Session, run_at_iso: str) -> None:
    """Stamp ``backup.last_run_at`` (used by the beat to claim a due window)."""
    sss.set_setting(db, KEY_LAST_RUN_AT, run_at_iso, "Timestamp of the last backup run (UTC)")


# =============================================================================
# Cron parsing + due check (minimal, no croniter dependency)
# =============================================================================
def _parse_field(field: str, low: int, high: int) -> set[int]:
    """Expand one cron field (``*``, ``a-b``, ``*/n``, ``a,b``) into a value set."""
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        token = part
        if "/" in part:
            token, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"Invalid step in cron field: {part!r}")
        if token in ("*", ""):
            start, end = low, high
        elif "-" in token:
            start_s, end_s = token.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(token)
        if start < low or end > high or start > end:
            raise ValueError(f"Cron field out of range: {part!r} (expected {low}-{high})")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """Parse a 5-field cron expression into per-field value sets.

    Fields: minute hour day-of-month month day-of-week (0=Sunday).
    Raises ``ValueError`` on a malformed expression.
    """
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"Cron must have 5 fields, got {len(parts)}: {expr!r}")
    minute = _parse_field(parts[0], 0, 59)
    hour = _parse_field(parts[1], 0, 23)
    dom = _parse_field(parts[2], 1, 31)
    month = _parse_field(parts[3], 1, 12)
    dow = _parse_field(parts[4], 0, 6)
    return minute, hour, dom, month, dow


def is_valid_cron(expr: str) -> bool:
    """Return True if ``expr`` is a parseable 5-field cron expression."""
    try:
        parse_cron(expr)
        return True
    except (ValueError, TypeError):
        return False


def _matches(dt: datetime, cron: tuple[set[int], set[int], set[int], set[int], set[int]]) -> bool:
    """Return True if ``dt`` (minute resolution) satisfies the cron expression.

    Day matching follows Vixie cron: when both day-of-month and day-of-week are
    restricted (not ``*``), the minute fires if EITHER matches.
    """
    minute, hour, dom, month, dow = cron
    if dt.minute not in minute or dt.hour not in hour or dt.month not in month:
        return False
    # Python weekday(): Mon=0..Sun=6 → cron dow Sun=0..Sat=6
    cron_dow = (dt.weekday() + 1) % 7
    dom_restricted = dom != set(range(1, 32))
    dow_restricted = dow != set(range(7))
    if dom_restricted and dow_restricted:
        return dt.day in dom or cron_dow in dow
    return dt.day in dom and cron_dow in dow


def is_due(schedule: str, last_run_at: str | None, now: datetime | None = None) -> bool:
    """Decide whether a backup should run at ``now`` given the last run time.

    Fires if any cron-matching minute falls in the window ``(last_run_at, now]``.
    On the first run (``last_run_at`` is None) only the current minute is checked, so the
    beat can't trigger an immediate backup the instant scheduling is enabled — it waits for
    the next matching minute. ``now`` defaults to the current UTC time (truncated to minute).
    """
    cron = parse_cron(schedule)
    now = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if not last_run_at:
        return _matches(now, cron)

    try:
        last = datetime.fromisoformat(last_run_at)
    except (ValueError, TypeError):
        return _matches(now, cron)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    last = last.replace(second=0, microsecond=0)

    if last >= now:
        return False
    # Walk minute-by-minute through the gap; cap the scan so a stale/old last_run
    # (e.g. weeks of downtime) can't loop forever — one matching minute is enough.
    from datetime import timedelta

    cursor = last + timedelta(minutes=1)
    scanned = 0
    while cursor <= now and scanned < 44640:  # 31 days of minutes
        if _matches(cursor, cron):
            return True
        cursor += timedelta(minutes=1)
        scanned += 1
    return False


# =============================================================================
# Destination + listing
# =============================================================================
def destination_status(destination: str) -> dict[str, Any]:
    """Report whether the destination dir exists and is writable (mount check)."""
    path = Path(destination)
    exists = path.is_dir()
    writable = exists and os.access(path, os.W_OK)
    return {"destination": destination, "exists": exists, "writable": writable, "mounted": writable}


def _backup_timestamp(name: str) -> datetime | None:
    """Parse the embedded timestamp from one of our backup filenames."""
    m = _TS_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)}{m.group(2)}", "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def list_backups(destination: str) -> list[dict[str, Any]]:
    """List backup files in the destination, newest first."""
    path = Path(destination)
    if not path.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in path.iterdir():
        if not entry.is_file():
            continue
        ts = _backup_timestamp(entry.name)
        if ts is None:
            continue
        stat = entry.stat()
        out.append(
            {
                "filename": entry.name,
                "size_bytes": stat.st_size,
                "created_at": ts.isoformat(),
                "encrypted": entry.name.endswith(_GPG_SUFFIX),
            }
        )
    out.sort(key=lambda b: b["created_at"], reverse=True)
    return out


# =============================================================================
# GFS retention pruning (pure-python, tested)
# =============================================================================
def select_backups_to_delete(
    filenames: list[str],
    *,
    retention_daily: int,
    retention_weekly: int,
    retention_monthly: int,
) -> list[str]:
    """Return the subset of ``filenames`` to delete under a GFS policy.

    Grandfather-father-son: keep the N most-recent dailies, then N weeklies (one per
    ISO week, the most recent in each), then N monthlies (one per calendar month). A
    backup retained by any tier survives. Filenames that don't match our pattern are
    ignored (never deleted). Pure function — caller does the unlinking.
    """
    dated = [(name, ts) for name in filenames if (ts := _backup_timestamp(name)) is not None]
    dated.sort(key=lambda x: x[1], reverse=True)  # newest first

    keep: set[str] = set()

    # Daily: the N most-recent backups overall.
    for name, _ in dated[: max(retention_daily, 0)]:
        keep.add(name)

    # Weekly: most-recent backup per ISO (year, week), newest N weeks.
    seen_weeks: dict[tuple[int, int], str] = {}
    for name, ts in dated:
        key = ts.isocalendar()[:2]
        if key not in seen_weeks:
            seen_weeks[key] = name
    for key in sorted(seen_weeks, reverse=True)[: max(retention_weekly, 0)]:
        keep.add(seen_weeks[key])

    # Monthly: most-recent backup per (year, month), newest N months.
    seen_months: dict[tuple[int, int], str] = {}
    for name, ts in dated:
        key = (ts.year, ts.month)
        if key not in seen_months:
            seen_months[key] = name
    for key in sorted(seen_months, reverse=True)[: max(retention_monthly, 0)]:
        keep.add(seen_months[key])

    return [name for name, _ in dated if name not in keep]


def prune_backups(destination: str, settings_dict: dict[str, Any]) -> list[str]:
    """Delete old backups in ``destination`` per the GFS retention settings.

    Returns the list of deleted filenames.
    """
    path = Path(destination)
    if not path.is_dir():
        return []
    filenames = [b["filename"] for b in list_backups(destination)]
    to_delete = select_backups_to_delete(
        filenames,
        retention_daily=int(settings_dict["retention_daily"]),
        retention_weekly=int(settings_dict["retention_weekly"]),
        retention_monthly=int(settings_dict["retention_monthly"]),
    )
    deleted: list[str] = []
    for name in to_delete:
        try:
            (path / name).unlink()
            deleted.append(name)
        except OSError as exc:
            logger.warning("Could not delete old backup %s: %s", name, exc)
    return deleted


# =============================================================================
# pg_dump execution
# =============================================================================
def _read_passphrase(passphrase_file: str) -> str:
    """Read a gpg passphrase from a file; raise if missing/empty."""
    p = Path(passphrase_file)
    if not p.is_file():
        raise FileNotFoundError(f"Backup passphrase file not found: {passphrase_file}")
    passphrase = p.read_text(encoding="utf-8").strip()
    if not passphrase:
        raise ValueError(f"Backup passphrase file is empty: {passphrase_file}")
    return passphrase


def run_pg_dump(
    dest_path: Path,
    *,
    encrypt: bool,
    passphrase_file: str,
    database_url: str | None = None,
) -> Path:
    """Run ``pg_dump -Fc`` to ``dest_path``; optionally gpg-encrypt in place.

    Uses ``PGSSLMODE`` + a ``--dbname`` URL so no password lands on the argv. Returns the
    final artifact path (``.dump`` or ``.dump.gpg``). Raises ``CalledProcessError`` /
    ``FileNotFoundError`` on failure (the task catches and records these).
    """
    url = database_url or settings.DATABASE_URL
    env = dict(os.environ)
    cmd = ["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--dbname", url]
    logger.info("Running pg_dump → %s", dest_path)
    subprocess.run(  # noqa: S603  # nosec B603 - fixed argv, no shell; url carries credentials, not user input
        cmd,
        stdout=dest_path.open("wb"),
        stderr=subprocess.PIPE,
        env=env,
        check=True,
    )

    if not encrypt:
        return dest_path

    passphrase = _read_passphrase(passphrase_file)
    gpg_path = dest_path.with_suffix(dest_path.suffix + ".gpg")
    gpg_cmd = [
        "gpg",
        "--batch",
        "--yes",
        "--symmetric",
        "--cipher-algo",
        "AES256",
        "--passphrase-fd",
        "0",
        "--output",
        str(gpg_path),
        str(dest_path),
    ]
    subprocess.run(  # noqa: S603  # nosec B603 - fixed argv, no shell
        gpg_cmd,
        input=passphrase.encode("utf-8"),
        stderr=subprocess.PIPE,
        check=True,
    )
    dest_path.unlink(missing_ok=True)  # drop the plaintext dump
    return gpg_path


def perform_backup(db: Session | None = None) -> dict[str, Any]:
    """Execute one backup run end-to-end and record the result in SystemSettings.

    Returns a result dict (``ok``, ``path``/``filename``, ``size_bytes``, ``duration_s``,
    ``error``, ``pruned``). No-ops gracefully (``ok=False``, status ``"no_destination"``)
    when the destination isn't a writable mount — never raises for that case.
    """
    cfg = get_settings(db)
    destination = cfg["destination"]
    started = time.monotonic()
    now_iso = datetime.now(timezone.utc).isoformat()

    status = destination_status(destination)
    if not status["writable"]:
        msg = (
            f"Backup destination {destination!r} is not a writable mount — "
            "skipping. Mount a host path via docker-compose.backup.yml (BACKUP_HOST_PATH)."
        )
        logger.warning(msg)
        result = {"ok": False, "status": "no_destination", "error": msg, "started_at": now_iso}
        _record_result(db, now_iso, result)
        return result

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest_path = Path(destination) / f"{_DUMP_PREFIX}{ts}{_DUMP_SUFFIX}"

    try:
        artifact = run_pg_dump(
            dest_path,
            encrypt=bool(cfg["encrypt"]),
            passphrase_file=cfg["passphrase_file"],
        )
        size = artifact.stat().st_size
        pruned = prune_backups(destination, cfg)
        duration = round(time.monotonic() - started, 2)
        result = {
            "ok": True,
            "status": "success",
            "filename": artifact.name,
            "path": str(artifact),
            "size_bytes": size,
            "duration_s": duration,
            "encrypted": artifact.name.endswith(_GPG_SUFFIX),
            "pruned": pruned,
            "started_at": now_iso,
        }
        logger.info(
            "Backup complete: %s (%.1f MB, %.2fs, pruned %d)",
            artifact.name,
            size / 1_048_576,
            duration,
            len(pruned),
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", "replace")[-2000:] if exc.stderr else ""
        _cleanup_partial(dest_path)
        duration = round(time.monotonic() - started, 2)
        result = {
            "ok": False,
            "status": "error",
            "error": f"{exc.cmd[0]} failed (exit {exc.returncode}): {stderr}",
            "duration_s": duration,
            "started_at": now_iso,
        }
        logger.error("Backup failed: %s", result["error"])
    except (OSError, ValueError, FileNotFoundError) as exc:
        _cleanup_partial(dest_path)
        duration = round(time.monotonic() - started, 2)
        result = {
            "ok": False,
            "status": "error",
            "error": str(exc),
            "duration_s": duration,
            "started_at": now_iso,
        }
        logger.error("Backup failed: %s", exc)

    _record_result(db, now_iso, result)
    return result


def _cleanup_partial(dest_path: Path) -> None:
    """Remove a partial dump (and its gpg envelope) after a failed run."""
    for p in (dest_path, dest_path.with_suffix(dest_path.suffix + ".gpg")):
        with contextlib.suppress(OSError):
            p.unlink(missing_ok=True)


def _record_result(db: Session | None, run_at_iso: str, result: dict[str, Any]) -> None:
    """Persist last_run_at + last_result to SystemSettings (own session if needed)."""
    with _session(db) as s:
        sss.set_setting(s, KEY_LAST_RUN_AT, run_at_iso, "Timestamp of the last backup run (UTC)")
        sss.set_setting(s, KEY_LAST_RESULT, json.dumps(result), "Result of the last backup run")


def pg_dump_available() -> bool:
    """Return True if the ``pg_dump`` binary is on PATH (image capability probe)."""
    return shutil.which("pg_dump") is not None
