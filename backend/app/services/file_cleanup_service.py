"""
File cleanup service for recovering stuck files and maintaining system health.
"""

import contextlib
import logging
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.utils.task_utils import check_for_stuck_files
from app.utils.task_utils import recover_stuck_file

logger = logging.getLogger(__name__)


class FileCleanupService:
    """Service for automated file cleanup and recovery operations."""

    def __init__(self):
        self.stuck_threshold_hours = 2
        self.orphan_threshold_hours = 12
        self.max_recovery_attempts = 3

    def run_cleanup_cycle(self) -> dict[str, Any]:
        """
        Run a complete cleanup cycle.

        Returns:
            Dictionary with cleanup results and statistics
        """
        results: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "stuck_files_checked": 0,
            "files_recovered": 0,
            "files_marked_orphaned": 0,
            "cleanup_errors": [],
            "recommendations": [],
        }

        try:
            with session_scope() as db:
                # Step 1: Check for stuck files
                stuck_file_ids = check_for_stuck_files(db, self.stuck_threshold_hours)
                results["stuck_files_checked"] = len(stuck_file_ids)

                if stuck_file_ids:
                    logger.info(f"Found {len(stuck_file_ids)} stuck files for cleanup")

                    # Step 2: Attempt recovery
                    for file_id in stuck_file_ids:
                        try:
                            success = self._attempt_file_recovery(db, file_id)
                            if success:
                                results["files_recovered"] += 1
                            else:
                                results["files_marked_orphaned"] += 1
                        except Exception as e:
                            error_msg = f"Error processing stuck file {file_id}: {str(e)}"
                            logger.error(error_msg)
                            results["cleanup_errors"].append(error_msg)

                # Step 3: Handle very old orphaned files
                old_orphaned_count = self._handle_old_orphaned_files(db)
                if old_orphaned_count > 0:
                    results["recommendations"].append(
                        f"Found {old_orphaned_count} old orphaned files that may need admin attention"
                    )

                # Step 4: Generate health recommendations
                health_recommendations = self._generate_health_recommendations(db)
                results["recommendations"].extend(health_recommendations)

        except Exception as e:
            error_msg = f"Critical error in cleanup cycle: {str(e)}"
            logger.error(error_msg)
            results["cleanup_errors"].append(error_msg)

        logger.info(f"Cleanup cycle completed: {results}")
        return results

    def _attempt_file_recovery(self, db: Session, file_id: int) -> bool:
        """
        Attempt to recover a single stuck file.

        Args:
            db: Database session
            file_id: ID of the file to recover

        Returns:
            True if recovery was successful, False if marked as orphaned
        """
        media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
        if not media_file:
            return False

        # Check if we've already tried recovery too many times
        if (media_file.recovery_attempts or 0) >= self.max_recovery_attempts:
            logger.warning(
                f"File {file_id} has exceeded max recovery attempts ({self.max_recovery_attempts})"
            )
            # Mark as permanently orphaned
            media_file.status = FileStatus.ORPHANED  # type: ignore[assignment]
            media_file.force_delete_eligible = True  # type: ignore[assignment]
            db.commit()
            return False

        # Attempt recovery
        return recover_stuck_file(db, file_id)

    def _handle_old_orphaned_files(self, db: Session) -> int:
        """
        Handle files that have been orphaned for a long time.

        Args:
            db: Database session

        Returns:
            Number of old orphaned files found
        """
        threshold_time = datetime.now(UTC) - timedelta(hours=self.orphan_threshold_hours)

        old_orphaned_files = (
            db.query(MediaFile)
            .filter(
                MediaFile.status == FileStatus.ORPHANED,
                MediaFile.last_recovery_attempt < threshold_time,
            )
            .all()
        )

        for file in old_orphaned_files:
            # Mark as eligible for force deletion
            file.force_delete_eligible = True  # type: ignore[assignment]
            logger.warning(
                f"File {file.id} has been orphaned for over {self.orphan_threshold_hours} hours"
            )

        if old_orphaned_files:
            db.commit()

        return len(old_orphaned_files)

    def _generate_health_recommendations(self, db: Session) -> list[str]:
        """
        Generate system health recommendations based on file states.

        Args:
            db: Database session

        Returns:
            List of recommendation strings
        """
        recommendations = []

        # Count files by status in a single query (replaces N separate queries)
        from sqlalchemy import func

        status_rows = (
            db.query(MediaFile.status, func.count().label("cnt")).group_by(MediaFile.status).all()
        )
        status_counts = {
            r.status.value if hasattr(r.status, "value") else r.status: r.cnt for r in status_rows
        }

        # Check for concerning patterns
        error_rate = status_counts.get("error", 0) / max(sum(status_counts.values()), 1)
        if error_rate > 0.1:  # More than 10% error rate
            recommendations.append(
                f"High error rate detected: {error_rate:.1%} of files are in error state. "
                "Consider investigating processing pipeline health."
            )

        orphaned_count = status_counts.get("orphaned", 0)
        if orphaned_count > 0:
            recommendations.append(
                f"Found {orphaned_count} orphaned file(s). "
                "Consider manual review or cleanup of these files."
            )

        processing_count = status_counts.get("processing", 0)
        if processing_count > 50:  # Arbitrary threshold
            recommendations.append(
                f"Large number of files currently processing ({processing_count}). "
                "Monitor worker capacity and queue health."
            )

        return recommendations

    def force_cleanup_orphaned_files(self, db: Session, dry_run: bool = False) -> dict[str, Any]:
        """
        Force cleanup of orphaned files (admin operation).

        Destroys each eligible file through :func:`purge_media_file`, the canonical
        destroy shared with the interactive, bulk and retention paths. It used to
        go through the API-layer ``delete_media_file``, which took a *file_uuid*
        and was handed ``str(file.id)`` — an integer — so ``UUID("123")`` raised
        for every file and the whole pass reported ``successfully_deleted: 0``
        while ``run_deep_cleanup`` logged it as a success. Nothing was ever
        deleted. That call also required a ``current_user`` for its permission
        lookup, which was faked with a transient ``User(role="admin")`` added to
        the caller's live session.

        Args:
            db: Database session
            dry_run: If True, only preview what would be cleaned up

        Returns:
            Dictionary with cleanup results
        """
        results: dict[str, Any] = {
            "dry_run": dry_run,
            "eligible_for_deletion": 0,
            "successfully_deleted": 0,
            "deletion_errors": [],
            "files_processed": [],
        }

        # Find files eligible for force deletion
        eligible_files = (
            db.query(MediaFile)
            .filter(
                MediaFile.force_delete_eligible.is_(True),
                MediaFile.status.in_([FileStatus.ORPHANED, FileStatus.ERROR]),
            )
            .all()
        )

        results["eligible_for_deletion"] = len(eligible_files)

        if not dry_run:
            from app.utils.task_utils import cancel_active_task

            for file in eligible_files:
                try:
                    # These files are orphaned/errored, but an abandoned task row can
                    # still be attached; the interactive force-delete cancels it too.
                    if file.active_task_id:
                        with contextlib.suppress(Exception):
                            cancel_active_task(db, int(file.id))

                    result = purge_media_file(db, file)
                    if not result["deleted"]:
                        raise RuntimeError(result.get("error") or "purge_media_file failed")
                    results["successfully_deleted"] += 1
                    results["files_processed"].append(
                        {"id": int(file.id), "filename": file.filename, "status": "deleted"}
                    )
                except Exception as e:
                    error_msg = f"Failed to delete file {file.id}: {str(e)}"
                    results["deletion_errors"].append(error_msg)
                    results["files_processed"].append(
                        {
                            "id": int(file.id),
                            "filename": file.filename,
                            "status": "error",
                            "error": str(e),
                        }
                    )
        else:
            # Dry run - just record what would be deleted
            for file in eligible_files:
                results["files_processed"].append(
                    {
                        "id": int(file.id),
                        "filename": file.filename,
                        "status": "would_delete",
                        "current_status": file.status,
                    }
                )

        return results

    def get_cleanup_statistics(self, db: Session) -> dict[str, Any]:
        """
        Get current cleanup statistics and system health metrics.

        Args:
            db: Database session

        Returns:
            Dictionary with statistics
        """
        stats: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "file_counts_by_status": {},
            "stuck_files_detected": 0,
            "files_eligible_for_cleanup": 0,
            "avg_processing_time_hours": 0,
            "health_score": "unknown",
        }

        # Count files by status in a single query (replaces N separate queries)
        from sqlalchemy import extract
        from sqlalchemy import func

        status_rows = (
            db.query(MediaFile.status, func.count().label("cnt")).group_by(MediaFile.status).all()
        )
        for r in status_rows:
            key = r.status.value if hasattr(r.status, "value") else r.status
            stats["file_counts_by_status"][key] = r.cnt

        # Count stuck files
        stuck_files = check_for_stuck_files(db, self.stuck_threshold_hours)
        stats["stuck_files_detected"] = len(stuck_files)

        # Count files eligible for cleanup
        eligible_count = (
            db.query(MediaFile).filter(MediaFile.force_delete_eligible.is_(True)).count()
        )
        stats["files_eligible_for_cleanup"] = eligible_count

        # Calculate average processing time via SQL aggregation (avoids full ORM hydration)
        avg_row = (
            db.query(
                func.avg(
                    extract("epoch", MediaFile.completed_at - MediaFile.task_started_at) / 3600.0
                ).label("avg_hours")
            )
            .filter(
                MediaFile.status == FileStatus.COMPLETED,
                MediaFile.task_started_at.isnot(None),
                MediaFile.completed_at.isnot(None),
            )
            .one()
        )
        if avg_row.avg_hours is not None:
            stats["avg_processing_time_hours"] = float(avg_row.avg_hours)

        # Calculate health score
        total_files = sum(stats["file_counts_by_status"].values())
        if total_files > 0:
            error_rate = stats["file_counts_by_status"].get("error", 0) / total_files
            orphaned_rate = stats["file_counts_by_status"].get("orphaned", 0) / total_files

            if error_rate < 0.05 and orphaned_rate < 0.02:
                stats["health_score"] = "healthy"
            elif error_rate < 0.1 and orphaned_rate < 0.05:
                stats["health_score"] = "fair"
            else:
                stats["health_score"] = "poor"
        else:
            stats["health_score"] = "empty"

        return stats


# Global service instance
cleanup_service = FileCleanupService()


def _opensearch_client():
    """Return the OpenSearch client, or ``None`` when it is disabled/unbuilt."""
    from app.services.opensearch_service import opensearch_client

    return opensearch_client


def _count_surviving(index: str, query: dict[str, Any]) -> int:
    """Count documents still matching ``query`` in ``index`` after a delete.

    An **absent index answers 0** — the cluster confirmed the documents are
    gone. Anything else (no client, connection refused, auth failure) **raises**,
    because "I could not ask" is not "nothing is there"; the caller records that
    as a residual error rather than silently treating it as a clean sweep.

    Args:
        index: Index to count in.
        query: OpenSearch query body's ``query`` clause.

    Returns:
        The number of documents that survived the deletion attempt.

    Raises:
        Exception: The cluster could not be reached or could not answer.
    """
    from opensearchpy.exceptions import NotFoundError

    client = _opensearch_client()
    if client is None:
        raise RuntimeError("OpenSearch client unavailable")
    try:
        if not client.indices.exists(index=index):
            return 0
        return int(client.count(index=index, body={"query": query})["count"])
    except NotFoundError:
        return 0


def _cleanup_opensearch_for_file(target: dict[str, Any], file_uuid: str) -> list[dict[str, Any]]:
    """Remove all OpenSearch documents associated with a media file.

    Covers: speaker embeddings (v3 + v4 + alias), the transcript document, the
    ``transcript_chunks`` RAG index, and transcript summaries.

    Takes **plain data, never an ORM instance**: it used to read ``file.speakers``
    and ``file.id`` off a live row, which lazy-loads — i.e. reopens a transaction
    — in the middle of half a dozen OpenSearch round trips. The speaker UUIDs are
    now enumerated in :func:`_load_purge_plan`'s short read session instead.

    Best-effort means **one failing store never stops the others** — it does NOT
    mean failures are invisible. Every step that could not prove its documents
    are gone is returned to the caller. This used to wrap each step in
    ``contextlib.suppress(Exception)``, so an OpenSearch outage during a GDPR
    Art. 17 erasure destroyed the DB rows (and the account) while leaving the
    verbatim transcript and its RAG chunks indexed and searchable — reported to
    the caller, the API and the audit log as a completed erasure, with the row
    that would identify what to re-delete already gone.

    Two of the steps go through helpers that swallow their own errors and return
    a value that cannot distinguish "absent" from "failed"
    (``remove_speaker_embedding`` returns ``False`` for both;
    ``delete_transcript_chunks`` returns ``0`` for both). For those, the
    surviving-document count — not the helper's return value — is the evidence.

    Args:
        target: The purge plan from :func:`_load_purge_plan` — ``file_id``,
            ``speaker_uuids`` and an optional ``speaker_read_error``.
        file_uuid: The file's UUID, used as the transcript/chunk document key.

    Returns:
        One ``{"stage", "file_uuid", "error"}`` dict per store whose documents
        may have survived. Empty means every store confirmed them gone.
    """
    residual: list[dict[str, Any]] = []

    def _fail(stage: str, err: object) -> None:
        logger.warning(f"OpenSearch erasure incomplete for file {file_uuid} at '{stage}': {err}")
        residual.append({"stage": stage, "file_uuid": file_uuid, "error": str(err)})

    # A read failure in the DB phase is still a miss: the embeddings it would
    # have named are unaccounted for, so it must surface as a residual here.
    read_error = target.get("speaker_read_error")
    if read_error:
        _fail("speakers", f"could not enumerate the file's speakers: {read_error}")

    _erase_speaker_docs(list(target.get("speaker_uuids") or []), _fail)
    _erase_transcript_doc(file_uuid, _fail)
    _erase_transcript_chunks(file_uuid, _fail)
    _erase_summary_docs(int(target["file_id"]), _fail)
    return residual


def _erase_speaker_docs(speaker_uuids: list[str], fail: Callable[[str, object], None]) -> None:
    """Delete the file's speaker embeddings (biometric data) and verify.

    ``remove_speaker_embedding`` already sweeps v3 + v4 + the alias, so there is
    exactly one deletion path — but it swallows its own errors and returns
    ``False`` for "absent" and "failed" alike, so the surviving-document count is
    what actually proves the embeddings are gone.
    """
    if not speaker_uuids:
        return

    for speaker_uuid in speaker_uuids:
        try:
            from app.services.opensearch_service import remove_speaker_embedding

            remove_speaker_embedding(speaker_uuid)
        except Exception as e:  # noqa: BLE001 — it swallows its own; this is belt-and-braces
            fail("speakers", e)

    from app.core.constants import get_speaker_index
    from app.core.constants import get_speaker_index_v3
    from app.core.constants import get_speaker_index_v4

    for idx in {get_speaker_index(), get_speaker_index_v3(), get_speaker_index_v4()}:
        try:
            left = _count_surviving(idx, {"ids": {"values": speaker_uuids}})
            if left:
                fail("speakers", f"{left} voiceprint doc(s) survive in {idx}")
        except Exception as e:  # noqa: BLE001 — unverifiable == not proven gone
            fail("speakers", f"could not verify {idx}: {e}")


def _erase_transcript_doc(file_uuid: str, fail: Callable[[str, object], None]) -> None:
    """Delete the transcript document — the verbatim text of the recording."""
    try:
        from opensearchpy.exceptions import NotFoundError

        from app.services.opensearch_service import settings as os_settings

        client = _opensearch_client()
        if client is None:
            raise RuntimeError("OpenSearch client unavailable")
        # NotFoundError is the cluster ANSWERING that the document is not there,
        # which is the desired end state — not a failure to remove it.
        with contextlib.suppress(NotFoundError):
            client.delete(index=os_settings.OPENSEARCH_TRANSCRIPT_INDEX, id=file_uuid)
    except Exception as e:  # noqa: BLE001
        fail("transcript", e)


def _erase_transcript_chunks(file_uuid: str, fail: Callable[[str, object], None]) -> None:
    """Delete the file's chunks from the RAG index, and verify none survive.

    ``delete_transcript_chunks`` returns 0 for "no chunks", "index absent" AND
    "the delete failed", so only the count is evidence. This index stores
    transcript text UNREDACTED, so a survivor here is the raw content.
    """
    try:
        from app.services.search.indexing_service import TranscriptIndexingService

        TranscriptIndexingService().delete_transcript_chunks(file_uuid)
    except Exception as e:  # noqa: BLE001 — it swallows its own; this is belt-and-braces
        fail("transcript_chunks", e)

    try:
        from app.core.config import settings as app_settings

        left = _count_surviving(
            app_settings.OPENSEARCH_CHUNKS_INDEX, {"term": {"file_uuid": file_uuid}}
        )
        if left:
            fail("transcript_chunks", f"{left} chunk(s) survive")
    except Exception as e:  # noqa: BLE001 — unverifiable == not proven gone
        fail("transcript_chunks", f"could not verify: {e}")


def _erase_summary_docs(file_id: int, fail: Callable[[str, object], None]) -> None:
    """Delete the file's LLM-written summaries — prose about the recording."""
    try:
        from app.services.opensearch_service import settings as os_settings

        client = _opensearch_client()
        if client is None:
            raise RuntimeError("OpenSearch client unavailable")
        summary_index = os_settings.OPENSEARCH_SUMMARY_INDEX
        if not client.indices.exists(index=summary_index):
            return
        resp = client.delete_by_query(
            index=summary_index,
            body={"query": {"term": {"file_id": file_id}}},
            refresh=True,
        )
        # delete_by_query reports per-document failures in the BODY rather than
        # raising, so a partial sweep looks like a success without this.
        failures = (resp or {}).get("failures") or []
        if failures:
            fail("transcript_summaries", f"delete_by_query reported {len(failures)} failure(s)")
    except Exception as e:  # noqa: BLE001
        fail("transcript_summaries", e)


def delete_file_storage_artifacts(file_id: int, artifacts: dict[str, Any]) -> bool:
    """Delete every object-storage artifact for a media file.

    Covers the original, its thumbnail, and the regenerable derived cache
    (subtitle-embedded videos + extracted audio under ``processed-videos/derived/``).
    Single source of truth shared by the interactive delete endpoint and the
    retention/auto-delete path so neither can orphan storage. Best-effort per
    artifact — a failure on one never blocks the rest.

    **Takes no DB session.** It used to take the caller's, purely to hand it to
    ``VideoProcessingService.clear_derived_cache`` (which takes no session),
    which put up to seven MinIO round trips inside the caller's transaction.
    :func:`_load_purge_plan` reads the filename instead.

    A missing object is NOT a failure: S3/MinIO deletes are idempotent and do
    not raise for an absent key, so anything that does raise is a real one.

    Args:
        file_id: Internal media file id — the derived-cache keys are keyed on it.
        artifacts: Plain values read in the caller's DB phase —
            ``filename``, ``storage_path`` and ``thumbnail_path``.

    Returns:
        True when every artifact this file has was deleted or was already
        absent; False when at least one may still be in object storage.
        ``purge_media_file`` turns a False into a residual error so a GDPR
        erasure that left the media itself behind cannot audit as complete —
        which is why this reports "all clear" rather than the narrower "the
        original was deleted" it used to. The per-artifact detail is logged.
    """
    from app.services.minio_service import delete_file

    all_deleted = True
    for path_key in ("storage_path", "thumbnail_path"):
        path = artifacts.get(path_key)
        if not path:
            continue
        try:
            delete_file(str(path))
            logger.info(f"Deleted MinIO object {path}")
        except Exception as minio_err:  # noqa: BLE001 — one artifact never blocks the rest
            all_deleted = False
            logger.warning(f"Could not delete MinIO object {path}: {minio_err}")

    # Regenerable derived cache — duplicates that must not outlive the original.
    # "Regenerable" describes how they were made, not what they hold: these are
    # subtitle-burned video and extracted audio, i.e. the media itself.
    filename = artifacts.get("filename")
    if filename:
        try:
            from app.services.minio_service import MinIOService
            from app.services.video_processing_service import VideoProcessingService

            VideoProcessingService(MinIOService()).clear_derived_cache(int(file_id), str(filename))
        except Exception as cache_err:  # noqa: BLE001
            all_deleted = False
            logger.warning(f"Derived-cache cleanup failed for file {file_id}: {cache_err}")

    return all_deleted


def _cleanup_empty_clusters(db: Session, owner_id: int) -> None:
    """Remove non-promoted speaker clusters left empty after a file's speakers are deleted.

    CASCADE removes cluster-membership rows but not the (now empty) cluster itself, so
    we check actual remaining members via a NOT EXISTS subquery. Best-effort.
    """
    try:
        from sqlalchemy import exists

        from app.models.media import SpeakerCluster
        from app.models.media import SpeakerClusterMember

        has_members = (
            exists()
            .where(SpeakerClusterMember.cluster_id == SpeakerCluster.id)
            .correlate(SpeakerCluster)
        )
        empty_clusters = (
            db.query(SpeakerCluster)
            .filter(
                SpeakerCluster.user_id == owner_id,
                ~has_members,
                SpeakerCluster.promoted_to_profile_id.is_(None),
            )
            .all()
        )
        if not empty_clusters:
            return
        for cluster in empty_clusters:
            with contextlib.suppress(Exception):
                from app.services.opensearch_service import delete_cluster_embedding

                delete_cluster_embedding(str(cluster.uuid))
            db.delete(cluster)
        db.commit()
        logger.info(f"Cleaned up {len(empty_clusters)} empty cluster(s) after file deletion")
    except Exception as e:
        db.rollback()
        logger.warning(f"Failed to clean up empty clusters: {e}")


def _load_purge_plan(db: Session, file: MediaFile) -> dict[str, Any]:
    """Read everything the external destroy needs, then hand back PLAIN DATA.

    No ORM instance leaves this function. ``file.speakers`` in particular is a
    lazy relationship: reading it from the OpenSearch phase reopened a
    transaction in the middle of half a dozen cluster round trips, which is the
    exact back door the three-phase rule exists to close.

    Args:
        db: The caller's session, used only for the reads below.
        file: The media file about to be destroyed.

    Returns:
        ``file_id``, ``file_uuid``, ``owner_id``, ``filename``,
        ``storage_path``, ``thumbnail_path``, ``speaker_uuids`` and — when the
        speaker enumeration itself failed — ``speaker_read_error``.
    """
    plan: dict[str, Any] = {
        "file_id": int(file.id),
        "file_uuid": str(file.uuid),
        "owner_id": int(file.user_id),
        "filename": str(file.filename) if file.filename else None,
        "storage_path": str(file.storage_path) if file.storage_path else None,
        "thumbnail_path": str(file.thumbnail_path) if file.thumbnail_path else None,
        "speaker_uuids": [],
        "speaker_read_error": None,
    }
    try:
        plan["speaker_uuids"] = [str(speaker.uuid) for speaker in list(file.speakers)]
    except Exception as e:  # noqa: BLE001 — a failed load is itself a miss
        plan["speaker_read_error"] = str(e)
    return plan


def _purge_external_copies(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Destroy every copy of a file that lives outside Postgres.

    Object storage first, then OpenSearch. **Takes no session and opens none**,
    so the caller's transaction is closed for the whole of it.

    Args:
        plan: The plain-data plan from :func:`_load_purge_plan`.

    Returns:
        One ``{"stage", "file_uuid", "error"}`` dict per store whose documents
        or objects may have survived. Empty means every store confirmed them gone.
    """
    file_uuid = plan["file_uuid"]
    residual: list[dict[str, Any]] = []

    storage_ok = delete_file_storage_artifacts(
        plan["file_id"],
        {
            "filename": plan["filename"],
            "storage_path": plan["storage_path"],
            "thumbnail_path": plan["thumbnail_path"],
        },
    )
    if not storage_ok:
        residual.append(
            {
                "stage": "storage",
                "file_uuid": file_uuid,
                "error": "one or more object-storage artifacts could not be deleted",
            }
        )

    residual.extend(_cleanup_opensearch_for_file(plan, file_uuid) or [])
    return residual


def purge_media_file(db: Session, file: MediaFile) -> dict:
    """Canonical destroy for a MediaFile and ALL associated data.

    Single source of truth shared by every delete path — interactive single/force
    delete, bulk delete, N-day retention, and orphan cleanup — so a change here applies
    everywhere and no path can drift or leak. Steps (each best-effort, DB delete is the
    commit point):

    1. Object storage: original + thumbnail + regenerable derived cache.
    2. OpenSearch: speaker embeddings (v3+v4), transcript doc, transcript chunks, summaries.
    3. Database row (CASCADE removes child rows).
    4. Redis caches for the owner.
    5. Empty non-promoted speaker clusters orphaned by the CASCADE.

    SpeakerProfile records and their profile embeddings are intentionally preserved.

    Returns ``{"deleted": bool, "file_uuid": str, "error": str | None,
    "residual_errors": list[dict]}``. Never raises.

    ``deleted`` reports the **database row** only. ``residual_errors`` is
    non-empty when a copy of the file's data may still exist in object storage
    or OpenSearch, and steps 1 and 2 are best-effort in the sense that one
    failing store does not stop the others — NOT in the sense that their
    failures are invisible. A caller that treats ``deleted: True`` as "every
    copy is gone" is wrong: the GDPR erasure path must surface
    ``residual_errors`` so an incomplete erasure audits as PARTIAL.

    **Session lifetime.** Three phases: read the plan, destroy the external
    copies with **no transaction open**, then delete the row. Steps 1 and 2 used
    to run on the caller's live session — a request session in the interactive
    and GDPR paths — so a MinIO outage or a slow OpenSearch cluster held
    ``ACCESS SHARE`` on ``media_file`` for the whole destroy. The ``db.commit()``
    between phases 1 and 2 is what actually ends that transaction; it is safe
    because this function already commits (the row delete below), so no caller
    can be relying on it to leave work pending.
    """
    file_uuid = str(file.uuid)
    residual: list[dict[str, Any]] = []
    try:
        # Phase 1 — read (DB session open, Postgres only).
        plan = _load_purge_plan(db, file)
        owner_id = plan["owner_id"]
        db.commit()

        # Phase 2 — object storage + OpenSearch. NO transaction is held here.
        residual.extend(_purge_external_copies(plan))

        # Phase 3 — write.
        db.delete(file)
        db.commit()
        logger.info(f"purge_media_file: deleted file {file_uuid} from database")

        try:
            from app.services.redis_cache_service import redis_cache

            redis_cache.invalidate_all_for_user(owner_id)
        except Exception as cache_err:
            logger.debug(f"purge_media_file: cache invalidation failed (non-critical): {cache_err}")

        _cleanup_empty_clusters(db, owner_id)

        return {
            "deleted": True,
            "file_uuid": file_uuid,
            "error": None,
            "residual_errors": residual,
        }

    except Exception as e:
        db.rollback()
        logger.error(f"purge_media_file: failed to delete file {file_uuid}: {e}")
        return {
            "deleted": False,
            "file_uuid": file_uuid,
            "error": str(e),
            "residual_errors": residual,
        }


def auto_delete_media_file(db: Session, file: MediaFile) -> dict:
    """Backwards-compatible alias for the canonical :func:`purge_media_file`."""
    return purge_media_file(db, file)
