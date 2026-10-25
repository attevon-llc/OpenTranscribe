"""Debounced re-index dispatch for transcript-content mutations (issue #666).

Four mutation paths change what a transcript's chunks/full-document index
*should* say and none of them dispatched a re-index: a segment text edit
(``PUT /files/{uuid}/transcript/segments/{uuid}``), a single-segment speaker
reassignment (``PUT /segments/{uuid}/speaker``), a speaker merge
(``POST /speakers/{uuid}/merge/{target_uuid}``), and a rediarize-only run
(``rediarize_task`` with no ``search_indexing`` in its caller-supplied
``downstream_tasks``). Search, RAG retrieval, and citations kept serving
pre-edit text indefinitely — see the issue for the full trace.

``dispatch_transcript_reindex`` is the ONE entry point all four now call. It
routes through ``index_transcript_search_task`` — the same full weight-class
task the initial transcription pipeline uses — rather than a narrower
``update_by_query`` path, because ``update_by_query`` can rewrite keyword
fields but cannot re-embed: the neural embedding pipeline is attached per bulk
*index* action (``services/search/indexing_service.py``'s ingest pipeline),
never as the chunks index's ``default_pipeline``, so a query-only rewrite
would leave the vector stale even though the text field looked fixed. The
full task also refreshes the ``transcripts`` full-document index
(``opensearch_service.index_transcript``, called from inside the task) and,
via ``TranscriptIndexingService.index_transcript_chunks``, bumps the chat
corpus version — see ``indexing_service._invalidate_chat_retrieval_cache`` —
which the search response cache now also keys on (``hybrid_search_service``'s
``corpus_version`` cache-key field), so a stale cached search page is never
served past its own bump either, without needing a second invalidation path.

Debounced per file via a Redis ``SET NX EX`` claim, matching the pattern
used throughout ``app/tasks`` (e.g. ``reindex_task.py``'s
``reindex_lock:{user_id}``). A user correcting many segments in quick
succession claims the debounce window once and queues exactly one re-index,
which fires ``TRANSCRIPT_REINDEX_DEBOUNCE_SECONDS`` after the FIRST edit in
the burst (not the last) — a bounded worst case rather than an unbounded
"never index while the user keeps typing" window.
"""

from __future__ import annotations

import enum
import logging

from app.core.constants import TRANSCRIPT_REINDEX_DEBOUNCE_SECONDS

logger = logging.getLogger(__name__)

_DEBOUNCE_KEY_TEMPLATE = "transcript_reindex_debounce:{file_uuid}"


class ReindexDispatch(enum.Enum):
    """What one dispatch attempt did (issue #1233).

    ``ALREADY_PENDING`` and ``FAILED`` used to be the same ``False``, so a caller that
    must report a reindex it could not queue (``speaker_cleanup``) could not tell the
    debounce window's "the reindex you need is already queued" from "nothing will ever
    rebuild this file's planes".
    """

    QUEUED = "queued"
    ALREADY_PENDING = "already_pending"
    FAILED = "failed"


def queue_transcript_reindex(
    *,
    file_id: int,
    file_uuid: str,
    user_id: int,
    priority: int | None = None,
) -> ReindexDispatch:
    """Queue a debounced full re-index of one file's transcript content.

    Safe to call from a request handler or a Celery task after committing a
    mutation. Never raises — a dispatch failure must not turn a successful
    edit into a failed request; the caller decides whether ``FAILED`` is worth
    reporting. A failed publish releases the debounce claim it took, so the next
    mutation of the file tries again instead of being told for the rest of the window
    that a reindex nobody queued is already pending.

    Args:
        file_id: Internal media file id.
        file_uuid: Media file UUID string.
        user_id: Owner user id (needed by the indexing task for the
            access-list rebuild and completion notification).
        priority: Optional Celery priority for the queued task (lower runs first). Left
            unset, the task's own default applies; the recovery sweep passes the retry
            priority so a file that already waited out a failure is not queued behind new work.

    Returns:
        ``QUEUED`` when a re-index was newly queued, ``ALREADY_PENDING`` when one is
        already queued within the debounce window, ``FAILED`` when none could be.
    """
    key = _DEBOUNCE_KEY_TEMPLATE.format(file_uuid=file_uuid)
    claimed = False
    try:
        from app.core.redis import get_redis
        from app.tasks.search_indexing_task import index_transcript_search_task

        redis = get_redis()
        claimed = bool(redis.set(key, "1", nx=True, ex=TRANSCRIPT_REINDEX_DEBOUNCE_SECONDS))
        if not claimed:
            logger.debug(
                f"Re-index already pending for file {file_uuid} within the debounce window"
            )
            return ReindexDispatch.ALREADY_PENDING

        options: dict[str, int] = {"countdown": TRANSCRIPT_REINDEX_DEBOUNCE_SECONDS}
        if priority is not None:
            options["priority"] = priority
        index_transcript_search_task.apply_async(
            kwargs={"file_id": file_id, "file_uuid": file_uuid, "user_id": user_id},
            **options,
        )
        logger.info(
            f"Queued debounced re-index for file {file_uuid} "
            f"(fires in {TRANSCRIPT_REINDEX_DEBOUNCE_SECONDS}s)"
        )
        _report_queued(file_id)
        return ReindexDispatch.QUEUED
    except Exception:  # noqa: BLE001 — a dispatch failure must not break the mutation
        logger.exception(f"Could not queue transcript re-index for file {file_uuid}")
        if claimed:
            _release_claim(key, file_uuid)
        return ReindexDispatch.FAILED


def _report_queued(file_id: int) -> None:
    """The file's search index is outstanding again until the queued rebuild reports."""
    from app.core.enums import PostProcessingState
    from app.core.enums import PostProcessingTask
    from app.services import post_processing_status

    post_processing_status.report(
        PostProcessingTask.SEARCH_INDEXING, PostProcessingState.QUEUED, file_id=file_id
    )


def _release_claim(key: str, file_uuid: str) -> None:
    """Drop a debounce claim whose publish failed; the key's TTL is the fallback."""
    try:
        from app.core.redis import get_redis

        get_redis().delete(key)
    except Exception:  # noqa: BLE001 — the claim expires on its own TTL
        logger.exception(f"Could not release the reindex debounce claim for file {file_uuid}")


def dispatch_transcript_reindex(
    *,
    file_id: int,
    file_uuid: str,
    user_id: int,
    priority: int | None = None,
) -> bool:
    """:func:`queue_transcript_reindex` for callers that only need "was one queued now".

    Returns:
        True if a re-index was newly queued for this file, False if one was
        already pending within the debounce window or dispatch failed.
    """
    return (
        queue_transcript_reindex(
            file_id=file_id, file_uuid=file_uuid, user_id=user_id, priority=priority
        )
        is ReindexDispatch.QUEUED
    )
