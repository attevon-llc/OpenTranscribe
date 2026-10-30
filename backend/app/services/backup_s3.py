"""S3-compatible backup destination (boto3 — AWS S3 / MinIO / Backblaze / Wasabi / etc.)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services.backup_config import DEST_S3
from app.services.backup_config import get_s3_secret_key
from app.services.backup_retention import _GPG_SUFFIX
from app.services.backup_retention import _TS_RE
from app.services.backup_retention import _backup_timestamp
from app.services.backup_retention import select_backups_to_delete

logger = logging.getLogger(__name__)


_S3_CONNECT_TIMEOUT = 10
_S3_READ_TIMEOUT = 60


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
        client = _build_s3_client(cfg, get_s3_secret_key(db))
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
        client = _build_s3_client(cfg, get_s3_secret_key(db))
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
        client = _build_s3_client(cfg, get_s3_secret_key(db))
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
    secret = override_secret if override_secret else get_s3_secret_key(db)
    try:
        client = _build_s3_client(cfg, secret)
        client.head_bucket(Bucket=bucket)
        client.list_objects_v2(Bucket=bucket, Prefix=_s3_prefix(cfg), MaxKeys=1)
        return {"ok": True, "error": None, "bucket": bucket}
    except Exception as exc:  # noqa: BLE001 - surface the failure as data, never raise
        return {"ok": False, "error": str(exc), "bucket": bucket}
