"""Derived-asset cache management.

The ``processed-videos`` bucket holds *regenerable* duplicates of original media —
subtitle-embedded videos and extracted audio under the ``derived/`` prefix. Originals
are the source of truth; these are recreated on demand in seconds. To bound disk/cloud
usage across laptops, homelabs, servers, and cloud, the cache auto-expires via a MinIO
lifecycle rule (server-side, no app cron) whose retention is admin-configurable.

Retention resolution (DB-over-env, so the admin UI changes apply with no redeploy):
    DB SystemSettings 'cache.derived_retention_days'  →  env DERIVED_CACHE_RETENTION_DAYS

This module is the single entry point for resolving, applying, inspecting, and clearing
the derived cache so endpoints, startup, and the worker share one implementation.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services import system_settings_service
from app.services.minio_service import MinIOService
from app.services.video_processing_service import VideoProcessingService

logger = logging.getLogger(__name__)

RETENTION_SETTING_KEY = "cache.derived_retention_days"


def resolve_retention_days(db: Session) -> int:
    """Resolve the effective retention (DB override, else env baseline). 0 = keep forever."""
    return system_settings_service.get_setting_int(
        db, RETENTION_SETTING_KEY, default=settings.DERIVED_CACHE_RETENTION_DAYS
    )


def apply_retention(db: Session) -> int:
    """(Re)apply the MinIO lifecycle rule from the current setting. Returns the days used."""
    days = resolve_retention_days(db)
    VideoProcessingService(MinIOService()).apply_derived_retention(days)
    return days


def set_retention_days(db: Session, days: int) -> int:
    """Persist a new retention value and apply it live (no redeploy). Returns the value."""
    system_settings_service.set_setting(
        db,
        RETENTION_SETTING_KEY,
        int(days),
        description="Days before regenerable derived media (processed-videos/derived/) "
        "auto-expires. 0 = keep forever.",
    )
    VideoProcessingService(MinIOService()).apply_derived_retention(int(days))
    return int(days)


def get_cache_stats() -> dict:
    """Return derived-cache usage: object count, total bytes, and the lifecycle prefix."""
    service = VideoProcessingService(MinIOService())
    count, total = service.minio_service.prefix_stats(
        service.cache_bucket, service.DERIVED_CACHE_PREFIX
    )
    return {
        "bucket": service.cache_bucket,
        "prefix": service.DERIVED_CACHE_PREFIX,
        "object_count": count,
        "total_bytes": total,
    }


def clear_derived_cache() -> int:
    """Delete every derived asset now. Returns the number of objects removed.

    Safe: these are regenerated on the next download request. Originals are untouched.
    """
    service = VideoProcessingService(MinIOService())
    deleted = service.minio_service.delete_prefix(
        service.cache_bucket, service.DERIVED_CACHE_PREFIX
    )
    logger.info(f"Cleared {deleted} derived cache object(s)")
    return deleted


def reclaim_legacy_derived_cache() -> int:
    """One-time upgrade reclaim of pre-prefix derived assets.

    Older versions wrote derived assets (``{base}_with_speakers.mp4``,
    ``{base}_audio_*``) at the ``processed-videos`` bucket root. They are now keyed
    under ``derived/`` and covered by the lifecycle rule — but the old root-level
    objects are orphaned duplicates the rule never sees. This deletes every object at
    the bucket root (no ``/`` in the key), leaving the managed ``derived/`` and
    ``bulk/`` prefixes intact. Safe: these are regenerable. Returns the count removed.
    """
    from minio.deleteobjects import DeleteObject

    service = VideoProcessingService(MinIOService())
    client = service.minio_service.client
    bucket = service.cache_bucket
    try:
        legacy = [
            obj.object_name
            for obj in client.list_objects(bucket, recursive=True)
            if "/" not in (obj.object_name or "")
        ]
    except Exception as e:
        logger.warning(f"Could not list legacy cache objects in {bucket}: {e}")
        return 0
    if not legacy:
        return 0
    failed = 0
    try:
        for err in client.remove_objects(bucket, (DeleteObject(n) for n in legacy)):
            failed += 1
            logger.warning(f"Failed to reclaim legacy object {err.name}: {err.code} {err.message}")
    except Exception as e:
        logger.warning(f"Legacy cache reclaim error in {bucket}: {e}")
        return 0
    deleted = len(legacy) - failed
    logger.info(f"Reclaimed {deleted} legacy root-level derived cache object(s) from {bucket}")
    return deleted
