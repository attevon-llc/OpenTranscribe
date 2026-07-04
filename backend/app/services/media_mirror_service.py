"""Media mirror settings + run bookkeeping (issue #242).

Configuration layer for the incremental MinIO media mirror: DB-backed
``SystemSettings`` under ``backup.mirror_*`` with coded ``DEFAULT_BACKUP_MIRROR_*``
defaults (``core/constants.py``) — same conventions as the scheduled database
backup (``backup_service``), whose cron/due-check/destination helpers are reused.

The sync mechanics live in ``media_mirror_engine`` — this module owns settings
round-trips, the write-only encrypted S3 secret, destination status probes, and
run-result persistence (last result + cumulative counters that back the
scrape-time Prometheus refresh in ``core/backup_metrics``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core import constants as C  # noqa: N812
from app.services import backup_service
from app.services import system_settings_service as sss
from app.services.backup_service import _session
from app.utils.encryption import decrypt_api_key
from app.utils.encryption import encrypt_api_key

logger = logging.getLogger(__name__)

# --- SystemSettings keys ------------------------------------------------------
KEY_ENABLED = "backup.mirror_enabled"
KEY_SCHEDULE = "backup.mirror_schedule"
KEY_DESTINATION_TYPE = "backup.mirror_destination_type"
KEY_DESTINATION = "backup.mirror_destination"
KEY_THROTTLE_MS = "backup.mirror_throttle_ms"
KEY_S3_ENDPOINT_URL = "backup.mirror_s3_endpoint_url"
KEY_S3_REGION = "backup.mirror_s3_region"
KEY_S3_BUCKET = "backup.mirror_s3_bucket"
KEY_S3_PREFIX = "backup.mirror_s3_prefix"
KEY_S3_ACCESS_KEY_ID = "backup.mirror_s3_access_key_id"
KEY_S3_SECRET_KEY = "backup.mirror_s3_secret_key"  # noqa: S105  # nosec B105 - settings key name, not a secret
KEY_LAST_RUN_AT = "backup.mirror_last_run_at"
KEY_LAST_RESULT = "backup.mirror_last_result"
# Cumulative run counts + last-success timestamp back the scrape-time Prometheus
# refresh (core/backup_metrics.update_media_mirror_metrics) — the #244 pattern.
KEY_RUNS_SUCCESS = "backup.mirror_runs_success_total"
KEY_RUNS_FAILURE = "backup.mirror_runs_failure_total"
KEY_LAST_SUCCESS_AT = "backup.mirror_last_success_at"

DEST_LOCAL = backup_service.DEST_LOCAL
DEST_S3 = backup_service.DEST_S3

# Redis lock identity (runs must never overlap; a full first mirror can take hours).
MIRROR_LOCK_KEY = "media_mirror_run"
MIRROR_LOCK_TIMEOUT = 12 * 3600  # expiry safety net if a worker dies mid-run


# =============================================================================
# Settings round-trip
# =============================================================================
def get_settings(db: Session | None = None) -> dict[str, Any]:
    """Return all media-mirror settings as a plain dict (coded defaults for unset keys)."""
    with _session(db) as s:
        vals = sss.get_settings_map(
            s,
            [
                KEY_ENABLED,
                KEY_SCHEDULE,
                KEY_DESTINATION_TYPE,
                KEY_DESTINATION,
                KEY_THROTTLE_MS,
                KEY_S3_ENDPOINT_URL,
                KEY_S3_REGION,
                KEY_S3_BUCKET,
                KEY_S3_PREFIX,
                KEY_S3_ACCESS_KEY_ID,
                KEY_S3_SECRET_KEY,
                KEY_LAST_RUN_AT,
                KEY_LAST_RESULT,
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
            "enabled": _b(KEY_ENABLED, C.DEFAULT_BACKUP_MIRROR_ENABLED),
            "schedule": vals.get(KEY_SCHEDULE) or C.DEFAULT_BACKUP_MIRROR_SCHEDULE,
            "destination_type": vals.get(KEY_DESTINATION_TYPE)
            or C.DEFAULT_BACKUP_MIRROR_DESTINATION_TYPE,
            "destination": vals.get(KEY_DESTINATION) or C.DEFAULT_BACKUP_MIRROR_DESTINATION,
            "throttle_ms": _i(KEY_THROTTLE_MS, C.DEFAULT_BACKUP_MIRROR_THROTTLE_MS),
            "s3_endpoint_url": vals.get(KEY_S3_ENDPOINT_URL)
            or C.DEFAULT_BACKUP_MIRROR_S3_ENDPOINT_URL,
            "s3_region": vals.get(KEY_S3_REGION) or C.DEFAULT_BACKUP_MIRROR_S3_REGION,
            "s3_bucket": vals.get(KEY_S3_BUCKET) or C.DEFAULT_BACKUP_MIRROR_S3_BUCKET,
            "s3_prefix": vals.get(KEY_S3_PREFIX) or C.DEFAULT_BACKUP_MIRROR_S3_PREFIX,
            "s3_access_key_id": vals.get(KEY_S3_ACCESS_KEY_ID)
            or C.DEFAULT_BACKUP_MIRROR_S3_ACCESS_KEY_ID,
            # NEVER expose the secret — only whether one is configured.
            "s3_secret_key_set": bool(vals.get(KEY_S3_SECRET_KEY)),
            "last_run_at": vals.get(KEY_LAST_RUN_AT),
            "last_result": last_result,
        }


def update_settings(
    db: Session,
    *,
    enabled: bool | None = None,
    schedule: str | None = None,
    destination_type: str | None = None,
    destination: str | None = None,
    throttle_ms: int | None = None,
    s3_endpoint_url: str | None = None,
    s3_region: str | None = None,
    s3_bucket: str | None = None,
    s3_prefix: str | None = None,
    s3_access_key_id: str | None = None,
    s3_secret_key: str | None = None,
) -> dict[str, Any]:
    """Persist any provided mirror settings; return the full current set.

    ``s3_secret_key`` is AES-256-GCM encrypted before storage and is never echoed
    back (``get_settings`` exposes only ``s3_secret_key_set``).
    """
    if enabled is not None:
        sss.set_setting(db, KEY_ENABLED, enabled, "Media mirror master toggle")
    if schedule is not None:
        if not backup_service.is_valid_cron(schedule):
            raise ValueError(f"Invalid cron schedule: {schedule!r}")
        sss.set_setting(db, KEY_SCHEDULE, schedule, "Media mirror cron schedule (5-field, UTC)")
    if destination_type is not None:
        if destination_type not in (DEST_LOCAL, DEST_S3):
            raise ValueError(f"Invalid destination_type: {destination_type!r}")
        sss.set_setting(
            db, KEY_DESTINATION_TYPE, destination_type, "Media mirror destination type (local|s3)"
        )
    if destination is not None:
        sss.set_setting(
            db, KEY_DESTINATION, destination, "Media mirror destination directory (mounted)"
        )
    if throttle_ms is not None:
        if int(throttle_ms) < 0:
            raise ValueError("throttle_ms must be >= 0")
        sss.set_setting(
            db, KEY_THROTTLE_MS, int(throttle_ms), "Media mirror inter-object sleep (ms)"
        )
    _s3_plain = (
        (s3_endpoint_url, KEY_S3_ENDPOINT_URL, "Mirror S3 endpoint URL (empty = real AWS S3)"),
        (s3_region, KEY_S3_REGION, "Mirror S3 region"),
        (s3_bucket, KEY_S3_BUCKET, "Mirror S3 destination bucket"),
        (s3_prefix, KEY_S3_PREFIX, "Mirror S3 key prefix within the bucket"),
        (s3_access_key_id, KEY_S3_ACCESS_KEY_ID, "Mirror S3 access key id"),
    )
    for value, key, desc in _s3_plain:
        if value is not None:
            sss.set_setting(db, key, value, desc)
    if s3_secret_key is not None:
        _store_s3_secret(db, s3_secret_key)
    return get_settings(db)


def _store_s3_secret(db: Session, s3_secret_key: str) -> None:
    """Encrypt + persist the mirror S3 secret (empty string clears it)."""
    if not s3_secret_key:
        sss.set_setting(db, KEY_S3_SECRET_KEY, "", "Mirror S3 secret access key (encrypted)")
        return
    encrypted = encrypt_api_key(s3_secret_key)
    if not encrypted:
        raise ValueError("Failed to encrypt S3 secret key")
    sss.set_setting(db, KEY_S3_SECRET_KEY, encrypted, "Mirror S3 secret access key (encrypted)")


def get_s3_secret_key(db: Session | None) -> str | None:
    """Decrypt and return the stored mirror S3 secret (runtime-only — never via API)."""
    with _session(db) as s:
        raw = sss.get_setting(s, KEY_S3_SECRET_KEY)
    if not raw:
        return None
    return decrypt_api_key(raw)


def update_settings_last_run(db: Session, run_at_iso: str) -> None:
    """Stamp ``backup.mirror_last_run_at`` (the beat claims a due window with this)."""
    sss.set_setting(db, KEY_LAST_RUN_AT, run_at_iso, "Timestamp of the last media mirror run (UTC)")


# =============================================================================
# Destination status probes (for the admin panel — never raise)
# =============================================================================
def _s3_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """Map mirror settings onto the key names ``backup_service._build_s3_client`` expects."""
    return {
        "s3_endpoint_url": cfg.get("s3_endpoint_url"),
        "s3_region": cfg.get("s3_region"),
        "s3_bucket": cfg.get("s3_bucket"),
        "s3_prefix": cfg.get("s3_prefix"),
        "s3_access_key_id": cfg.get("s3_access_key_id"),
    }


def build_s3_client(cfg: dict[str, Any], db: Session | None = None, secret: str | None = None):
    """Boto3 client for the mirror destination bucket (reuses the backup builder)."""
    return backup_service._build_s3_client(
        _s3_cfg(cfg), secret if secret is not None else get_s3_secret_key(db)
    )


def s3_bucket_status(cfg: dict[str, Any], db: Session | None = None) -> dict[str, Any]:
    """Cheap head_bucket reachability check — graceful, never raises."""
    bucket = (cfg.get("s3_bucket") or "").strip()
    result: dict[str, Any] = {
        "bucket": bucket,
        "prefix": (cfg.get("s3_prefix") or "").lstrip("/"),
        "endpoint_url": (cfg.get("s3_endpoint_url") or "").strip(),
        "reachable": False,
        "error": None,
    }
    if not bucket:
        result["error"] = "No S3 bucket configured"
        return result
    try:
        client = build_s3_client(cfg, db)
        client.head_bucket(Bucket=bucket)
        result["reachable"] = True
    except Exception as exc:  # noqa: BLE001 - report any failure, never raise to the caller
        result["error"] = str(exc)
    return result


def test_s3_connection(
    cfg: dict[str, Any], db: Session | None = None, override_secret: str | None = None
) -> dict[str, Any]:
    """Admin connection test: head_bucket + a cheap list. Returns an ok/error envelope."""
    bucket = (cfg.get("s3_bucket") or "").strip()
    if not bucket:
        return {"ok": False, "error": "No S3 bucket configured", "bucket": bucket}
    try:
        client = build_s3_client(cfg, db, secret=override_secret)
        client.head_bucket(Bucket=bucket)
        client.list_objects_v2(
            Bucket=bucket, Prefix=(cfg.get("s3_prefix") or "").lstrip("/"), MaxKeys=1
        )
        return {"ok": True, "error": None, "bucket": bucket}
    except Exception as exc:  # noqa: BLE001 - surface the failure as data, never raise
        return {"ok": False, "error": str(exc), "bucket": bucket}


# =============================================================================
# Run-result persistence (the #244 metrics/alerting bookkeeping)
# =============================================================================
def record_result(db: Session | None, run_at_iso: str, result: dict[str, Any]) -> None:
    """Persist run bookkeeping and fire admin alerting (own session if needed).

    Maintains ``mirror_last_run_at``/``mirror_last_result`` plus the cumulative
    success/failure counters and last-success timestamp that back the scrape-time
    Prometheus refresh, then delegates notifications to
    ``backup_alerts.notify_mirror_result`` (failure → admin WS; success silent).
    """
    ok = bool(result.get("ok"))
    with _session(db) as s:
        sss.set_setting(
            s, KEY_LAST_RUN_AT, run_at_iso, "Timestamp of the last media mirror run (UTC)"
        )
        sss.set_setting(
            s, KEY_LAST_RESULT, json.dumps(result), "Result of the last media mirror run"
        )
        counter_key = KEY_RUNS_SUCCESS if ok else KEY_RUNS_FAILURE
        current = sss.get_setting_int(s, counter_key, 0)
        sss.set_setting(s, counter_key, current + 1, "Cumulative media mirror run count (metrics)")
        if ok:
            sss.set_setting(
                s,
                KEY_LAST_SUCCESS_AT,
                run_at_iso,
                "Timestamp of the last successful media mirror run (UTC)",
            )
        from app.services import backup_alerts

        backup_alerts.notify_mirror_result(s, result)
