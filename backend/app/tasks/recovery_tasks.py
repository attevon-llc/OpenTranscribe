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
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.core.enums import DurationSource
from app.core.enums import FileStatus
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.services import storage_recovery_service as recovery

logger = logging.getLogger(__name__)

#: Rows whose stored duration equals the transcript's speech extent to within this
#: tolerance are the signature of issue #969's overwrite. Deliberately NOT the same
#: constant as ``storage_recovery_service.DURATION_MATCH_TOLERANCE_SECONDS`` (2.0s,
#: a YouTube-sidecar fuzzy match) — this one identifies the defect's exact-equality
#: fingerprint, not an approximate match.
_OVERWRITE_SIGNATURE_TOLERANCE_SECONDS = 0.001

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

        # The budget advances on every ATTEMPT, not every success (issue #457).
        #
        # It used to increment only in the success branch below, so `limit` stopped
        # bounding requests exactly when fetches were FAILING — the state most
        # likely to mean this host is already soft-blocked, which is the reason
        # this module rate-limits itself at all. `limit=2` against 3,000 ids ran
        # the full sweep. `summary["fetched"]` is what reports successes.
        new_this_run += 1

        record = recovery.fetch_youtube_metadata(video_id)
        if record is None:
            summary["failed"] += 1
        else:
            cache[video_id] = record
            summary["fetched"] += 1
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


@dataclass
class _DurationCandidate:
    """Plain data snapshot of one row, read once and carried through slow work.

    Never carries an ORM instance across the read -> slow-work -> write phases
    (``backend/app/tasks/CLAUDE.md``'s session-lifetime rule) — the ffprobe
    subprocess/MinIO presign in Tier B must run with NO database session open.
    """

    id: int
    uuid: str
    user_id: int
    stored_duration: float
    storage_path: str
    metadata_important: dict[str, Any] = field(default_factory=dict)


def _tier_a_container_duration(metadata_important: dict[str, Any]) -> float | None:
    """The container's own claim, already sitting in ``metadata_important``.

    Pure function, no I/O — the value ``metadata_extractor.py::_set_duration`` would
    have written had issue #969's group-prefix defect not swallowed it.
    """
    raw = (metadata_important or {}).get("Duration")
    if raw is None:
        return None
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _tier_b_probe_duration(storage_path: str) -> float | None:
    """Re-probe the MinIO object directly with ffprobe. Network I/O — no DB session."""
    from app.services.minio_service import get_internal_presigned_url
    from app.tasks.transcription.metadata_extractor import probe_media_duration

    try:
        url = get_internal_presigned_url(storage_path, expires=3600)
    except Exception as e:
        logger.warning("media_duration_backfill: could not mint presigned URL: %s", e)
        return None
    return probe_media_duration(url)


@celery_app.task(
    name="recovery.media_duration_backfill",
    bind=True,
    queue="cpu",
    priority=CPUPriority.ADMIN_BATCH,
)
def media_duration_backfill(self, user_id: int | None = None, dry_run: bool = True) -> dict:
    """Restore container duration on rows corrupted by issue #969.

    Targets rows with ``duration_source IS NULL`` (written before the fix, provenance
    unknown) AND ``status = COMPLETED`` AND whose stored ``duration`` matches
    ``max(transcript_segment.end_time)`` to within
    ``_OVERWRITE_SIGNATURE_TOLERANCE_SECONDS`` — the exact-equality signature of the
    pre-#969 overwrite (measured 10 of 10 on the live dev DB). A row outside that
    signature was never touched by the bug and is left alone.

    Tier A: ``metadata_important['Duration']`` still holds the container value (a pure
            dict read, no I/O). Sets ``duration_source='container'``.
    Tier B: no usable metadata Duration. Re-probes the MinIO object directly via
            ``probe_media_duration()``. Sets ``duration_source='container'``.
    Neither: leaves ``duration`` untouched and sets ``duration_source='transcript_extent'``
            so the row is honestly labelled and is not re-selected on the next run.

    Safety, mirroring ``recovery.youtube_metadata_backfill``'s precedent plus the
    additional guarantees issue #969 requires:
      - ``dry_run=True`` default. Nothing is written unless explicitly disabled.
      - Idempotent: every branch clears ``duration_source``, so a second run selects
        nothing (``updated=0``).
      - Never LOWERS a duration: a Tier A/B candidate shorter than the stored value is
        an anomaly (measured ``gt_segend=0`` on dev — should never happen), not a
        correction, and is skipped + logged rather than applied.
      - Quarantined / legal-hold rows are excluded entirely (issue #824/#664).
      - The DB session never spans Tier B's network I/O (``app/tasks/CLAUDE.md``'s
        session-lifetime rule): candidates are read as plain data, probed with no
        session open, then written back in a short session.
      - Every updated file is re-dispatched for OpenSearch reindexing — ``duration``
        is an indexed field copied into every chunk's metadata, so a DB-only backfill
        would leave search filters and result cards stale.

    Returns:
        ``{examined, tier_a, tier_b, unrecoverable, updated, skipped_lower, reindexed}``.
    """
    from sqlalchemy import func

    from app.models.media import TranscriptSegment
    from app.services.takedown_service import exclude_quarantined

    summary = {
        "examined": 0,
        "tier_a": 0,
        "tier_b": 0,
        "unrecoverable": 0,
        "updated": 0,
        "skipped_lower": 0,
        "reindexed": 0,
    }

    # --- Phase 1: read candidates as PLAIN DATA. Short session, no slow work inside. ---
    with session_scope() as db:
        segment_max = (
            db.query(
                TranscriptSegment.media_file_id.label("media_file_id"),
                func.max(TranscriptSegment.end_time).label("max_end"),
            )
            .group_by(TranscriptSegment.media_file_id)
            .subquery()
        )
        query = (
            db.query(MediaFile)
            .join(segment_max, segment_max.c.media_file_id == MediaFile.id)
            .filter(
                MediaFile.duration_source.is_(None),
                MediaFile.status == FileStatus.COMPLETED,
                MediaFile.legal_hold.is_(False),
                func.abs(MediaFile.duration - segment_max.c.max_end)
                < _OVERWRITE_SIGNATURE_TOLERANCE_SECONDS,
            )
        )
        query = exclude_quarantined(query)
        if user_id is not None:
            query = query.filter(MediaFile.user_id == user_id)

        candidates = [
            _DurationCandidate(
                id=row.id,
                uuid=str(row.uuid),
                user_id=row.user_id,
                stored_duration=row.duration,
                storage_path=row.storage_path,
                metadata_important=row.metadata_important or {},
            )
            for row in query.all()
        ]

    summary["examined"] = len(candidates)
    if not candidates:
        logger.info("media_duration_backfill: no candidate rows")
        return summary

    # --- Phase 2: slow work (dict reads + Tier B network I/O). NO session open. ---
    resolutions: list[tuple[_DurationCandidate, float | None, str]] = []
    for candidate in candidates:
        tier_a = _tier_a_container_duration(candidate.metadata_important)
        if tier_a is not None:
            summary["tier_a"] += 1
            resolutions.append((candidate, tier_a, DurationSource.CONTAINER.value))
            continue

        tier_b = _tier_b_probe_duration(candidate.storage_path)
        if tier_b is not None:
            summary["tier_b"] += 1
            resolutions.append((candidate, tier_b, DurationSource.CONTAINER.value))
            continue

        summary["unrecoverable"] += 1
        resolutions.append((candidate, None, DurationSource.TRANSCRIPT_EXTENT.value))

    # --- Phase 3: write back. Short session(s), batched. ---
    to_reindex: list[tuple[int, str, int]] = []
    batch_size = 200
    for batch_start in range(0, len(resolutions), batch_size):
        batch = resolutions[batch_start : batch_start + batch_size]
        with session_scope() as db:
            for candidate, new_duration, source in batch:
                if new_duration is not None and new_duration < candidate.stored_duration:
                    summary["skipped_lower"] += 1
                    logger.warning(
                        "media_duration_backfill: file %s probed SHORTER (%.3f) than "
                        "stored (%.3f) -- anomaly, skipping",
                        candidate.id,
                        new_duration,
                        candidate.stored_duration,
                    )
                    continue

                if dry_run:
                    summary["updated"] += 1
                    continue

                mf = db.query(MediaFile).filter(MediaFile.id == candidate.id).first()
                if mf is None:
                    continue
                if new_duration is not None:
                    mf.duration = new_duration
                mf.duration_source = source
                summary["updated"] += 1
                if new_duration is not None:
                    to_reindex.append((candidate.id, candidate.uuid, candidate.user_id))
            if not dry_run:
                db.commit()

    # --- Phase 4: dispatch reindexing. Duration is indexed and copied into every ---
    # chunk's metadata (issue #969's blast-radius survey) -- a DB-only backfill
    # would leave search filters and result cards stale.
    if not dry_run and to_reindex:
        from app.tasks.search_indexing_task import index_transcript_search_task

        for file_id, file_uuid, owner_id in to_reindex:
            index_transcript_search_task.delay(
                file_id=file_id, file_uuid=file_uuid, user_id=owner_id
            )
            summary["reindexed"] += 1

    logger.info("media_duration_backfill complete: %s", summary)
    return summary
