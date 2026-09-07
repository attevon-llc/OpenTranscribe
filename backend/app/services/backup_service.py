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
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core import constants as C  # noqa: N812
from app.core.config import settings
from app.services import system_settings_service as sss
from app.utils.encryption import decrypt_api_key
from app.utils.encryption import encrypt_api_key

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
# Failure-surfacing state (issues #243/#244): cumulative run counts + last-success
# timestamp back the scrape-time Prometheus refresh (core/backup_metrics.py); the
# notice flag gates the one-time "backups exclude your encryption keys" admin warning.
KEY_RUNS_SUCCESS = "backup.runs_success_total"
KEY_RUNS_FAILURE = "backup.runs_failure_total"
KEY_LAST_SUCCESS_AT = "backup.last_success_at"
KEY_RECOVERY_NOTICE_SENT = "backup.recovery_notice_sent"
# S3-compatible destination (off-host backups).
KEY_DESTINATION_TYPE = "backup.destination_type"
KEY_S3_ENDPOINT_URL = "backup.s3_endpoint_url"
KEY_S3_REGION = "backup.s3_region"
KEY_S3_BUCKET = "backup.s3_bucket"
KEY_S3_PREFIX = "backup.s3_prefix"
KEY_S3_ACCESS_KEY_ID = "backup.s3_access_key_id"
KEY_S3_SECRET_KEY = "backup.s3_secret_key"  # noqa: S105  # nosec B105 - settings key name, not a secret

DEST_LOCAL = "local"
DEST_S3 = "s3"

_S3_CONNECT_TIMEOUT = 10
_S3_READ_TIMEOUT = 60

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
        # One SELECT for every backup key instead of 11 round-trips.
        vals = sss.get_settings_map(
            s,
            [
                KEY_LAST_RESULT,
                KEY_ENABLED,
                KEY_SCHEDULE,
                KEY_DESTINATION,
                KEY_RETENTION_DAILY,
                KEY_RETENTION_WEEKLY,
                KEY_RETENTION_MONTHLY,
                KEY_ENCRYPT,
                KEY_PASSPHRASE_FILE,
                KEY_INCLUDE_OPENSEARCH,
                KEY_LAST_RUN_AT,
                KEY_DESTINATION_TYPE,
                KEY_S3_ENDPOINT_URL,
                KEY_S3_REGION,
                KEY_S3_BUCKET,
                KEY_S3_PREFIX,
                KEY_S3_ACCESS_KEY_ID,
                KEY_S3_SECRET_KEY,
            ],
        )

        def _b(key: str, default: bool) -> bool:
            v = vals.get(key)
            return v.lower() in ("true", "1", "yes", "on") if v is not None else default

        def _i(key: str, default: int) -> int:
            v = vals.get(key)
            if v is None:
                return default
            try:
                return int(v)
            except (ValueError, TypeError):
                return default

        last_result_raw = vals.get(KEY_LAST_RESULT)
        last_result: dict[str, Any] | None = None
        if last_result_raw:
            try:
                last_result = json.loads(last_result_raw)
            except (ValueError, TypeError):
                last_result = None
        return {
            "enabled": _b(KEY_ENABLED, C.DEFAULT_BACKUP_ENABLED),
            "schedule": vals.get(KEY_SCHEDULE) or C.DEFAULT_BACKUP_SCHEDULE,
            "destination": vals.get(KEY_DESTINATION) or C.DEFAULT_BACKUP_DESTINATION,
            "retention_daily": _i(KEY_RETENTION_DAILY, C.DEFAULT_BACKUP_RETENTION_DAILY),
            "retention_weekly": _i(KEY_RETENTION_WEEKLY, C.DEFAULT_BACKUP_RETENTION_WEEKLY),
            "retention_monthly": _i(KEY_RETENTION_MONTHLY, C.DEFAULT_BACKUP_RETENTION_MONTHLY),
            "encrypt": _b(KEY_ENCRYPT, C.DEFAULT_BACKUP_ENCRYPT),
            "passphrase_file": vals.get(KEY_PASSPHRASE_FILE) or C.DEFAULT_BACKUP_PASSPHRASE_FILE,
            "include_opensearch": _b(KEY_INCLUDE_OPENSEARCH, C.DEFAULT_BACKUP_INCLUDE_OPENSEARCH),
            "last_run_at": vals.get(KEY_LAST_RUN_AT),
            "last_result": last_result,
            "destination_type": vals.get(KEY_DESTINATION_TYPE) or C.DEFAULT_BACKUP_DESTINATION_TYPE,
            "s3_endpoint_url": vals.get(KEY_S3_ENDPOINT_URL) or C.DEFAULT_BACKUP_S3_ENDPOINT_URL,
            "s3_region": vals.get(KEY_S3_REGION) or C.DEFAULT_BACKUP_S3_REGION,
            "s3_bucket": vals.get(KEY_S3_BUCKET) or C.DEFAULT_BACKUP_S3_BUCKET,
            "s3_prefix": vals.get(KEY_S3_PREFIX) or C.DEFAULT_BACKUP_S3_PREFIX,
            "s3_access_key_id": vals.get(KEY_S3_ACCESS_KEY_ID) or C.DEFAULT_BACKUP_S3_ACCESS_KEY_ID,
            # NEVER expose the secret — only whether one is configured.
            "s3_secret_key_set": bool(vals.get(KEY_S3_SECRET_KEY)),
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
    destination_type: str | None = None,
    s3_endpoint_url: str | None = None,
    s3_region: str | None = None,
    s3_bucket: str | None = None,
    s3_prefix: str | None = None,
    s3_access_key_id: str | None = None,
    s3_secret_key: str | None = None,
) -> dict[str, Any]:
    """Persist any provided backup settings; return the full current set.

    ``s3_secret_key`` is AES-256-GCM encrypted before storage and is never echoed back
    (``get_settings`` exposes only ``s3_secret_key_set``).
    """
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
            "Also take an OpenSearch snapshot alongside the pg dump (convenience; rebuildable)",
        )
    if destination_type is not None:
        if destination_type not in (DEST_LOCAL, DEST_S3):
            raise ValueError(f"Invalid destination_type: {destination_type!r}")
        sss.set_setting(
            db, KEY_DESTINATION_TYPE, destination_type, "Backup destination type (local | s3)"
        )
    # Plain (non-secret) S3 string fields — data-driven to keep this function simple.
    _s3_plain = (
        (s3_endpoint_url, KEY_S3_ENDPOINT_URL, "S3 endpoint URL (empty = real AWS S3)"),
        (s3_region, KEY_S3_REGION, "S3 region (e.g. us-east-1)"),
        (s3_bucket, KEY_S3_BUCKET, "S3 destination bucket"),
        (s3_prefix, KEY_S3_PREFIX, "S3 key prefix within the bucket"),
        (s3_access_key_id, KEY_S3_ACCESS_KEY_ID, "S3 access key id"),
    )
    for value, key, desc in _s3_plain:
        if value is not None:
            sss.set_setting(db, key, value, desc)
    if s3_secret_key is not None:
        _store_s3_secret(db, s3_secret_key)
    return get_settings(db)


def _store_s3_secret(db: Session, s3_secret_key: str) -> None:
    """Encrypt + persist the S3 secret (empty string clears it)."""
    if not s3_secret_key:
        sss.set_setting(db, KEY_S3_SECRET_KEY, "", "S3 secret access key (encrypted)")
        return
    encrypted = encrypt_api_key(s3_secret_key)
    if not encrypted:
        raise ValueError("Failed to encrypt S3 secret key")
    sss.set_setting(db, KEY_S3_SECRET_KEY, encrypted, "S3 secret access key (encrypted)")


def _get_s3_secret_key(db: Session | None) -> str | None:
    """Decrypt and return the stored S3 secret key (runtime-only — never exposed via API)."""
    with _session(db) as s:
        raw = sss.get_setting(s, KEY_S3_SECRET_KEY)
    if not raw:
        return None
    return decrypt_api_key(raw)


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
    now = (now or datetime.now(UTC)).replace(second=0, microsecond=0)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    if not last_run_at:
        return _matches(now, cron)

    try:
        last = datetime.fromisoformat(last_run_at)
    except (ValueError, TypeError):
        return _matches(now, cron)
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
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
        return datetime.strptime(f"{m.group(1)}{m.group(2)}", "%Y%m%d%H%M%S").replace(tzinfo=UTC)
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
# S3-compatible destination (boto3 — AWS S3 / MinIO / Backblaze / Wasabi / etc.)
# =============================================================================
def _build_s3_client(cfg: dict[str, Any], secret_key: str | None):
    """Construct a boto3 S3 client for the configured endpoint (path-style for non-AWS).

    Mirrors ``watch_sources/s3_client.py``: explicit ``endpoint_url`` for S3-compatible
    services, path-style addressing so a bare hostname like ``minio:9000`` works, bounded
    timeouts + retries. Returns the boto3 client; raises ``ValueError`` if no bucket is set.
    """
    bucket = (cfg.get("s3_bucket") or "").strip()
    if not bucket:
        raise ValueError("S3 destination requires a bucket name")

    import boto3
    from botocore.config import Config

    endpoint = (cfg.get("s3_endpoint_url") or "").strip() or None
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=(cfg.get("s3_access_key_id") or "").strip() or None,
        aws_secret_access_key=secret_key or None,
        region_name=(cfg.get("s3_region") or "").strip() or None,
        config=Config(
            connect_timeout=_S3_CONNECT_TIMEOUT,
            read_timeout=_S3_READ_TIMEOUT,
            retries={"max_attempts": 3, "mode": "standard"},
            # Path-style addressing — required for MinIO/Backblaze and bare-host endpoints.
            s3={"addressing_style": "path"} if endpoint else {},
        ),
    )


def _s3_prefix(cfg: dict[str, Any]) -> str:
    """Return the normalized key prefix (no leading slash, trailing slash kept if present)."""
    return (cfg.get("s3_prefix") or "").lstrip("/")


def s3_bucket_status(cfg: dict[str, Any], db: Session | None = None) -> dict[str, Any]:
    """Cheap head_bucket reachability check — graceful, never raises."""
    bucket = (cfg.get("s3_bucket") or "").strip()
    result: dict[str, Any] = {
        "destination_type": DEST_S3,
        "bucket": bucket,
        "prefix": _s3_prefix(cfg),
        "endpoint_url": (cfg.get("s3_endpoint_url") or "").strip(),
        "reachable": False,
        "error": None,
    }
    if not bucket:
        result["error"] = "No S3 bucket configured"
        return result
    try:
        client = _build_s3_client(cfg, _get_s3_secret_key(db))
        client.head_bucket(Bucket=bucket)
        result["reachable"] = True
    except Exception as exc:  # noqa: BLE001 - report any failure, never raise to the caller
        result["error"] = str(exc)
    return result


def _s3_list_keys(client, bucket: str, prefix: str) -> list[str]:
    """List object keys under ``prefix`` that match our backup-filename pattern."""
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if _TS_RE.search(key):
                keys.append(key)
    return keys


def list_backups_s3(cfg: dict[str, Any], db: Session | None = None) -> list[dict[str, Any]]:
    """List backup objects in the configured bucket/prefix, newest first. Graceful on error."""
    bucket = (cfg.get("s3_bucket") or "").strip()
    if not bucket:
        return []
    prefix = _s3_prefix(cfg)
    try:
        client = _build_s3_client(cfg, _get_s3_secret_key(db))
        paginator = client.get_paginator("list_objects_v2")
        out: list[dict[str, Any]] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                ts = _backup_timestamp(key)
                if ts is None:
                    continue
                name = key[len(prefix) :] if key.startswith(prefix) else key
                out.append(
                    {
                        "filename": name,
                        "size_bytes": int(obj.get("Size", 0)),
                        "created_at": ts.isoformat(),
                        "encrypted": key.endswith(_GPG_SUFFIX),
                    }
                )
        out.sort(key=lambda b: b["created_at"], reverse=True)
        return out
    except Exception as exc:  # noqa: BLE001 - graceful; UI shows empty list + status banner
        logger.warning("Could not list S3 backups: %s", exc)
        return []


def prune_backups_s3(cfg: dict[str, Any], client=None, db: Session | None = None) -> list[str]:
    """Delete GFS-excess backup objects in the bucket. Returns deleted object keys."""
    bucket = (cfg.get("s3_bucket") or "").strip()
    if not bucket:
        return []
    prefix = _s3_prefix(cfg)
    if client is None:
        client = _build_s3_client(cfg, _get_s3_secret_key(db))
    keys = _s3_list_keys(client, bucket, prefix)
    to_delete = select_backups_to_delete(
        keys,
        retention_daily=int(cfg["retention_daily"]),
        retention_weekly=int(cfg["retention_weekly"]),
        retention_monthly=int(cfg["retention_monthly"]),
    )
    deleted: list[str] = []
    for key in to_delete:
        try:
            client.delete_object(Bucket=bucket, Key=key)
            deleted.append(key)
        except Exception as exc:  # noqa: BLE001 - one bad delete shouldn't abort the run
            logger.warning("Could not delete old S3 backup %s: %s", key, exc)
    return deleted


def test_s3_connection(
    cfg: dict[str, Any], db: Session | None = None, override_secret: str | None = None
) -> dict[str, Any]:
    """Admin connection test: head_bucket + a cheap list. Returns an ok/error envelope.

    ``override_secret`` lets the API test a just-submitted secret without persisting it
    first; when omitted the stored (encrypted) secret is decrypted and used.
    """
    bucket = (cfg.get("s3_bucket") or "").strip()
    if not bucket:
        return {"ok": False, "error": "No S3 bucket configured", "bucket": bucket}
    secret = override_secret if override_secret else _get_s3_secret_key(db)
    try:
        client = _build_s3_client(cfg, secret)
        client.head_bucket(Bucket=bucket)
        client.list_objects_v2(Bucket=bucket, Prefix=_s3_prefix(cfg), MaxKeys=1)
        return {"ok": True, "error": None, "bucket": bucket}
    except Exception as exc:  # noqa: BLE001 - surface the failure as data, never raise
        return {"ok": False, "error": str(exc), "bucket": bucket}


# =============================================================================
# pg_dump execution
# =============================================================================
#: Custom-format (-Fc) is a load-bearing contract, not an implementation detail:
#: scripts/common.sh's pg_replay_custom_dump / pg_verify_custom_restore can only
#: read this format, and ./opentr.sh restore dispatches on its PGDMP magic bytes.
#: Changing it requires changing those too (issue #600).
PG_DUMP_FORMAT = "custom"


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
    cmd = ["pg_dump", f"--format={PG_DUMP_FORMAT}", "--no-owner", "--no-acl", "--dbname", url]
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

    Dispatches to the local-dir or S3 backend based on ``backup.destination_type``.
    Returns a result dict (``ok``, ``path``/``filename``, ``size_bytes``, ``duration_s``,
    ``error``, ``pruned``). No-ops gracefully (``ok=False``, status ``"no_destination"``)
    when the destination isn't usable — never raises for that case.
    """
    cfg = get_settings(db)
    if cfg["destination_type"] == DEST_S3:
        return _perform_backup_s3(cfg, db)
    return _perform_backup_local(cfg, db)


def _maybe_snapshot_opensearch(cfg: dict[str, Any], ts: str) -> dict[str, Any] | None:
    """Take an OpenSearch snapshot when ``include_opensearch`` is on — never raises.

    Returns the snapshot status sub-object (``{"status": ok|skipped|unsupported|error, ...}``)
    to fold into ``last_result["opensearch"]``, or None when the toggle is off (so the
    result stays unchanged for pg-only runs). Called only AFTER a successful pg dump, and
    its outcome is independent of pg success — a snapshot failure never flips ``ok``.

    ``ts`` is the same ``YYYYMMDD-HHMMSS`` stamp as the pg dump so the snapshot name shares
    the stem (GFS pruning over names lines up across both artifacts).
    """
    if not cfg.get("include_opensearch"):
        return None
    from app.services import opensearch_snapshot

    return opensearch_snapshot.perform_snapshot(cfg, ts=ts)


def _prune_local_safe(destination: str, cfg: dict[str, Any]) -> tuple[list[str], str | None]:
    """Prune with failure isolation: a prune error warns but never fails the run (#244)."""
    try:
        return prune_backups(destination, cfg), None
    except Exception as exc:  # noqa: BLE001 - the dump already succeeded; record + continue
        logger.warning("Backup retention pruning failed (dump itself succeeded): %s", exc)
        return [], str(exc)


def _prune_s3_safe(cfg: dict[str, Any], client: Any) -> tuple[list[str], str | None]:
    """S3 twin of ``_prune_local_safe`` — prune errors never fail a completed upload."""
    try:
        return prune_backups_s3(cfg, client=client), None
    except Exception as exc:  # noqa: BLE001 - the upload already succeeded; record + continue
        logger.warning("S3 backup retention pruning failed (dump itself succeeded): %s", exc)
        return [], str(exc)


def _build_recovery_artifact(cfg: dict[str, Any], dest_dir: Path) -> dict[str, Any]:
    """Write the recovery companion (encrypted) or key README (plaintext) into ``dest_dir``.

    Issue #243: with gpg encryption on, the essential env keys travel beside the dump in
    ``opentranscribe-recovery.env.gpg`` (same passphrase); otherwise a no-secrets
    ``RECOVERY-README.txt`` names what the operator must preserve separately. Returns the
    ``recovery`` status sub-object; never raises and never fails the backup.
    """
    from app.services import backup_recovery

    if cfg["encrypt"]:
        try:
            passphrase = _read_passphrase(cfg["passphrase_file"])
        except (OSError, ValueError) as exc:
            return {"status": backup_recovery.STATUS_ERROR, "error": str(exc)}
        return backup_recovery.write_companion(dest_dir, passphrase)
    return backup_recovery.write_readme(dest_dir)


def _write_recovery_s3(
    cfg: dict[str, Any], scratch: Path, client: Any, bucket: str, prefix: str
) -> dict[str, Any]:
    """Build the recovery artifact in ``scratch`` and upload it beside the dumps (S3)."""
    result = _build_recovery_artifact(cfg, scratch)
    filename = result.get("filename")
    if not filename:
        return result
    local = scratch / filename
    key = f"{prefix}{filename}"
    try:
        client.upload_file(str(local), bucket, key)
        result["path"] = f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001 - companion upload never fails the backup
        logger.warning("Could not upload recovery companion to s3://%s/%s: %s", bucket, key, exc)
        result = {"status": "error", "error": str(exc)}
    finally:
        with contextlib.suppress(OSError):
            local.unlink(missing_ok=True)
    return result


def _perform_backup_local(cfg: dict[str, Any], db: Session | None) -> dict[str, Any]:
    """Local-dir backend: pg_dump straight to the mounted destination, prune in-place."""
    destination = cfg["destination"]
    started = time.monotonic()
    now_iso = datetime.now(UTC).isoformat()

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

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest_path = Path(destination) / f"{_DUMP_PREFIX}{ts}{_DUMP_SUFFIX}"

    try:
        artifact = run_pg_dump(
            dest_path,
            encrypt=bool(cfg["encrypt"]),
            passphrase_file=cfg["passphrase_file"],
        )
        size = artifact.stat().st_size
        pruned, prune_error = _prune_local_safe(destination, cfg)
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
        if prune_error:
            result["prune_error"] = prune_error
        os_result = _maybe_snapshot_opensearch(cfg, ts)
        if os_result is not None:
            result["opensearch"] = os_result
        result["recovery"] = _build_recovery_artifact(cfg, Path(destination))
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


def _scratch_dir(cfg: dict[str, Any]) -> Path:
    """Pick a scratch dir for the temp dump: the mounted dir if writable, else a system temp.

    Reusing the mounted ``/backups`` volume (when present) avoids filling the container's
    ephemeral layer with a large dump; falls back to a fresh ``mkdtemp`` otherwise.
    """
    dest = cfg.get("destination") or ""
    if dest and destination_status(dest)["writable"]:
        return Path(dest)
    return Path(tempfile.mkdtemp(prefix="ot-backup-"))


def _perform_backup_s3(cfg: dict[str, Any], db: Session | None) -> dict[str, Any]:
    """S3 backend: pg_dump to a temp file, upload to the bucket, prune over the listing.

    The temp file (scratch dir or system temp) is always removed afterwards. Bucket
    unreachability / upload failure is recorded as an error result — never raises.
    """
    started = time.monotonic()
    now_iso = datetime.now(UTC).isoformat()

    bucket = (cfg.get("s3_bucket") or "").strip()
    if not bucket:
        msg = "S3 backup destination has no bucket configured — skipping."
        logger.warning(msg)
        result = {"ok": False, "status": "no_destination", "error": msg, "started_at": now_iso}
        _record_result(db, now_iso, result)
        return result

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    scratch = _scratch_dir(cfg)
    using_temp_scratch = scratch != Path(cfg.get("destination") or "")
    tmp_dump = scratch / f"{_DUMP_PREFIX}{ts}{_DUMP_SUFFIX}"
    artifact: Path | None = None

    try:
        artifact = run_pg_dump(
            tmp_dump,
            encrypt=bool(cfg["encrypt"]),
            passphrase_file=cfg["passphrase_file"],
        )
        size = artifact.stat().st_size
        secret = _get_s3_secret_key(db)
        client = _build_s3_client(cfg, secret)
        prefix = _s3_prefix(cfg)
        key = f"{prefix}{artifact.name}"
        logger.info("Uploading backup → s3://%s/%s (%.1f MB)", bucket, key, size / 1_048_576)
        client.upload_file(str(artifact), bucket, key)
        pruned, prune_error = _prune_s3_safe(cfg, client)
        duration = round(time.monotonic() - started, 2)
        result = {
            "ok": True,
            "status": "success",
            "filename": artifact.name,
            "path": f"s3://{bucket}/{key}",
            "size_bytes": size,
            "duration_s": duration,
            "encrypted": artifact.name.endswith(_GPG_SUFFIX),
            "pruned": pruned,
            "started_at": now_iso,
        }
        if prune_error:
            result["prune_error"] = prune_error
        os_result = _maybe_snapshot_opensearch(cfg, ts)
        if os_result is not None:
            result["opensearch"] = os_result
        result["recovery"] = _write_recovery_s3(cfg, scratch, client, bucket, prefix)
        logger.info(
            "S3 backup complete: %s (%.1f MB, %.2fs, pruned %d)",
            key,
            size / 1_048_576,
            duration,
            len(pruned),
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", "replace")[-2000:] if exc.stderr else ""
        duration = round(time.monotonic() - started, 2)
        result = {
            "ok": False,
            "status": "error",
            "error": f"{exc.cmd[0]} failed (exit {exc.returncode}): {stderr}",
            "duration_s": duration,
            "started_at": now_iso,
        }
        logger.error("S3 backup failed: %s", result["error"])
    except Exception as exc:  # noqa: BLE001 - boto3/network/value errors → recorded, never raised
        duration = round(time.monotonic() - started, 2)
        result = {
            "ok": False,
            "status": "error",
            "error": str(exc),
            "duration_s": duration,
            "started_at": now_iso,
        }
        logger.error("S3 backup failed: %s", exc)
    finally:
        # Always remove the local temp artifact (dump and/or gpg envelope).
        _cleanup_partial(tmp_dump)
        if artifact is not None:
            with contextlib.suppress(OSError):
                artifact.unlink(missing_ok=True)
        if using_temp_scratch:
            with contextlib.suppress(OSError):
                scratch.rmdir()

    _record_result(db, now_iso, result)
    return result


def _cleanup_partial(dest_path: Path) -> None:
    """Remove a partial dump (and its gpg envelope) after a failed run."""
    for p in (dest_path, dest_path.with_suffix(dest_path.suffix + ".gpg")):
        with contextlib.suppress(OSError):
            p.unlink(missing_ok=True)


def _record_result(db: Session | None, run_at_iso: str, result: dict[str, Any]) -> None:
    """Persist run bookkeeping and fire admin alerting (own session if needed).

    Beyond ``last_run_at``/``last_result``, this maintains the cumulative
    success/failure counters and last-success timestamp that back the scrape-time
    Prometheus refresh (issue #244), then delegates proactive notifications to
    ``backup_alerts.notify_backup_result`` (never raises).
    """
    ok = bool(result.get("ok"))
    with _session(db) as s:
        sss.set_setting(s, KEY_LAST_RUN_AT, run_at_iso, "Timestamp of the last backup run (UTC)")
        sss.set_setting(s, KEY_LAST_RESULT, json.dumps(result), "Result of the last backup run")
        counter_key = KEY_RUNS_SUCCESS if ok else KEY_RUNS_FAILURE
        current = sss.get_setting_int(s, counter_key, 0)
        sss.set_setting(s, counter_key, current + 1, "Cumulative backup run count (metrics)")
        if ok:
            sss.set_setting(
                s,
                KEY_LAST_SUCCESS_AT,
                run_at_iso,
                "Timestamp of the last successful backup run (UTC)",
            )
        from app.services import backup_alerts

        backup_alerts.notify_backup_result(s, result)


def pg_dump_available() -> bool:
    """Return True if the ``pg_dump`` binary is on PATH (image capability probe)."""
    return shutil.which("pg_dump") is not None
