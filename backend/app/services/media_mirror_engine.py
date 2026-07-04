"""Incremental MinIO media mirror — sync engine (issue #242).

Copies the irreplaceable originals (the media bucket, minus regenerable prefixes)
to a second location: a mounted folder or an S3-compatible bucket. Additive-only
by design:

- **NEVER deletes** on the destination — the destination interface exposes only
  ``lookup`` and ``copy`` (fat-finger / ransomware protection; a source-side mass
  delete can't propagate). Tested as an explicit invariant.
- **Incremental**: objects are compared by size, plus ETag when both sides carry a
  simple (non-multipart) MD5 — up-to-date objects are skipped, so the steady-state
  nightly delta for write-once media is tiny.
- **Memory-safe**: the source listing is a streaming iterator (minio-py pages
  internally); copies stream without buffering whole files in memory.
- **Contained**: one failed object never aborts the run; failures are counted and
  a bounded sample of errors is recorded.
- ``max_objects`` bounds how many source objects one run examines (tests use this
  to stay off the full 484 GB bucket); ``throttle_ms`` adds an inter-object sleep.

Settings/result persistence live in ``media_mirror_service``; scheduling in
``tasks/backup_tasks.py`` (Redis-locked, download queue — never the GPU queue).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Protocol

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Object-key prefixes holding regenerable data — never mirrored.
# - temp/   : preprocessed-audio staging (minio_service.TEMP_PREPROCESS_PREFIX).
# - derived/, bulk/ : defensive — subtitle-embedded videos / export zips live in the
#   separate ``processed-videos`` cache bucket (which is not mirrored at all); listed
#   here so a key-layout change can never silently leak regenerable cache into the
#   mirror. Originals (user_*/file_*/...), thumbnails, and avatars ARE mirrored.
EXCLUDED_PREFIXES: tuple[str, ...] = ("temp/", "derived/", "bulk/")

# Cap on per-object error messages carried in the result (the counts stay exact).
MAX_RECORDED_ERRORS = 10


@dataclass(frozen=True)
class SourceObject:
    """One source-bucket object: key + the size/etag used for the compare."""

    key: str
    size: int
    etag: str | None = None


class MirrorDestination(Protocol):
    """Write-only view of a mirror destination.

    The never-delete invariant is structural: the interface has **no delete** —
    only ``lookup`` (for the size/etag compare) and ``copy``.
    """

    def lookup(self, key: str) -> tuple[int, str | None] | None:
        """Return (size, etag-or-None) for ``key`` at the destination, or None."""
        ...  # pragma: no cover - protocol

    def copy(self, obj: SourceObject) -> None:
        """Copy one source object to the destination (streaming)."""
        ...  # pragma: no cover - protocol


# =============================================================================
# Pure planning helpers (unit-tested)
# =============================================================================
def is_excluded(key: str) -> bool:
    """True when ``key`` sits under a regenerable prefix that must not be mirrored."""
    return key.startswith(EXCLUDED_PREFIXES)


def _norm_etag(etag: str | None) -> str | None:
    return etag.strip('"').lower() if etag else None


def should_copy(src: SourceObject, dst: tuple[int, str | None] | None) -> bool:
    """Decide whether ``src`` needs copying given the destination's (size, etag).

    Missing or size-mismatched objects always copy. ETags only participate when
    both sides carry a simple single-part MD5 (multipart ETags depend on chunking
    and differ across systems for identical bytes — comparing them would re-copy
    everything forever).
    """
    if dst is None:
        return True
    dst_size, dst_etag = dst
    if src.size != dst_size:
        return True
    s, d = _norm_etag(src.etag), _norm_etag(dst_etag)
    if s and d and "-" not in s and "-" not in d:
        return s != d
    return False


def _safe_relative_key(key: str) -> str:
    """Validate an object key for filesystem use; raises ValueError on traversal."""
    if key.startswith("/") or ".." in Path(key).parts:
        raise ValueError(f"Refusing unsafe object key: {key!r}")
    return key


# =============================================================================
# Destinations
# =============================================================================
class FolderDestination:
    """Mounted-folder destination — mirrors the bucket's key layout as files."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def lookup(self, key: str) -> tuple[int, str | None] | None:
        path = self.root / _safe_relative_key(key)
        if not path.is_file():
            return None
        return (path.stat().st_size, None)

    def copy(self, obj: SourceObject) -> None:
        from app.core.config import settings
        from app.services.minio_service import minio_client

        dest = self.root / _safe_relative_key(obj.key)
        # fget_object creates parent dirs and downloads via a temp part-file +
        # rename, so a killed run never leaves a truncated file at the final path.
        minio_client.fget_object(settings.MEDIA_BUCKET_NAME, obj.key, str(dest))


class S3Destination:
    """S3-compatible destination — streams source objects into ``bucket/prefix``.

    The destination inventory is loaded once via paged ``list_objects_v2`` (one
    LIST per 1000 keys beats a HEAD per object; the key→(size, etag) map is a few
    MB even for tens of thousands of objects).
    """

    def __init__(self, client: Any, bucket: str, prefix: str):
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.lstrip("/")
        self._inventory: dict[str, tuple[int, str | None]] | None = None

    def _load_inventory(self) -> dict[str, tuple[int, str | None]]:
        inventory: dict[str, tuple[int, str | None]] = {}
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rel = key[len(self.prefix) :] if key.startswith(self.prefix) else key
                inventory[rel] = (int(obj.get("Size", 0)), obj.get("ETag"))
        return inventory

    def lookup(self, key: str) -> tuple[int, str | None] | None:
        if self._inventory is None:
            self._inventory = self._load_inventory()
        return self._inventory.get(key)

    def copy(self, obj: SourceObject) -> None:
        from app.core.config import settings
        from app.services.minio_service import minio_client

        response = minio_client.get_object(settings.MEDIA_BUCKET_NAME, obj.key)
        try:
            # upload_fileobj streams (multipart for large objects) — no full-file buffer.
            self.client.upload_fileobj(response, self.bucket, f"{self.prefix}{obj.key}")
        finally:
            response.close()
            response.release_conn()


# =============================================================================
# Source listing + core loop
# =============================================================================
def iter_source_objects(bucket: str) -> Iterator[SourceObject]:
    """Stream all objects in the source bucket (minio-py pages internally)."""
    from app.services.minio_service import minio_client

    for obj in minio_client.list_objects(bucket, recursive=True):
        if obj.is_dir:
            continue
        yield SourceObject(key=obj.object_name, size=obj.size or 0, etag=obj.etag)


def execute_mirror(
    source: Iterable[SourceObject],
    destination: MirrorDestination,
    *,
    throttle_ms: int = 0,
    max_objects: int | None = None,
) -> dict[str, Any]:
    """Run one incremental mirror pass; returns the counter dict.

    Per-object failures are contained (counted + sampled into ``errors``); the
    loop never deletes anything. ``max_objects`` bounds how many source objects
    are examined (None = all).
    """
    scanned = excluded = copied = skipped = failed = 0
    bytes_copied = 0
    errors: list[str] = []

    for obj in source:
        if max_objects is not None and scanned >= max_objects:
            break
        scanned += 1
        if is_excluded(obj.key):
            excluded += 1
            continue
        try:
            if should_copy(obj, destination.lookup(obj.key)):
                destination.copy(obj)
                copied += 1
                bytes_copied += obj.size
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001 - one failed object never aborts the run
            failed += 1
            if len(errors) < MAX_RECORDED_ERRORS:
                errors.append(f"{obj.key}: {exc}")
            logger.warning("Media mirror: failed to copy %s: %s", obj.key, exc)
        if throttle_ms > 0:
            time.sleep(throttle_ms / 1000.0)

    return {
        "objects_scanned": scanned,
        "objects_excluded": excluded,
        "objects_copied": copied,
        "objects_skipped": skipped,
        "objects_failed": failed,
        "bytes_copied": bytes_copied,
        "errors": errors,
    }


# =============================================================================
# Orchestration (settings → destination → run → record)
# =============================================================================
def _build_destination(cfg: dict[str, Any], db: Session | None) -> MirrorDestination:
    """Build the configured destination; raises ValueError when it isn't usable."""
    from app.services import backup_service
    from app.services import media_mirror_service as mm

    if cfg["destination_type"] == mm.DEST_S3:
        bucket = (cfg.get("s3_bucket") or "").strip()
        if not bucket:
            raise ValueError("Media mirror S3 destination has no bucket configured")
        return S3Destination(mm.build_s3_client(cfg, db), bucket, cfg.get("s3_prefix") or "")
    destination = cfg["destination"]
    if not backup_service.destination_status(destination)["writable"]:
        raise ValueError(
            f"Media mirror destination {destination!r} is not a writable mount — mount a "
            "host path via docker-compose.backup.yml (BACKUP_MIRROR_HOST_PATH)."
        )
    return FolderDestination(destination)


def perform_mirror(db: Session | None = None, max_objects: int | None = None) -> dict[str, Any]:
    """Execute one mirror run end-to-end and record the result in SystemSettings.

    Returns a result dict (``ok``, counters, ``duration_s``, ``error``). An unusable
    destination no-ops gracefully (``status="no_destination"``); a run-level failure
    (listing/credentials) is recorded as ``status="error"`` — never raises.
    """
    from app.core.config import settings
    from app.services import media_mirror_service as mm

    cfg = mm.get_settings(db)
    started = time.monotonic()
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        destination = _build_destination(cfg, db)
    except ValueError as exc:
        logger.warning("Media mirror skipped: %s", exc)
        result = {"ok": False, "status": "no_destination", "error": str(exc), "started_at": now_iso}
        mm.record_result(db, now_iso, result)
        return result

    try:
        counters = execute_mirror(
            iter_source_objects(settings.MEDIA_BUCKET_NAME),
            destination,
            throttle_ms=int(cfg["throttle_ms"]),
            max_objects=max_objects,
        )
        result = {
            "ok": True,
            "status": "success",
            **counters,
            "duration_s": round(time.monotonic() - started, 2),
            "started_at": now_iso,
        }
        logger.info(
            "Media mirror complete: %d scanned, %d copied (%.1f MB), %d skipped, %d failed "
            "in %.1fs",
            counters["objects_scanned"],
            counters["objects_copied"],
            counters["bytes_copied"] / 1_048_576,
            counters["objects_skipped"],
            counters["objects_failed"],
            result["duration_s"],
        )
    except Exception as exc:  # noqa: BLE001 - listing/network errors → recorded, never raised
        result = {
            "ok": False,
            "status": "error",
            "error": str(exc),
            "duration_s": round(time.monotonic() - started, 2),
            "started_at": now_iso,
        }
        logger.error("Media mirror failed: %s", exc)

    mm.record_result(db, now_iso, result)
    return result
