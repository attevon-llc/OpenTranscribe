"""Celery tasks for storage recovery / in-place re-ingestion (Feature B).

Two task families, both CPU-queue:

1. ``youtube_metadata_fetch`` — standalone, rate-limited, metadata-ONLY harvest
   of title/duration for the YouTube ids surfaced by surviving thumbnail
   prefixes. Resumable: progress persists to the MinIO sidecar object
   ``recovery/youtube_metadata.json`` after every fetch, so a re-run picks up
   where it left off. NEVER downloads media (re-downloading is forbidden — IP
   block risk); it only pulls the info JSON via yt-dlp ``skip_download``.

2. ``youtube_metadata_backfill`` — the duration-based matcher. For rows that
   lack a real title, it loads the sidecar and fills title/source_url ONLY when
   a unique duration match exists (see ``match_metadata_by_duration``). It is a
   no-op when the sidecar is empty or no unambiguous match is found — honest by
   construction.

The include-list entry ``"app.tasks.recovery_tasks"`` is already present in
``core/celery.py`` (owned by another agent); this module only registers tasks.
"""

from __future__ import annotations

import logging
import time

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.services import storage_recovery_service as recovery

logger = logging.getLogger(__name__)

# Conservative pacing between YouTube metadata requests (~1 req / 5 s). Metadata
# requests are far lighter than downloads, but we still stay well under any
# bot-detection threshold since re-downloading is forbidden and we cannot afford
# an IP block on the surviving thumbnail ids.
_YT_METADATA_DELAY_SECONDS = 5.0


@celery_app.task(
    name="recovery.youtube_metadata_fetch",
    bind=True,
    queue="cpu",
    priority=CPUPriority.ADMIN_BATCH,
)
def youtube_metadata_fetch(self, user_id: int, limit: int | None = None) -> dict:
    """Harvest title/duration for thumbnail-surfaced YouTube ids (resumable).

    Args:
        user_id: Owner whose ``user_<id>/youtube_*`` thumbnail prefixes are scanned.
        limit: Optional cap on how many NEW ids to fetch this run.

    Returns:
        ``{ids_discovered, fetched, already_present, failed, total_cached}``.
    """
    from app.services.minio_service import MinIOService
    from app.services.minio_service import minio_client

    svc = MinIOService()
    summary = {
        "ids_discovered": 0,
        "fetched": 0,
        "already_present": 0,
        "failed": 0,
        "total_cached": 0,
    }

    video_ids = recovery.discover_youtube_ids(minio_client, user_id)
    summary["ids_discovered"] = len(video_ids)
    logger.info("youtube_metadata_fetch: %d thumbnail ids for user %s", len(video_ids), user_id)

    cache = recovery.load_metadata_sidecar(svc)
    new_this_run = 0

    for video_id in video_ids:
        if video_id in cache and cache[video_id].get("title"):
            summary["already_present"] += 1
            continue
        if limit is not None and new_this_run >= limit:
            logger.info("youtube_metadata_fetch: hit limit %d, stopping", limit)
            break

        record = recovery.fetch_youtube_metadata(video_id)
        if record is None:
            summary["failed"] += 1
        else:
            cache[video_id] = record
            summary["fetched"] += 1
            new_this_run += 1
            # Persist after every successful fetch so the run is resumable.
            recovery.save_metadata_sidecar(svc, cache)

        time.sleep(_YT_METADATA_DELAY_SECONDS)

    # Final save (covers the case where the loop ended without a trailing fetch).
    recovery.save_metadata_sidecar(svc, cache)
    summary["total_cached"] = len(cache)
    logger.info("youtube_metadata_fetch complete: %s", summary)
    return summary


@celery_app.task(
    name="recovery.youtube_metadata_backfill",
    bind=True,
    queue="cpu",
    priority=CPUPriority.ADMIN_BATCH,
)
def youtube_metadata_backfill(self, user_id: int) -> dict:
    """Fill titles/source_url on recovered rows via unique duration matching.

    No-op unless the metadata sidecar already holds records (populated by
    ``youtube_metadata_fetch``) AND a row's ffprobe ``duration`` uniquely
    matches exactly one record. Run AFTER files have been processed (duration
    comes from ffprobe during transcription).

    Returns:
        ``{candidate_rows, matched, updated}``.
    """
    from app.services.minio_service import MinIOService

    svc = MinIOService()
    cache = recovery.load_metadata_sidecar(svc)
    summary = {"candidate_rows": 0, "matched": 0, "updated": 0}

    if not cache:
        logger.info("youtube_metadata_backfill: empty sidecar, nothing to match")
        return summary

    with session_scope() as db:
        # Rows that still have no real title (title == filename placeholder, or a
        # storage-key basename) and have a duration to match on.
        rows = (
            db.query(MediaFile)
            .filter(
                MediaFile.user_id == user_id,
                MediaFile.duration.isnot(None),
            )
            .all()
        )
        # Only consider rows whose title still looks like the placeholder basename.
        candidates = [r for r in rows if _looks_unenriched(r)]
        summary["candidate_rows"] = len(candidates)

        matches = recovery.match_metadata_by_duration(candidates, cache)
        summary["matched"] = len(matches)
        summary["updated"] = recovery.apply_metadata_matches(db, matches)

    logger.info("youtube_metadata_backfill complete: %s", summary)
    return summary


def _looks_unenriched(media_file: MediaFile) -> bool:
    """True when a row's title still looks like a recovery placeholder.

    ``register_object`` sets title = the object's basename (e.g. a uuid + ext),
    so a title equal to the storage basename means metadata was never restored.
    """
    title = (media_file.title or "").strip()
    if not title:
        return True
    basename = str(media_file.storage_path).rsplit("/", 1)[-1]
    return title == basename
