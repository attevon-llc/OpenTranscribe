"""Celery task for search indexing after transcription completion."""

import logging
import time
from typing import Any

from app.core.celery import celery_app
from app.core.constants import EmbeddingPriority
from app.core.constants import UtilityPriority
from app.utils import benchmark_timing

logger = logging.getLogger(__name__)


def resolve_chunk_speaker_name(speaker: Any) -> str:
    """The chunk-index writer's speaker-label resolution.

    Delegates to :func:`~app.utils.speaker_labels.canonical_speaker_label` — the
    SINGLE home for this resolution — rather than the ad hoc
    ``display_name or name or "Unknown"`` chain this writer used to run
    inline. That chain emitted a bare ``"Unknown"`` for an unresolved segment
    while ``file_facts`` wrote ``"Unknown Speaker"`` and ``files/crud.py``
    wrote lowercase ``"Unknown speaker"`` — a THIRD spelling of "nobody was
    attributed here" in the one plane a `terms(speaker)` search filter reads.
    It also never looked at a confident LLM/embedding suggestion, so a speaker
    the digest/facts planes already named from ``suggested_name`` still
    indexed under the raw diarization label here.

    A pure function (no I/O) so it is directly testable and directly reused —
    see this module's own docstring note and
    ``tests/unit/test_canonical_speaker_label.py``'s cross-plane agreement
    tests, which import and call this exact function rather than a copy of
    its logic.

    Args:
        speaker: The segment's linked ``Speaker`` ORM row (or any object with
            the same four attributes), or ``None`` for an unresolved segment.

    Returns:
        The canonical display label. Never the bare ``"Unknown"`` this writer
        used to emit for an unattributed segment.
    """
    from app.utils.speaker_labels import canonical_speaker_label

    if speaker is None:
        return canonical_speaker_label(None)
    return canonical_speaker_label(
        speaker.name,
        display_name=speaker.display_name,
        suggested_name=speaker.suggested_name,
        confidence=speaker.confidence,
    )


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

    # Re-verify the neural pipeline before stamping provenance on anything (#437).
    #
    # `_neural_pipeline_verified` is a permanent process latch with NO TTL — unlike
    # its `hybrid_search_service` neighbours, which expire in 30 s/120 s — and it
    # caches the embedding-model LABEL alongside it. The two existing reset points
    # do not reach this process: `model_switch` resets the API process and
    # `reindex_task` resets whichever worker runs `reindex_transcripts`, which is
    # routed to the CPU queue. THIS task is routed to `CeleryQueues.EMBEDDING`
    # (`core/celery.py`), i.e. the separate search-indexer container, and nothing
    # reset it there.
    #
    # So after an embedding-model switch, a newly transcribed file was stamped with
    # the OLD model name while the cluster's ingest pipeline embedded it with the
    # NEW one. `survey_embedding_models()` then reported `MIXED VECTOR SPACE` and
    # told the operator to reindex a corpus that was in fact uniform — the exact
    # cry-wolf failure `embedding_provenance`'s own docstring warns about. It
    # self-healed only when the worker recycled at `--max-tasks-per-child=500`.
    #
    # Cost is one `ingest.get_pipeline` per file. A TTL would bound the staleness
    # instead of removing it; this is the pattern `reindex_task` already uses.
    from app.services.search.indexing_service import reset_neural_pipeline_state

    reset_neural_pipeline_state()

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

            # start_time alone is NOT a total order: overlapping speech and
            # interpolated backchannels routinely share an onset (measured on the
            # eval corpus: 3,072 tie groups covering 6,152 segments). Postgres then
            # returns tied rows in physical order, which a delete-then-bulk-insert
            # reshuffles — so the same transcript re-indexed produced a DIFFERENT
            # speaker-turn grouping, a different chunk count, and a different
            # nDCG@10. Ordering by (start_time, end_time, id) makes the sequence a
            # function of the data alone, so re-indexing is reproducible.
            segments = (
                db.query(TranscriptSegment)
                .options(joinedload(TranscriptSegment.speaker))
                .filter(TranscriptSegment.media_file_id == file_id)
                .order_by(
                    TranscriptSegment.start_time,
                    TranscriptSegment.end_time,
                    TranscriptSegment.id,
                )
                .all()
            )

            if not segments:
                logger.warning(f"No segments found for file {file_uuid}, skipping indexing")
                update_task_status(db, task_id, "completed", progress=1.0, completed=True)
                return {"status": "skipped", "reason": "no_segments"}

            # Shared per-segment derived values consumed by both indexes.
            # Chunk-level index wants a non-null, CANONICAL fallback
            # (`resolve_chunk_speaker_name`) so the BM25 ``speaker`` field is
            # always filterable and agrees with every other plane on how "no
            # attribution" spells; the full-doc index wants the raw name or
            # None to preserve speaker-transition structure in the document
            # body.
            seg_dicts_full: list[dict[str, str | None]] = []
            segment_dicts: list[dict[str, float | str | int | None]] = []
            for seg in segments:
                speaker_obj = seg.speaker
                raw_name = speaker_obj.name if speaker_obj else None
                chunk_speaker = resolve_chunk_speaker_name(speaker_obj)

                seg_dicts_full.append({"text": seg.text, "speaker": raw_name})
                segment_dicts.append(
                    {
                        "start": float(seg.start_time),
                        "end": float(seg.end_time),
                        "text": seg.text or "",
                        "speaker": chunk_speaker,
                        # The joinedload above already attached `speaker`, so
                        # these two are plain column reads — no new query. Both
                        # are None (and therefore absent from the chunk doc,
                        # see chunking_service._make_chunk) for an unresolved
                        # segment; downstream readers must use an `exists`
                        # compat arm rather than assume the field is present.
                        "speaker_id": speaker_obj.id if speaker_obj else None,
                        "profile_id": speaker_obj.profile_id if speaker_obj else None,
                    }
                )

            # Extract per-file metadata from the MediaFile while the session
            # is still open — relationship access (tags, collections)
            # requires attached ORM state.
            meta = extract_file_index_metadata(db, media_file, file_id)
            title = meta["title"]
            # SORTED, not `list(set(...))` — see storage.get_unique_speaker_names
            # (issue #455). Python randomises string hashing per process unless
            # PYTHONHASHSEED is pinned (it is not, anywhere), so set iteration
            # order differed between workers. This list goes into every chunk
            # document, so an unsorted one made the same transcript index to
            # different content — and therefore different EMBEDDINGS — depending
            # on which worker happened to pick up the task.
            from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABELS

            speaker_names = sorted(
                {
                    str(s["speaker"])
                    for s in segment_dicts
                    if s["speaker"] not in UNKNOWN_SPEAKER_LABELS
                }
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

        # `index_transcript_chunks` returns a dict or RAISES (issue #495). It used to
        # be able to return a bare int, and that arm is what turned every indexing
        # failure into a reported success: the method swallowed its exception and
        # returned 0, this branch wrapped it as `{"chunk_count": 0}`, and the task went
        # on to mark the row completed and return `"status": "success"`.
        #
        # The int was ALSO the legitimate "nothing to index" answer (no client, no
        # segments, no chunks generated), which is exactly why the failure was
        # invisible — the two were the same value. Those cases now return a dict
        # carrying a `reason`, and a genuine failure reaches the `except` below.
        timing = result

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


def _document_plane_exclusion_clause() -> dict[str, Any]:
    """The MEDIA-plane predicate: everything in the chunks index EXCEPT a document's
    own chunks (#T10 — the `file_id` collision).

    ``Document.id`` and ``MediaFile.id`` are independent integer sequences, so a bare
    ``{"term": {"file_id": file_id}}`` in an ACL/tag rewrite also matches a document
    whose id happens to equal this media file's id — sharing media file #42 would then
    silently overwrite document #42's chunk ACL, possibly with a DIFFERENT user's grant
    list. This is deliberately NOT :func:`~app.services.search.indexing_service.
    chunk_plane_clause` (chunk-plane-only): the digest plane must stay reachable here
    (addendum G5, ``services/search/CLAUDE.md``) — a share revocation that stopped
    reaching digests would leave a readable summary of a de-shared recording. So this
    excludes only the document planes, leaving both transcript chunks and digests in
    scope, exactly as before the fix for every id that does NOT collide.
    """
    from app.services.ingest_artifacts import index_mapping as digest_mapping

    return {
        "bool": {
            "must_not": [
                {"term": {digest_mapping.DOC_TYPE_FIELD: digest_mapping.DOC_TYPE_DOCUMENT_CHUNK}},
                {"term": {digest_mapping.DOC_TYPE_FIELD: digest_mapping.DOC_TYPE_DOCUMENT_DIGEST}},
            ]
        }
    }


def _document_accessible_user_ids(owner_id: int) -> list[int]:
    """The full grant list for one document's chunks — currently just its owner.

    The named seam a future document-sharing lane extends: once a real grant
    source exists (a direct or collection-style document share), this is the one
    place that widens past ``[owner_id]``, the same role
    ``PermissionService.get_users_with_file_access`` already plays for media files.
    Kept as its own function (rather than inlined in :func:`update_document_access_index`'s
    loop) so a test can monkeypatch exactly this and prove the REST of the rewrite
    mechanism — the plane-scoped ``update_by_query`` — already produces a
    sharee-visible chunk today, with no other change needed when that grant source
    lands.
    """
    return [owner_id]


def _document_plane_clause() -> dict[str, Any]:
    """The DOCUMENT-plane predicate: only a document's own chunks.

    No compat arm needed — every ``document_chunk`` document postdates v6 and always
    carries ``doc_type`` (matches
    :func:`~app.services.search.indexing_service.document_chunk_plane_clause`, which
    this re-derives rather than imports only to keep this module's OpenSearch-query
    surface self-contained the way its media sibling above is).
    """
    from app.services.ingest_artifacts import index_mapping as digest_mapping

    return {"term": {digest_mapping.DOC_TYPE_FIELD: digest_mapping.DOC_TYPE_DOCUMENT_CHUNK}}


@celery_app.task(
    name="update_file_access_index",
    priority=UtilityPriority.ROUTINE,
    max_retries=3,
    default_retry_delay=10,
)
def update_file_access_index(file_ids: list[int]) -> dict[str, Any]:
    """Reindex accessible_user_ids for specified MEDIA files.

    Called when:
    - Collection share is created/updated/revoked
    - Group membership changes
    - File is added to/removed from a collection

    Computes the full set of user IDs with access to each file and
    performs a bulk partial update on the OpenSearch index. Scoped to the
    media plane (transcript chunks + digests, never a document's chunks) by
    :func:`_document_plane_exclusion_clause` — see its docstring for why a
    same-integer-id document must not be touched here.

    Args:
        file_ids: List of media file integer IDs to update.

    Returns:
        Dict with update stats. ``missing_file_ids`` (only present when non-empty)
        lists ids for which no accessible-user set could be computed at all — a
        real anomaly (``PermissionService.get_users_with_file_access`` always
        includes the file's own owner unless the ``MediaFile`` row itself no
        longer exists), counted in ``errors`` rather than silently skipped, so a
        caller can tell "this file has no rights to propagate" apart from "we
        never touched this file's ACL."
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
    missing_file_ids: list[int] = []
    plane_filter = _document_plane_exclusion_clause()

    for file_id in file_ids:
        try:
            # Compute all user IDs with access to this file
            with session_scope() as db:
                accessible_ids = PermissionService.get_users_with_file_access(db, file_id)

            if not accessible_ids:
                # get_users_with_file_access always seeds the set with the file's
                # own owner, so an empty result means the MediaFile row itself
                # could not be resolved — a real error, not "zero accessible
                # users." A bare `continue` here used to be indistinguishable
                # from a successful no-op update.
                errors += 1
                missing_file_ids.append(file_id)
                logger.warning(
                    f"File {file_id} could not be resolved while computing access; "
                    "access index NOT updated for it"
                )
                continue

            # Plane-parameterised (#T10's shared-visibility half): the ONLY
            # difference from update_document_access_index's identical body below
            # is which plane_filter was computed above the loop.
            response = client.update_by_query(
                index=index_name,
                body={
                    "query": {"bool": {"filter": [{"term": {"file_id": file_id}}, plane_filter]}},
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
    result: dict[str, Any] = {
        "status": "success",
        "updated": updated,
        "files": len(file_ids),
        "errors": errors,
    }
    if missing_file_ids:
        result["missing_file_ids"] = missing_file_ids
    return result


@celery_app.task(
    name="update_document_access_index",
    priority=UtilityPriority.ROUTINE,
    max_retries=3,
    default_retry_delay=10,
)
def update_document_access_index(document_ids: list[int]) -> dict[str, Any]:
    """Reindex accessible_user_ids for specified DOCUMENTS — the document-plane
    sibling of :func:`update_file_access_index` (#T10's shared-visibility half).

    ``services/search/indexing_service.py``'s ``index_document_chunks`` hard-codes
    ``accessible_user_ids: [user_id]`` at index time because documents have no
    sharing model YET (v1 scope, ``api/endpoints/documents.py``'s own docstrings say
    so). This task is the missing rewrite path a future document-sharing lane needs —
    without it, granting a document share would have nothing to dispatch to, the same
    gap :func:`update_file_access_index` already closes for media files. Until that
    lane adds a real grant source, ``accessible_ids`` per document is just its owner,
    which is a correct (if trivial) no-op rewrite — the plumbing this task adds is
    the reusable part, not a new sharing feature.

    One shape with :func:`update_file_access_index`: the ``update_by_query`` body is
    identical in both; only the plane predicate differs (:func:`_document_plane_clause`
    here vs :func:`_document_plane_exclusion_clause` there). The body is not factored
    into a shared helper on purpose — see ``tests/unit/test_chunk_plane_compat_arm.py``,
    which requires each reader of this index to build its own predicate inline so the
    AST sweep over every caller can see it.

    Args:
        document_ids: ``Document.id`` values to update.

    Returns:
        Dict with update stats, same shape as :func:`update_file_access_index`.
    """
    from app.core.config import settings
    from app.db.session_utils import session_scope
    from app.models.document import Document
    from app.services.opensearch_service import get_opensearch_client

    if not document_ids:
        return {"status": "skipped", "reason": "no_document_ids"}

    client = get_opensearch_client()
    if not client:
        logger.warning("OpenSearch client not available, skipping document access index update")
        return {"status": "skipped", "reason": "no_opensearch"}

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    updated = 0
    errors = 0
    missing_document_ids: list[int] = []
    plane_filter = _document_plane_clause()

    # One grouped query for the whole batch — same reasoning as
    # update_file_tags_index's tags_by_file lookup below.
    with session_scope() as db:
        owners = dict(
            db.query(Document.id, Document.user_id).filter(Document.id.in_(document_ids)).all()
        )

    for document_id in document_ids:
        owner_id = owners.get(document_id)
        if owner_id is None:
            errors += 1
            missing_document_ids.append(document_id)
            logger.warning(
                f"Document {document_id} could not be resolved while computing access; "
                "access index NOT updated for it"
            )
            continue

        accessible_ids = _document_accessible_user_ids(int(owner_id))
        try:
            response = client.update_by_query(
                index=index_name,
                body={
                    "query": {
                        "bool": {"filter": [{"term": {"file_id": document_id}}, plane_filter]}
                    },
                    "script": {
                        "source": "ctx._source.accessible_user_ids = params.ids",
                        "lang": "painless",
                        "params": {"ids": accessible_ids},
                    },
                },
                refresh=True,
                conflicts="proceed",
            )
            updated += response.get("updated", 0)
        except Exception as e:
            errors += 1
            logger.error(f"Failed to update access index for document {document_id}: {e}")

    logger.info(
        f"Document access index update complete: {updated} chunks updated across "
        f"{len(document_ids)} documents, {errors} errors"
    )
    result: dict[str, Any] = {
        "status": "success",
        "updated": updated,
        "documents": len(document_ids),
        "errors": errors,
    }
    if missing_document_ids:
        result["missing_document_ids"] = missing_document_ids
    return result


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
    :func:`update_file_access_index` — including its plane scoping: MEDIA
    files only, via :func:`_document_plane_exclusion_clause`, for the same
    ``file_id``-collision reason (#T10) documented there. Tags are file
    metadata, not document metadata, so there is no document-plane sibling
    task for this one the way :func:`update_document_access_index` mirrors
    the ACL rewrite.

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
    plane_filter = _document_plane_exclusion_clause()

    # One grouped query for the whole batch: a 500-file merge used to open 500
    # sessions and issue 500 round trips for what is a single join.
    tags_by_file: dict[int, list[str]] = {}
    try:
        with session_scope() as db:
            rows = (
                db.query(FileTag.media_file_id, Tag.name)
                .join(Tag, FileTag.tag_id == Tag.id)
                .filter(FileTag.media_file_id.in_(file_ids))
                .all()
            )
        for tagged_file_id, name in rows:
            if tagged_file_id is None:
                continue
            tags_by_file.setdefault(int(tagged_file_id), []).append(name)
        pending_ids = file_ids
    except Exception as e:
        # The per-file version counted a failed lookup as that file's error and
        # carried on; with one query the whole batch fails together.
        logger.error(f"Failed to load tag names for {len(file_ids)} file(s): {e}")
        errors = len(file_ids)
        pending_ids = []

    for file_id in pending_ids:
        try:
            tag_names = tags_by_file.get(int(file_id), [])
            response = client.update_by_query(
                index=index_name,
                body={
                    "query": {"bool": {"filter": [{"term": {"file_id": file_id}}, plane_filter]}},
                    "script": {
                        "source": "ctx._source.tags = params.tags",
                        "lang": "painless",
                        "params": {"tags": tag_names},
                    },
                },
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

    # One refresh for the batch instead of a forced segment refresh per file;
    # the writes are still visible by the time the task returns.
    if updated:
        try:
            client.indices.refresh(index=index_name)
        except Exception as e:
            logger.warning(f"Tag index refresh failed after {updated} update(s): {e}")

    logger.info(
        f"Tag index update complete: {updated} chunks updated across "
        f"{len(file_ids)} files, {errors} errors"
    )
    return {"status": "success", "updated": updated, "files": len(file_ids), "errors": errors}


@celery_app.task(
    name="backfill_speaker_id_fields",
    priority=UtilityPriority.ROUTINE,
)
def backfill_speaker_id_fields_task(limit: int = 200) -> dict[str, Any]:
    """OPT-IN maintenance pass: accelerate coverage of ``speaker_id``/``profile_id``.

    **Nothing dispatches this automatically.** The two fields are written going
    forward and backfilled LAZILY — rename propagation, a per-file reprocess, or
    the ordinary next time a file is reindexed for any other reason all pick them
    up for free (``chunking_service._make_chunk`` writes them whenever the segment
    dicts it is handed carry them, which both ``index_transcript_search_task`` and
    ``reindex_task._extract_file_metadata`` now do). This task exists only for an
    operator who wants to accelerate that ahead of natural churn; call it by hand
    or wire it to a schedule deliberately — it is not part of the indexing pipeline.

    It is a THIN wrapper around the existing, already-safe per-user
    ``reindex_transcripts_task`` coordinator, not a second bulk OpenSearch write
    path: it only decides WHICH (user, file) pairs are missing the fields, then
    reuses that coordinator's lock, orphan-sweep and progress-tracking machinery
    exactly as a manual "reindex this file" action would.

    Candidates are found with a ``composite`` aggregation over CHUNK-plane
    documents lacking ``speaker_id`` — paginated by ``after_key``, not the
    50,000-bucket ``terms`` ceiling ``_get_indexed_uuids`` has (see its warning in
    ``services/search/indexing_service.py``), so this scales past that ceiling by
    construction rather than by coincidence of corpus size.

    Args:
        limit: Maximum number of distinct files to dispatch a reindex for in one
            call — a maintenance knob bounding queue impact, not a target to hit.

    Returns:
        Dict with ``status`` and, when files were found, ``dispatched_files`` /
        ``dispatched_users``.
    """
    from app.services.ingest_artifacts.index_mapping import chunk_plane_clause
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if not client:
        return {"status": "skipped", "reason": "no_opensearch"}

    from app.core.config import settings as app_settings

    index_name = app_settings.OPENSEARCH_CHUNKS_INDEX
    try:
        if not client.indices.exists(index=index_name):
            return {"status": "skipped", "reason": "no_index"}
    except Exception as e:
        logger.error(f"speaker_id backfill: could not check index existence: {e}")
        return {"status": "skipped", "reason": "opensearch_error"}

    by_user: dict[int, set[str]] = {}
    after_key: dict[str, Any] | None = None
    collected = 0

    while collected < limit:
        page_size = min(100, limit - collected)
        composite: dict[str, Any] = {
            "size": page_size,
            "sources": [
                {"user_id": {"terms": {"field": "user_id"}}},
                {"file_uuid": {"terms": {"field": "file_uuid"}}},
            ],
        }
        if after_key:
            composite["after"] = after_key

        try:
            response = client.search(
                index=index_name,
                body={
                    "size": 0,
                    "query": {
                        "bool": {
                            "filter": [chunk_plane_clause()],
                            "must_not": [{"exists": {"field": "speaker_id"}}],
                        }
                    },
                    "aggs": {"files": {"composite": composite}},
                },
            )
        except Exception as e:
            logger.error(f"speaker_id backfill: candidate survey failed: {e}")
            break

        files_agg = (response.get("aggregations") or {}).get("files", {})
        buckets = files_agg.get("buckets", [])
        if not buckets:
            break

        for bucket in buckets:
            key = bucket.get("key", {})
            user_id, file_uuid = key.get("user_id"), key.get("file_uuid")
            if user_id is None or file_uuid is None:
                continue
            by_user.setdefault(int(user_id), set()).add(str(file_uuid))
            collected += 1

        after_key = files_agg.get("after_key")
        if not after_key:
            break

    if not by_user:
        return {"status": "skipped", "reason": "fully_covered"}

    from app.core.constants import CPUPriority
    from app.tasks.reindex_task import reindex_transcripts_task

    dispatched_files = 0
    for user_id, file_uuids in by_user.items():
        reindex_transcripts_task.apply_async(
            args=[user_id, sorted(file_uuids)],
            priority=CPUPriority.MAINTENANCE,
        )
        dispatched_files += len(file_uuids)

    logger.info(
        f"speaker_id backfill: dispatched reindex for {dispatched_files} file(s) "
        f"across {len(by_user)} user(s)"
    )
    return {
        "status": "dispatched",
        "dispatched_files": dispatched_files,
        "dispatched_users": len(by_user),
    }


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
