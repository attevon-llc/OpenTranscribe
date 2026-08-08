"""Celery task for search indexing after transcription completion."""

import logging
import time
from typing import Any

from app.core.celery import celery_app
from app.core.constants import EmbeddingPriority
from app.core.constants import UtilityPriority
from app.utils import benchmark_timing

logger = logging.getLogger(__name__)


def extract_file_index_metadata(db: Any, media_file: Any, file_id: int) -> dict[str, Any]:
    """Collect the per-file metadata a search document carries.

    Must run while the caller's session is still open — every field below is
    read off attached ORM state.

    Tags and collections are read through the **association rows** that
    ``MediaFile`` actually declares (``file_tags`` / ``collection_memberships``).
    There is no ``.tags`` or ``.collections`` relationship: reading those names
    yields nothing at all, which is how every transcript came to be indexed with
    an empty tags array and no collection ids. ``reindex_task`` has always read
    the association rows — this is the same extraction, so a live index and a
    rebuilt one now agree.

    Args:
        db: Open database session owning ``media_file``.
        media_file: The attached :class:`~app.models.media.MediaFile`.
        file_id: Its integer primary key.

    Returns:
        Dict with ``title``, ``tag_names``, ``upload_time``, ``language``,
        ``content_type``, ``duration``, ``file_size``, ``collection_ids``,
        ``accessible_user_ids`` and ``organization_id``.
    """
    from app.services.permission_service import PermissionService

    tag_names: list[str] = [ft.tag.name for ft in media_file.file_tags if ft.tag]
    collection_ids: list[int] = [int(cm.collection_id) for cm in media_file.collection_memberships]

    upload_time = (
        (media_file.creation_date or media_file.upload_time).isoformat()
        if media_file.creation_date or media_file.upload_time
        else None
    )

    return {
        "title": media_file.title or media_file.filename or f"File {file_id}",
        "tag_names": tag_names,
        "upload_time": upload_time,
        "language": media_file.language or "en",
        "content_type": media_file.content_type or "",
        "duration": media_file.duration,
        "file_size": media_file.file_size,
        "collection_ids": collection_ids,
        "accessible_user_ids": PermissionService.get_users_with_file_access(db, file_id),
        "organization_id": media_file.organization_id,  # tenant scope (None = personal)
    }


@celery_app.task(
    bind=True,
    name="index_transcript_search",
    priority=EmbeddingPriority.PIPELINE_CRITICAL,
    max_retries=3,
    default_retry_delay=30,
)
def index_transcript_search_task(  # noqa: C901
    self,
    file_id: int,
    file_uuid: str,
    user_id: int,
    pipeline_task_id: str | None = None,
) -> dict[str, Any]:
    """Index a transcript in OpenSearch as a tracked Celery task.

    Creates a Task database record for visibility in the file status modal.
    Loads transcript segments from PostgreSQL and indexes them with embeddings.

    Args:
        file_id: Media file integer ID.
        file_uuid: Media file UUID string.
        user_id: Owner user ID.

    Returns:
        Dict with indexing stats and timing.
    """
    from sqlalchemy.orm import joinedload

    from app.db.session_utils import get_refreshed_object
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment
    from app.services.search.indexing_service import TranscriptIndexingService
    from app.utils.task_utils import create_task_record
    from app.utils.task_utils import update_task_status

    task_id = self.request.id
    logger.info(f"Search indexing task {task_id} started for file {file_uuid}")

    # Create a tracked Task record
    with session_scope() as db:
        create_task_record(db, task_id, user_id, file_id, "search_indexing")
        update_task_status(db, task_id, "in_progress", progress=0.1)

    total_start = time.time()
    benchmark_timing.mark(pipeline_task_id, "search_index_chunks_start")

    try:
        # Single DB session: fetch segments (with speaker joinedload),
        # media_file, tags/collections, and the access-list in one sweep.
        # Previously the task opened three separate sessions and issued
        # three segment-range queries for the same ``media_file_id``; that
        # doubled row-read pressure on Postgres for every completed file.
        # Phase 2 PR #10: consolidate to one session, one segment fetch.
        from app.services.opensearch_service import index_transcript
        from app.tasks.transcription.storage import generate_full_transcript
        from app.tasks.transcription.storage import get_unique_speaker_names

        with session_scope() as db:
            update_task_status(db, task_id, "in_progress", progress=0.2)

            media_file = get_refreshed_object(db, MediaFile, file_id)
            if not media_file:
                raise ValueError(f"Media file {file_id} not found")

            segments = (
                db.query(TranscriptSegment)
                .options(joinedload(TranscriptSegment.speaker))
                .filter(TranscriptSegment.media_file_id == file_id)
                .order_by(TranscriptSegment.start_time)
                .all()
            )

            if not segments:
                logger.warning(f"No segments found for file {file_uuid}, skipping indexing")
                update_task_status(db, task_id, "completed", progress=1.0, completed=True)
                return {"status": "skipped", "reason": "no_segments"}

            # Shared per-segment derived values consumed by both indexes.
            # Chunk-level index wants a non-null "Unknown" fallback so the
            # BM25 ``speaker`` field is always filterable; the full-doc
            # index wants the raw name or None to preserve
            # speaker-transition structure in the document body.
            seg_dicts_full: list[dict[str, str | None]] = []
            segment_dicts: list[dict[str, float | str]] = []
            for seg in segments:
                speaker_obj = seg.speaker
                raw_name = speaker_obj.name if speaker_obj else None
                display_name = (
                    speaker_obj.display_name
                    if speaker_obj and speaker_obj.display_name
                    else raw_name
                )
                chunk_speaker = display_name or "Unknown"

                seg_dicts_full.append({"text": seg.text, "speaker": raw_name})
                segment_dicts.append(
                    {
                        "start": float(seg.start_time),
                        "end": float(seg.end_time),
                        "text": seg.text or "",
                        "speaker": chunk_speaker,
                    }
                )

            # Extract per-file metadata from the MediaFile while the session
            # is still open — relationship access (tags, collections)
            # requires attached ORM state.
            meta = extract_file_index_metadata(db, media_file, file_id)
            title = meta["title"]
            speaker_names = list(
                {str(s["speaker"]) for s in segment_dicts if s["speaker"] != "Unknown"}
            )
            update_task_status(db, task_id, "in_progress", progress=0.4)

            doc_title = title  # captured before session close

        # Phase 2 PR #5: full-document transcript index runs here on the
        # embedding worker (moved off the CPU postprocess critical path).
        # Best-effort — a failure here must not block the chunk-level index.
        try:
            full_transcript = generate_full_transcript(seg_dicts_full)
            doc_speaker_names = get_unique_speaker_names(seg_dicts_full)
            index_transcript(
                file_id, file_uuid, user_id, full_transcript, doc_speaker_names, doc_title
            )
        except Exception as full_doc_err:
            logger.warning(f"Full-document transcript indexing failed (non-fatal): {full_doc_err}")

        indexing_service = TranscriptIndexingService()
        result = indexing_service.index_transcript_chunks(
            file_id=file_id,
            file_uuid=file_uuid,
            user_id=user_id,
            segments=segment_dicts,
            title=title,
            speakers=speaker_names,
            tags=meta["tag_names"],
            upload_time=meta["upload_time"],
            language=meta["language"],
            content_type=meta["content_type"],
            duration=meta["duration"],
            file_size=meta["file_size"],
            collection_ids=meta["collection_ids"],
            accessible_user_ids=meta["accessible_user_ids"],
            organization_id=meta["organization_id"],
        )

        total_ms = round((time.time() - total_start) * 1000)

        # Build timing result
        if isinstance(result, dict):
            timing = result
        else:
            timing = {"chunk_count": result, "total_ms": total_ms}

        # Mark task as completed
        with session_scope() as db:
            update_task_status(db, task_id, "completed", progress=1.0, completed=True)

        # Send notification
        _send_indexing_notification(user_id, file_id, timing)

        logger.info(
            f"Search indexing completed for file {file_uuid}: "
            f"{timing.get('chunk_count', 0)} chunks in {total_ms}ms"
        )
        benchmark_timing.mark(pipeline_task_id, "search_index_chunks_end")
        return {"status": "success", "file_id": file_id, **timing}

    except Exception as exc:
        logger.error(f"Search indexing failed for file {file_uuid}: {exc}")
        benchmark_timing.mark(pipeline_task_id, "search_index_chunks_end")

        # Mark task as failed
        try:
            with session_scope() as db:
                update_task_status(db, task_id, "failed", error_message=str(exc))
        except Exception:
            logger.error(f"Failed to update task status for {task_id}")

        # Record this attempt's wall-clock into per_retry_timings so the
        # analysis layer can tell retry-inflated wall-clock apart from
        # first-try wall-clock (Phase 2 PR #8, G26).
        benchmark_timing.record_retry(
            pipeline_task_id,
            stage="search_index",
            attempt=int(self.request.retries) + 1,
            start=total_start,
            end=time.time(),
            error=str(exc),
        )

        # Retry with exponential backoff for transient errors
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc

        return {"status": "failed", "file_id": file_id, "error": str(exc)}


@celery_app.task(
    name="update_file_access_index",
    priority=UtilityPriority.ROUTINE,
    max_retries=3,
    default_retry_delay=10,
)
def update_file_access_index(file_ids: list[int]) -> dict[str, Any]:
    """Reindex accessible_user_ids for specified files.

    Called when:
    - Collection share is created/updated/revoked
    - Group membership changes
    - File is added to/removed from a collection

    Computes the full set of user IDs with access to each file and
    performs a bulk partial update on the OpenSearch index.

    Args:
        file_ids: List of media file integer IDs to update.

    Returns:
        Dict with update stats.
    """
    from app.core.config import settings
    from app.db.session_utils import session_scope
    from app.services.opensearch_service import get_opensearch_client
    from app.services.permission_service import PermissionService

    if not file_ids:
        return {"status": "skipped", "reason": "no_file_ids"}

    client = get_opensearch_client()
    if not client:
        logger.warning("OpenSearch client not available, skipping access index update")
        return {"status": "skipped", "reason": "no_opensearch"}

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    updated = 0
    errors = 0

    for file_id in file_ids:
        try:
            # Compute all user IDs with access to this file
            with session_scope() as db:
                accessible_ids = PermissionService.get_users_with_file_access(db, file_id)

            if not accessible_ids:
                logger.debug(f"No accessible users found for file {file_id}, skipping")
                continue

            # Build bulk update-by-query to set accessible_user_ids on all chunks
            response = client.update_by_query(
                index=index_name,
                body={
                    "query": {"term": {"file_id": file_id}},
                    "script": {
                        "source": "ctx._source.accessible_user_ids = params.ids",
                        "lang": "painless",
                        "params": {"ids": accessible_ids},
                    },
                },
                refresh=True,
                conflicts="proceed",
            )

            file_updated = response.get("updated", 0)
            updated += file_updated
            logger.debug(
                f"Updated accessible_user_ids for file {file_id}: "
                f"{file_updated} chunks, {len(accessible_ids)} users"
            )

        except Exception as e:
            errors += 1
            logger.error(f"Failed to update access index for file {file_id}: {e}")

    logger.info(
        f"Access index update complete: {updated} chunks updated across "
        f"{len(file_ids)} files, {errors} errors"
    )
    return {"status": "success", "updated": updated, "files": len(file_ids), "errors": errors}


@celery_app.task(
    name="update_file_tags_index",
    priority=UtilityPriority.ROUTINE,
    max_retries=3,
    default_retry_delay=10,
)
def update_file_tags_index(file_ids: list[int]) -> dict[str, Any]:
    """Overwrite the ``tags`` field on the search documents for the given files.

    Dispatched by ``services/tag_service.on_tags_changed`` from every path that
    changes which tags a file carries, so filtering by tag and searching by tag
    stay in agreement.

    Deliberately *not* ``index_transcript_search`` (reloads every segment and
    regenerates chunk embeddings on the GPU-adjacent embedding worker — the
    wrong weight class for a metadata edit) and *not* ``reindex_transcripts``
    (a per-user coordinator holding ``reindex_lock:{user_id}``, which would
    no-op on exactly the large multi-file merges this matters for). This is a
    lightweight ``update_by_query`` on the utility queue, same shape as
    :func:`update_file_access_index`.

    An empty tag list is written as an empty array rather than skipped —
    detaching a file's last tag has to clear the indexed value.

    Args:
        file_ids: Media file integer IDs whose tags changed.

    Returns:
        Dict with update stats.
    """
    from app.core.config import settings
    from app.db.session_utils import session_scope
    from app.models.media import FileTag
    from app.models.media import Tag
    from app.services.opensearch_service import get_opensearch_client

    if not file_ids:
        return {"status": "skipped", "reason": "no_file_ids"}

    client = get_opensearch_client()
    if not client:
        logger.warning("OpenSearch client not available, skipping tag index update")
        return {"status": "skipped", "reason": "no_opensearch"}

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    updated = 0
    errors = 0

    for file_id in file_ids:
        try:
            with session_scope() as db:
                tag_names = [
                    name
                    for (name,) in db.query(Tag.name)
                    .join(FileTag, FileTag.tag_id == Tag.id)
                    .filter(FileTag.media_file_id == file_id)
                    .all()
                ]

            response = client.update_by_query(
                index=index_name,
                body={
                    "query": {"term": {"file_id": file_id}},
                    "script": {
                        "source": "ctx._source.tags = params.tags",
                        "lang": "painless",
                        "params": {"tags": tag_names},
                    },
                },
                refresh=True,
                conflicts="proceed",
            )

            file_updated = response.get("updated", 0)
            updated += file_updated
            logger.debug(
                f"Updated tags for file {file_id}: {file_updated} chunks, {len(tag_names)} tags"
            )

        except Exception as e:
            errors += 1
            logger.error(f"Failed to update tag index for file {file_id}: {e}")

    logger.info(
        f"Tag index update complete: {updated} chunks updated across "
        f"{len(file_ids)} files, {errors} errors"
    )
    return {"status": "success", "updated": updated, "files": len(file_ids), "errors": errors}


def _send_indexing_notification(user_id: int, file_id: int, timing: dict[str, Any]) -> None:
    """Send search indexing completion notification via WebSocket."""
    try:
        from app.services.notification_service import send_task_notification

        send_task_notification(
            user_id,
            "search_indexing_complete",
            extra={"file_id": file_id, "timing": timing},
        )
    except Exception as e:
        logger.debug(f"Failed to send search indexing notification: {e}")
