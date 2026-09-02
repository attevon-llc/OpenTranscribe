"""Which owners — and which of their files — a reindex must cover (issue #627).

``POST /search/reindex`` scoped **all three** of its modes to the calling admin:
the "Reindex all" button, the pending-only sweep, and an explicitly supplied list
of file UUIDs. ``reindex_transcripts_task`` filters ``MediaFile.user_id ==
user_id`` (``tasks/reindex_task.py``), so an admin repairing "the corpus" from
Settings → Search repaired only their own account: every other user's files were
left exactly as they were, with no error, no warning and nothing in the response
to say the scope had been narrowed. Naming another user's file UUID explicitly
did nothing at all, for the same reason.

This module answers the **scope** question. Dispatch stays in
``model_switch.dispatch_reindex_for_every_owner``, which #437 already built for
the model-switch path and ``index_health`` already reuses — there is one fan-out
loop in this codebase, not two.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.services.ingest_artifacts.index_mapping import chunk_plane_clause

logger = logging.getLogger(__name__)

#: Ceiling on the ``terms`` aggregation that surveys which files are indexed.
#: OpenSearch refuses a ``size`` above ``search.max_buckets``; 65,536 is the
#: cluster default and also far beyond any realistic corpus.
_MAX_AGG_BUCKETS = 65536

#: Headroom over the known file count, so a file indexed between the Postgres
#: read and the aggregation still lands in a bucket instead of being truncated
#: away and re-queued.
_AGG_BUCKET_HEADROOM = 100


def owners_of_files(file_uuids: list[str]) -> dict[int, list[str]]:
    """Group explicitly named files by the account that owns them.

    An operator naming file UUIDs by hand has no way to know — and no reason to
    care — which account each one belongs to. Attributing all of them to the
    caller means the coordinator's ``MediaFile.user_id == user_id`` filter
    silently drops every file the caller does not own.

    Only COMPLETED files are returned, matching the coordinator's own filter: a
    UUID that is unknown, deleted, or still processing cannot be re-indexed, and
    reporting that as "queued" is the class of silence this module exists to end.

    Args:
        file_uuids: The UUIDs the caller asked for.

    Returns:
        Owner id → the subset of ``file_uuids`` that owner owns. Empty when none
        of them resolve.
    """
    from app.db.session_utils import session_scope
    from app.models.media import FileStatus
    from app.models.media import MediaFile

    if not file_uuids:
        return {}

    with session_scope() as db:
        rows = (
            db.query(MediaFile.user_id, MediaFile.uuid)
            .filter(
                MediaFile.uuid.in_(file_uuids),
                MediaFile.status == FileStatus.COMPLETED,
            )
            .all()
        )

    grouped: dict[int, list[str]] = {}
    for user_id, file_uuid in rows:
        grouped.setdefault(int(user_id), []).append(str(file_uuid))
    return grouped


def pending_files_by_owner() -> tuple[dict[int, list[str]], int]:
    """Every indexable file with no chunks in the index, grouped by owner.

    "Indexable" is COMPLETED **and** holding at least one transcript segment —
    a completed file with nothing to chunk would be reported as forever pending.

    Returns:
        ``(pending_by_owner, indexable_total)``. An empty mapping with a nonzero
        total means the corpus is fully indexed; a zero total means there is
        nothing to index at all. The two are different answers and the endpoint
        reports them differently.
    """
    from sqlalchemy import exists
    from sqlalchemy import select

    from app.db.session_utils import session_scope
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment

    with session_scope() as db:
        has_segments = exists(
            select(TranscriptSegment.id).where(TranscriptSegment.media_file_id == MediaFile.id)
        )
        rows = (
            db.query(MediaFile.user_id, MediaFile.uuid)
            .filter(MediaFile.status == FileStatus.COMPLETED, has_segments)
            .all()
        )

    owner_by_uuid = {str(file_uuid): int(user_id) for user_id, file_uuid in rows}
    if not owner_by_uuid:
        return {}, 0

    indexed = indexed_file_uuids(len(owner_by_uuid))

    pending: dict[int, list[str]] = {}
    for file_uuid, user_id in owner_by_uuid.items():
        if file_uuid not in indexed:
            pending.setdefault(user_id, []).append(file_uuid)
    return pending, len(owner_by_uuid)


def indexed_file_uuids(expected_files: int) -> set[str]:
    """File UUIDs that already hold CHUNK-plane documents, across every owner.

    ``chunk_plane_clause()`` rather than a bare ``file_uuid`` term because the
    question is "which files still need indexing?": a file left holding only a
    digest — a rebuild that failed part-way — would otherwise read as indexed and
    never be repaired.

    ⚠️ **This fails OPEN, and now at corpus scale.** An unreachable cluster or a
    rejected aggregation returns the empty set, so every file reads as pending
    and the sweep queues the whole corpus rather than nothing. That is the
    pre-existing behaviour of this survey and it is the safe direction — a
    re-index overwrites documents in place by deterministic id, so the cost of
    guessing wrong is time, whereas guessing "already indexed" leaves a broken
    index broken. The blast radius is larger than it was when the survey was
    per-caller, which is why the failure is logged with a traceback.

    Args:
        expected_files: How many files the caller is asking about; sizes the
            aggregation so no owner's files are truncated out of the answer.

    Returns:
        The set of file UUIDs with at least one chunk document.
    """
    from app.services.opensearch_service import opensearch_client

    if not opensearch_client:
        return set()

    try:
        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        if not opensearch_client.indices.exists(index=index_name):
            return set()

        agg_response = opensearch_client.search(
            index=index_name,
            body={
                "size": 0,
                "query": {"bool": {"filter": [chunk_plane_clause()]}},
                "aggs": {
                    "indexed_files": {
                        "terms": {
                            "field": "file_uuid",
                            "size": min(expected_files + _AGG_BUCKET_HEADROOM, _MAX_AGG_BUCKETS),
                        }
                    }
                },
            },
        )
        buckets = agg_response.get("aggregations", {}).get("indexed_files", {}).get("buckets", [])
        return {bucket["key"] for bucket in buckets}
    except Exception as e:
        logger.exception(f"Error querying indexed files; treating the corpus as pending: {e}")
        return set()
