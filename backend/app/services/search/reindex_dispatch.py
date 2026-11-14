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
*index* action (``services/search/pipelines.py``'s ingest pipeline),
never as the chunks index's ``default_pipeline``, so a query-only rewrite
would leave the vector stale even though the text field looked fixed. The
full task also refreshes the ``transcripts`` full-document index
(``opensearch_service.index_transcript``, called from inside the task) and,
via ``TranscriptIndexingService.index_transcript_chunks``, bumps the chat
corpus version — see ``chat.retrieval_cache.bump_corpus_version`` —
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
# Claimed per file by the search-index sweep task; defined here so services never import tasks.
SWEEP_COOLDOWN_KEY_TEMPLATE = "search_index_sweep:{file_uuid}"


def reindex_coordination_keys(file_uuid: str) -> list[str]:
    """Every Redis key the reindex path claims for ``file_uuid`` (debounce + sweep cooldown)."""
    return [
        _DEBOUNCE_KEY_TEMPLATE.format(file_uuid=file_uuid),
        SWEEP_COOLDOWN_KEY_TEMPLATE.format(file_uuid=file_uuid),
    ]


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
        from app.core.redis import get_request_redis
        from app.tasks.search_indexing_task import index_transcript_search_task

        redis = get_request_redis()
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
    except Exception:  # a dispatch failure must not break the mutation
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
        from app.core.redis import get_request_redis

        get_request_redis().delete(key)
    except Exception:  # the claim expires on its own TTL
        logger.exception(f"Could not release the reindex debounce claim for file {file_uuid}")


def requeue_after_version_conflicts(
    *,
    conflicts: int,
    what: str,
    file_id: int | None = None,
    file_uuid: str | None = None,
) -> ReindexDispatch | None:
    """Queue the file's rebuild when a field rewrite skipped documents on version conflicts.

    The field rewrites (tags, access, speaker and title renames) are ``update_by_query``
    calls with ``conflicts="proceed"``. OpenSearch takes a snapshot of the index when the
    call starts, and a document another writer changed since then is not rewritten; it is
    only counted in ``version_conflicts`` (OpenSearch docs, "Update by query"). Two rewrites
    of one file in the same second collide on every document, because each chunk carries
    the file-level ``speakers`` and ``tags``: renaming two speakers and adding a tag right
    after left the losing value in the index until somebody ran a full reindex.

    The rebuild is :func:`queue_transcript_reindex`, which reads every field from Postgres
    when it runs, so whichever writes collided, the file converges on what Postgres holds.

    Args:
        conflicts: The ``version_conflicts`` count of the rewrite.
        what: The rewrite, for the log line.
        file_id: The file's integer id, when the caller has it.
        file_uuid: The file's UUID, when the caller has it. One of the two is required.

    Returns:
        ``None`` when nothing conflicted or the file no longer exists, else the dispatch
        outcome.
    """
    if not conflicts:
        return None
    identity = _file_identity(file_id=file_id, file_uuid=file_uuid)
    if identity is None:
        return None
    outcome = queue_transcript_reindex(
        file_id=identity[0], file_uuid=identity[1], user_id=identity[2]
    )
    logger.warning(
        f"{what} for file {identity[1]} skipped {conflicts} document(s) on version "
        f"conflicts; file rebuild {outcome.value}"
    )
    return outcome


def _file_identity(*, file_id: int | None, file_uuid: str | None) -> tuple[int, str, int] | None:
    """``(id, uuid, owner id)`` of one file, looked up by whichever key the caller holds."""
    from sqlalchemy.exc import SQLAlchemyError

    from app.db.session_utils import session_scope
    from app.models.media import MediaFile

    if file_id is None and not file_uuid:
        return None
    try:
        with session_scope() as db:
            query = db.query(MediaFile.id, MediaFile.uuid, MediaFile.user_id)
            if file_id is not None:
                query = query.filter(MediaFile.id == file_id)
            else:
                query = query.filter(MediaFile.uuid == file_uuid)
            row = query.first()
    except SQLAlchemyError:
        logger.exception(f"Could not look up file {file_id or file_uuid} to queue its rebuild")
        return None
    if row is None:
        return None
    return int(row[0]), str(row[1]), int(row[2])


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
