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

import logging

from app.core.constants import TRANSCRIPT_REINDEX_DEBOUNCE_SECONDS

logger = logging.getLogger(__name__)

_DEBOUNCE_KEY_TEMPLATE = "transcript_reindex_debounce:{file_uuid}"


def dispatch_transcript_reindex(
    *,
    file_id: int,
    file_uuid: str,
    user_id: int,
) -> bool:
    """Queue a debounced full re-index of one file's transcript content.

    Safe to call from a request handler or a Celery task after committing a
    mutation. Never raises — a dispatch failure must not turn a successful
    edit into a failed request; it only means the index stays stale until the
    next full/maintenance reindex picks it up.

    Args:
        file_id: Internal media file id.
        file_uuid: Media file UUID string.
        user_id: Owner user id (needed by the indexing task for the
            access-list rebuild and completion notification).

    Returns:
        True if a re-index was newly queued for this file, False if one was
        already pending within the debounce window (or dispatch failed).
    """
    try:
        from app.core.redis import get_redis
        from app.tasks.search_indexing_task import index_transcript_search_task

        key = _DEBOUNCE_KEY_TEMPLATE.format(file_uuid=file_uuid)
        claimed = get_redis().set(key, "1", nx=True, ex=TRANSCRIPT_REINDEX_DEBOUNCE_SECONDS)
        if not claimed:
            logger.debug(
                f"Re-index already pending for file {file_uuid} within the debounce window"
            )
            return False

        index_transcript_search_task.apply_async(
            kwargs={"file_id": file_id, "file_uuid": file_uuid, "user_id": user_id},
            countdown=TRANSCRIPT_REINDEX_DEBOUNCE_SECONDS,
        )
        logger.info(
            f"Queued debounced re-index for file {file_uuid} "
            f"(fires in {TRANSCRIPT_REINDEX_DEBOUNCE_SECONDS}s)"
        )
        return True
    except Exception:  # noqa: BLE001 — a dispatch failure must not break the mutation
        logger.exception(f"Could not queue transcript re-index for file {file_uuid}")
        return False
