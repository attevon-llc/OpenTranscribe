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

import dataclasses
import logging
import re
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services import system_settings_service
from app.services.minio_service import MinIOService
from app.services.video_processing_service import VideoProcessingService

if TYPE_CHECKING:  # pragma: no cover - typing only
    from minio import Minio

logger = logging.getLogger(__name__)

RETENTION_SETTING_KEY = "cache.derived_retention_days"

#: Set once :func:`reclaim_legacy_audio_cache` has finished a complete pass.
LEGACY_AUDIO_RECLAIM_KEY = "cache.legacy_audio_keys_reclaimed"
#: Keys deleted per pass at most; a larger backlog finishes on later boots.
LEGACY_AUDIO_RECLAIM_LIMIT = 5000
# Pre-hardening extracted audio: ``derived/{base}_audio_{mp3.mp3|wav.wav|original}`` with NO
# leading ``{file_id}_``. A key that starts with digits and an underscore is skipped even when
# it is legacy (a filename like ``2024_q1.mp4``): it cannot be told from a current key, and the
# file's own delete still removes it by name.
_LEGACY_AUDIO_KEY = re.compile(r"^derived/(?![0-9]+_).+_audio_(?:mp3\.mp3|wav\.wav|original)$")


def resolve_retention_days(db: Session) -> int:
    """Resolve the effective retention (DB override, else env baseline). 0 = keep forever."""
    return system_settings_service.get_setting_int(
        db, RETENTION_SETTING_KEY, default=settings.DERIVED_CACHE_RETENTION_DAYS
    )


def _push_lifecycle_rule(days: int) -> None:
    """Push the retention window to object storage. **Takes no DB session.**

    Deliberately separate from the two callers below: constructing the service
    contacts the bucket and applying the rule is a second round trip, and both
    used to run on the caller's session — a request-scoped one in the admin
    endpoint's case. See ``app/tasks/CLAUDE.md``.

    Args:
        days: Retention window in days; 0 removes the rule (keep forever).
    """
    VideoProcessingService(MinIOService()).apply_derived_retention(days)


def apply_retention(db: Session) -> int:
    """(Re)apply the MinIO lifecycle rule from the current setting. Returns the days used."""
    days = resolve_retention_days(db)
    # Release the read transaction before the object-storage round trips: nothing
    # below this line touches Postgres.
    db.commit()
    _push_lifecycle_rule(days)
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
    # ``set_setting`` commits and then ``refresh``es, which opens a fresh
    # transaction; end it before the object-storage work rather than holding it
    # across two network round trips on a request session.
    db.commit()
    _push_lifecycle_rule(int(days))
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


@dataclasses.dataclass(frozen=True)
class LegacyAudioReclaim:
    """Counts from one pass of :func:`reclaim_legacy_audio_cache`.

    Attributes:
        scanned: Keys listed under ``derived/``.
        matched: Legacy audio keys found (versions counted once per key).
        deleted: Objects (or object versions) deleted.
        failed: Deletes storage refused.
        complete: Every legacy key was found and deleted; False when the pass was cut at the
            limit, the listing failed or a delete failed (rerun it).
    """

    scanned: int
    matched: int
    deleted: int
    failed: int
    complete: bool


def is_legacy_audio_key(key: str) -> bool:
    """Whether ``key`` is a pre-hardening extracted-audio key (never an id-prefixed one)."""
    return bool(_LEGACY_AUDIO_KEY.match(key))


def _bucket_is_versioned(client: Minio, bucket: str) -> bool:
    try:
        status = client.get_bucket_versioning(bucket).status
    except Exception as e:  # noqa: BLE001 — unknown is treated as versioned, the safe side
        logger.warning(f"Could not read versioning of {bucket} ({e}); deleting by version.")
        return True
    return status in ("Enabled", "Suspended")


def reclaim_legacy_audio_cache(
    *, client: Minio | None = None, limit: int = LEGACY_AUDIO_RECLAIM_LIMIT
) -> LegacyAudioReclaim:
    """Delete extracted-audio keys written before the file id led the key. Safe to rerun.

    Lists ``derived/`` and deletes only keys matching the legacy audio shape, never a
    burned-in video render and never an id-prefixed key. On a versioned bucket every version
    and delete marker of a matched key is removed, so the bytes are really gone. At most
    ``limit`` keys per pass. Never raises.
    """
    from minio.deleteobjects import DeleteObject

    service = VideoProcessingService(MinIOService())
    storage = client or service.minio_service.client
    bucket = service.cache_bucket
    versioned = _bucket_is_versioned(storage, bucket)
    scanned = 0
    targets: list[DeleteObject] = []
    keys: set[str] = set()
    truncated = False
    try:
        for obj in storage.list_objects(
            bucket,
            prefix=service.DERIVED_CACHE_PREFIX,
            recursive=True,
            include_version=versioned,
        ):
            scanned += 1
            name = obj.object_name or ""
            if not is_legacy_audio_key(name):
                continue
            if name not in keys and len(keys) >= limit:
                truncated = True
                break
            keys.add(name)
            targets.append(DeleteObject(name, obj.version_id) if versioned else DeleteObject(name))
    except Exception as e:  # noqa: BLE001 — reported as an incomplete pass, never raised
        logger.warning(f"Could not list legacy audio cache keys in {bucket}: {e}")
        return LegacyAudioReclaim(scanned, len(keys), 0, 0, False)

    failed = 0
    if targets:
        try:
            for err in storage.remove_objects(bucket, iter(targets)):
                failed += 1
                logger.warning(f"Could not delete legacy audio key {err.name}: {err.code}")
        except Exception as e:  # noqa: BLE001 — reported as an incomplete pass, never raised
            logger.warning(f"Legacy audio cache reclaim error in {bucket}: {e}")
            return LegacyAudioReclaim(scanned, len(keys), 0, len(targets), False)
    result = LegacyAudioReclaim(
        scanned, len(keys), len(targets) - failed, failed, not truncated and failed == 0
    )
    logger.info(
        f"Legacy audio cache reclaim in {bucket}: scanned {result.scanned}, matched "
        f"{result.matched}, deleted {result.deleted}, failed {result.failed}"
        + ("" if result.complete else " (incomplete; runs again on the next start)")
    )
    return result


def reclaim_legacy_audio_cache_once(db: Session) -> LegacyAudioReclaim | None:
    """Run :func:`reclaim_legacy_audio_cache` at startup until one pass completes.

    The ``LEGACY_AUDIO_RECLAIM_KEY`` flag is set only after a COMPLETE pass, so a pass cut at
    the limit or by a storage error runs again on the next start. The read transaction is
    ended before the storage calls.

    Returns:
        The pass result, or None when a complete pass already ran.
    """
    if system_settings_service.get_setting_bool(db, LEGACY_AUDIO_RECLAIM_KEY, False):
        return None
    db.commit()
    result = reclaim_legacy_audio_cache()
    if result.complete:
        system_settings_service.set_setting(
            db,
            LEGACY_AUDIO_RECLAIM_KEY,
            True,
            "One-time removal of extracted-audio cache keys without a file id",
        )
    return result
