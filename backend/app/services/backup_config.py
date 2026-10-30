"""Scheduled-backup settings: ``backup.*`` SystemSettings keys, defaults and the settings round-trip.

The S3 secret is AES-256-GCM encrypted at rest and never leaves this module except via
``get_s3_secret_key`` (runtime-only).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core import constants as C  # noqa: N812
from app.services import system_settings_service as sss
from app.services.backup_cron import is_valid_cron
from app.services.scheduled_job_settings import bool_value
from app.services.scheduled_job_settings import int_value
from app.services.scheduled_job_settings import json_value
from app.services.scheduled_job_settings import read_encrypted_secret
from app.services.scheduled_job_settings import session_scope
from app.services.scheduled_job_settings import store_encrypted_secret

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


# =============================================================================
# Settings round-trip
# =============================================================================
def get_settings(db: Session | None = None) -> dict[str, Any]:
    """Return all backup settings as a plain dict (coded defaults for unset keys)."""
    with session_scope(db) as s:
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

        last_result = json_value(vals, KEY_LAST_RESULT)
        return {
            "enabled": bool_value(vals, KEY_ENABLED, C.DEFAULT_BACKUP_ENABLED),
            "schedule": vals.get(KEY_SCHEDULE) or C.DEFAULT_BACKUP_SCHEDULE,
            "destination": vals.get(KEY_DESTINATION) or C.DEFAULT_BACKUP_DESTINATION,
            "retention_daily": int_value(
                vals, KEY_RETENTION_DAILY, C.DEFAULT_BACKUP_RETENTION_DAILY
            ),
            "retention_weekly": int_value(
                vals, KEY_RETENTION_WEEKLY, C.DEFAULT_BACKUP_RETENTION_WEEKLY
            ),
            "retention_monthly": int_value(
                vals, KEY_RETENTION_MONTHLY, C.DEFAULT_BACKUP_RETENTION_MONTHLY
            ),
            "encrypt": bool_value(vals, KEY_ENCRYPT, C.DEFAULT_BACKUP_ENCRYPT),
            "passphrase_file": vals.get(KEY_PASSPHRASE_FILE) or C.DEFAULT_BACKUP_PASSPHRASE_FILE,
            "include_opensearch": bool_value(
                vals, KEY_INCLUDE_OPENSEARCH, C.DEFAULT_BACKUP_INCLUDE_OPENSEARCH
            ),
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
    _set_provided(db, _s3_plain)
    if s3_secret_key is not None:
        _store_s3_secret(db, s3_secret_key)
    return get_settings(db)


def _set_provided(db: Session, fields: tuple[tuple[str | None, str, str], ...]) -> None:
    """Persist each ``(value, key, description)`` whose value was provided (not ``None``)."""
    for value, key, desc in fields:
        if value is not None:
            sss.set_setting(db, key, value, desc)


def _store_s3_secret(db: Session, s3_secret_key: str) -> None:
    """Encrypt + persist the S3 secret (empty string clears it)."""
    store_encrypted_secret(db, KEY_S3_SECRET_KEY, s3_secret_key, "S3 secret access key (encrypted)")


def get_s3_secret_key(db: Session | None) -> str | None:
    """Decrypt and return the stored S3 secret key (runtime-only — never exposed via API)."""
    return read_encrypted_secret(db, KEY_S3_SECRET_KEY)


def update_settings_last_run(db: Session, run_at_iso: str) -> None:
    """Stamp ``backup.last_run_at`` (used by the beat to claim a due window)."""
    sss.set_setting(db, KEY_LAST_RUN_AT, run_at_iso, "Timestamp of the last backup run (UTC)")


def destination_status(destination: str) -> dict[str, Any]:
    """Report whether the destination dir exists and is writable (mount check)."""
    path = Path(destination)
    exists = path.is_dir()
    writable = exists and os.access(path, os.W_OK)
    return {"destination": destination, "exists": exists, "writable": writable, "mounted": writable}
