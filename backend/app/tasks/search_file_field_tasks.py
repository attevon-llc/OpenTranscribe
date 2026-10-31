"""Celery tasks that rewrite one field family of an already-indexed file.

``update_file_access_index`` (ACL fields), ``update_file_tags_index`` (tag names) and
``index_file_summary`` (the summary plane) each touch a slice of the search documents
without re-embedding the transcript; the transcript indexer itself lives in
:mod:`app.tasks.search_indexing_task`.
"""

import logging
from typing import Any

from app.core.celery import celery_app
from app.core.enums import PostProcessingTask
from app.core.queues import UtilityPriority
from app.services.post_processing_status import tracks_post_processing
from app.tasks.search_indexing_task import extract_file_index_metadata

logger = logging.getLogger(__name__)


@celery_app.task(
    name="update_file_access_index",
    priority=UtilityPriority.ROUTINE,
    max_retries=3,
    default_retry_delay=10,
)
def update_file_access_index(file_ids: list[int]) -> dict[str, Any]:
    """Reindex accessible_user_ids and collection_ids for specified MEDIA files.

    Called when:
    - Collection share is created/updated/revoked
    - Group membership changes
    - File is added to/removed from a collection

    Computes the full set of user IDs with access to each file and
    performs a bulk partial update on the OpenSearch index. ``collection_ids`` is
    refreshed in the same pass: it is the second arm of the read-access clause
    (``search/access_filter``, organization collections), so it must follow every
    add to or removal from a collection, not only the ones that change a share.

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
    from app.models.collection import CollectionMember
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

    for file_id in file_ids:
        try:
            # Compute all user IDs with access to this file
            with session_scope() as db:
                accessible_ids = PermissionService.get_users_with_file_access(db, file_id)
                collection_ids = [
                    int(cid)
                    for (cid,) in db.query(CollectionMember.collection_id).filter(
                        CollectionMember.media_file_id == file_id
                    )
                ]

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

            response = client.update_by_query(
                index=index_name,
                body={
                    "query": {"term": {"file_id": file_id}},
                    "script": {
                        "source": (
                            "ctx._source.accessible_user_ids = params.ids; "
                            "ctx._source.collection_ids = params.cids"
                        ),
                        "lang": "painless",
                        "params": {"ids": accessible_ids, "cids": collection_ids},
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

    # Unconditional — including when errors > 0. A chat retrieval cache entry is
    # keyed ACL-blind (see retrieval_cache.cache_key), so an ACL rewrite here is
    # invisible to it unless the corpus version moves. This must run AFTER the
    # update_by_query loop above (which passed refresh=True), so a forced cache
    # miss re-runs against the now-corrected index rather than racing it. A
    # bump is never incorrect — worst case is an extra cache miss — so it must
    # fire even on partial failure; fail safe, never fail closed on this.
    from app.services.chat.retrieval_cache import bump_corpus_version

    bump_corpus_version()

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
    from app.models.tag import FileTag
    from app.models.tag import Tag
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
                    "query": {"term": {"file_id": file_id}},
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
    name="index_file_summary",
    priority=UtilityPriority.ROUTINE,
)
@tracks_post_processing(PostProcessingTask.SUMMARY_INDEXING, file_param="file_id")
def index_file_summary(file_id: int) -> dict[str, Any]:
    """Rebuild ONE file's summary plane from ``media_file.summary_data`` (issue #963).

    The *fresh-summary* trigger: ``index_transcript_chunks`` already regenerates
    the summary plane on every transcript rebuild (addendum G1), but a summary
    is generated — or cleared — LATER and independently of the transcript, so
    it needs its own light trigger. Dispatched AFTER the write that changed
    ``summary_data`` has COMMITTED — never from inside that transaction. A
    dispatch inside the transaction would either read uncommitted state or,
    worse, tempt a caller to pass the dict along instead of letting this task
    re-read it from Postgres, which is precisely the #67 shape
    (``_index_summary_plane`` has no ``summary_data`` parameter for exactly
    this reason).

    Args:
        file_id: Media file integer id. ``summary_data`` may be a populated
            dict, ``None`` (a cleared/never-generated summary — the plane
            prunes to zero), or anything else JSON-shaped; the document
            builder handles all three.

    Returns:
        Dict with ``status`` and ``summary_leaves`` (the count that landed).
    """
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.services.search.embedding_provenance import active_embedding_model
    from app.services.search.indexing_service import TranscriptIndexingService
    from app.services.search.pipelines import is_neural_pipeline_available

    with session_scope() as db:
        media_file = db.query(MediaFile).filter(MediaFile.id == file_id).one_or_none()
        if media_file is None:
            logger.warning(f"index_file_summary: file {file_id} not found")
            return {"status": "not_found", "summary_leaves": 0}
        file_uuid = media_file.uuid
        user_id = media_file.user_id
        meta = extract_file_index_metadata(db, media_file, file_id)

    use_neural = is_neural_pipeline_available()
    provenance = active_embedding_model() if use_neural else None

    indexing_service = TranscriptIndexingService()
    leaves = indexing_service._index_summary_plane(
        file_id=file_id,
        file_uuid=str(file_uuid),
        base_metadata={
            "user_id": user_id,
            "title": meta["title"],
            "tags": meta["tag_names"],
            "upload_time": meta["upload_time"],
            "language": meta["language"],
            "content_type": meta["content_type"],
            "duration": meta["duration"],
            "file_size": meta["file_size"],
            "collection_ids": meta["collection_ids"],
            "accessible_user_ids": meta["accessible_user_ids"],
            "indexed_at": None,
            "embedding_model": provenance,
            **(
                {}
                if meta["organization_id"] is None
                else {"organization_id": meta["organization_id"]}
            ),
        },
        use_neural=use_neural,
    )
    return {"status": "success", "summary_leaves": leaves}
