"""Scope and skip logic for the v3->v4 speaker embedding migration.

Migration status, the v4 index clear, the already-migrated file scan and the
privacy-policy (#1280) skip bookkeeping.
"""

import contextlib
import logging
from typing import Any

from app.core.constants import SPEAKER_SHORT_SEGMENT_MIN_DURATION
from app.core.embedding_models import get_speaker_index_v3
from app.core.embedding_models import get_speaker_index_v3_backup
from app.core.embedding_models import get_speaker_index_v4
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.services.embedding_mode_service import EmbeddingModeService
from app.services.privacy.policy import voiceprints_allowed_for_file
from app.services.privacy.redact_view import PrivacyView

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status helpers (unchanged API)
# ---------------------------------------------------------------------------


def get_migration_status() -> dict:
    """Get the current migration status.

    Degrades gracefully when OpenSearch is unreachable: constructing the client
    never touches the network, so an outage only surfaces on the first real call
    — return the same "not available" envelope as the no-client branch instead
    of letting a connection error bubble up as a 500 from the status endpoint.
    """
    from opensearchpy.exceptions import OpenSearchException

    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if not client:
        return {"status": "error", "message": "OpenSearch not available"}

    current_mode = EmbeddingModeService.detect_mode()
    v3_index = get_speaker_index_v3()
    v4_index = get_speaker_index_v4()
    backup_index = get_speaker_index_v3_backup()
    try:
        v4_exists = client.indices.exists(index=v4_index)
        backup_exists = client.indices.exists(index=backup_index)
    except OpenSearchException as e:
        logger.warning("OpenSearch unreachable while reading migration status: %s", e)
        return {"status": "error", "message": "OpenSearch not available"}

    # Count only speaker documents (exclude profile_ and cluster_ documents
    # which share the same index but have a document_type field)
    speaker_only_query = {"query": {"bool": {"must_not": {"exists": {"field": "document_type"}}}}}

    # Query concrete versioned indices directly (not through alias)
    try:
        v3_document_count = (
            client.count(index=v3_index, body=speaker_only_query)["count"]
            if client.indices.exists(index=v3_index)
            else 0
        )
    except Exception:
        v3_document_count = 0

    try:
        v4_document_count = (
            client.count(index=v4_index, body=speaker_only_query)["count"] if v4_exists else 0
        )
    except Exception:
        v4_document_count = 0

    # If v3 concrete index is empty, check the legacy backup (non-critical)
    if v3_document_count == 0 and backup_exists:
        with contextlib.suppress(Exception):
            v3_document_count = client.count(index=backup_index, body=speaker_only_query)["count"]

    return {
        "current_mode": current_mode,
        "v4_index_exists": v4_exists,
        "v3_document_count": v3_document_count,
        "v4_document_count": v4_document_count,
        "migration_needed": current_mode == "v3",
        "migration_complete": current_mode == "v4" and not backup_exists,
        "transcription_paused": False,
    }


# ---------------------------------------------------------------------------
# Index management helpers
# ---------------------------------------------------------------------------


def _clear_v4_index() -> None:
    """Delete all documents from the v4 speaker index for a clean re-migration."""
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if not client:
        logger.warning("OpenSearch not available — cannot clear v4 index")
        return

    v4_index = get_speaker_index_v4()
    if not client.indices.exists(index=v4_index):
        logger.info("v4 index does not exist — nothing to clear")
        return

    try:
        count_before = client.count(index=v4_index).get("count", 0)
        client.delete_by_query(
            index=v4_index,
            body={"query": {"match_all": {}}},
            refresh=True,
        )
        logger.info(f"Cleared {count_before} documents from {v4_index} for force re-migration")
    except Exception as e:
        logger.error(f"Failed to clear v4 index: {e}")


# ---------------------------------------------------------------------------
# Skip-logic helper
# ---------------------------------------------------------------------------


def _count_embeddable_speakers_per_file(file_ids: list[int]) -> dict[int, int]:
    """Count speakers that the pipeline will produce embeddings for, per file.

    Mirrors the threshold logic in migration_pipeline.collect_work_items:
    any speaker with at least one segment >= SPEAKER_SHORT_SEGMENT_MIN_DURATION
    will be processed (the pipeline falls back to that lower threshold when a
    speaker's longest merged segment is below SPEAKER_SEGMENT_MIN_DURATION).
    """
    from sqlalchemy import distinct
    from sqlalchemy import func as sa_func

    from app.models.media import TranscriptSegment
    from app.models.speaker import Speaker

    if not file_ids:
        return {}

    with session_scope() as db:
        rows = (
            db.query(Speaker.media_file_id, sa_func.count(distinct(Speaker.id)))
            .join(
                TranscriptSegment,
                (TranscriptSegment.speaker_id == Speaker.id)
                & (TranscriptSegment.media_file_id == Speaker.media_file_id),
            )
            .filter(
                Speaker.media_file_id.in_(file_ids),
                (TranscriptSegment.end_time - TranscriptSegment.start_time)
                >= SPEAKER_SHORT_SEGMENT_MIN_DURATION,
            )
            .group_by(Speaker.media_file_id)
            .all()
        )
        return {int(fid): cnt for fid, cnt in rows}


_ALREADY_MIGRATED_COMPOSITE_PAGE_SIZE = 10000


def _get_already_migrated_file_ids() -> set[int]:
    """Return set of media_file_id values fully migrated in the v4 index."""
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if not client:
        return set()

    v4_index = get_speaker_index_v4()
    if not client.indices.exists(index=v4_index):
        return set()

    try:
        # Issue #657, defect 6: a plain `terms` agg caps at `size` buckets -
        # 50,000 - and silently UNDER-reports past that, which re-processes
        # already-migrated files rather than erroring loudly. `composite`
        # pages through every distinct media_file_id via `after`, with no
        # cap on the total number of buckets visited.
        v4_counts: dict[int, int] = {}
        after: dict[str, Any] | None = None
        page_size = _ALREADY_MIGRATED_COMPOSITE_PAGE_SIZE
        while True:
            composite_agg: dict[str, Any] = {
                "size": page_size,
                "sources": [{"media_file_id": {"terms": {"field": "media_file_id"}}}],
            }
            if after is not None:
                composite_agg["after"] = after
            response = client.search(
                index=v4_index,
                body={
                    "size": 0,
                    "aggs": {"file_ids": {"composite": composite_agg}},
                },
            )
            file_ids_agg = response.get("aggregations", {}).get("file_ids", {})
            buckets = file_ids_agg.get("buckets", [])
            if not buckets:
                break
            for b in buckets:
                v4_counts[int(b["key"]["media_file_id"])] = b["doc_count"]
            after = file_ids_agg.get("after_key")
            if after is None or len(buckets) < page_size:
                break
    except Exception as e:
        logger.warning(f"Could not query v4 index for skip logic: {e}")
        return set()

    if not v4_counts:
        return set()

    embeddable_counts = _count_embeddable_speakers_per_file(list(v4_counts.keys()))

    fully_migrated: set[int] = set()
    partial_count = 0

    for file_id, v4_count in v4_counts.items():
        embeddable = embeddable_counts.get(file_id, 0)
        if embeddable == 0 or v4_count >= embeddable:
            fully_migrated.add(file_id)
        else:
            partial_count += 1

    if partial_count:
        logger.info(f"Found {partial_count} partially migrated files (will be re-processed)")

    return fully_migrated


# ---------------------------------------------------------------------------
# Privacy policy (#1280): a scope with voiceprints off is skipped, not migrated
# ---------------------------------------------------------------------------

#: Redis counter of files this migration run skipped because their owner or organization has
#: voiceprints switched off. Read (and cleared) by ``finalize_v4_migration_task``, which
#: reports it so an admin sees that the swapped-in index holds no voiceprints for them.
_SKIPPED_BY_POLICY_KEY = "embedding_migration:skipped_by_policy"


def _count_skipped_by_policy() -> None:
    try:
        from app.core.redis import get_redis

        r = get_redis()
        r.incr(_SKIPPED_BY_POLICY_KEY)
        r.expire(_SKIPPED_BY_POLICY_KEY, 86400)
    except Exception as e:
        logger.warning("Could not count a policy-skipped migration file: %s", e)


def _take_skipped_by_policy() -> int:
    try:
        from app.core.redis import get_redis

        r = get_redis()
        raw = r.get(_SKIPPED_BY_POLICY_KEY)
        r.delete(_SKIPPED_BY_POLICY_KEY)
        return int(raw or 0)
    except Exception as e:
        logger.warning("Could not read the policy-skipped migration count: %s", e)
        return 0


def _file_skipped_by_policy(media_file_id: int) -> bool:
    """True (and counted) when ``media_file_id``'s scope has voiceprints off.

    Asked fresh from a short session of its own, so a switch flipped mid-migration is honoured
    for every file not yet written. An unreadable policy is a skip (fail closed).
    """
    with session_scope() as db:
        allowed = voiceprints_allowed_for_file(db, int(media_file_id))
    if not allowed:
        _count_skipped_by_policy()
    return not allowed


def _scopes_off_by_policy() -> list[tuple[int, int | None]]:
    """Every (owner, organization) scope that owns files or profiles and has voiceprints off."""
    from app.models.speaker import SpeakerProfile

    with session_scope() as db:
        pairs: set[tuple[int, int | None]] = set()
        for model in (MediaFile, SpeakerProfile):
            for uid, org in db.query(model.user_id, model.organization_id).distinct().all():
                pairs.add((int(uid), int(org) if org is not None else None))
        view = PrivacyView(db)
        return sorted(
            (p for p in pairs if not view.voiceprints_for_scope(p[0], p[1]).enabled),
            key=lambda p: (p[0], -1 if p[1] is None else p[1]),
        )


def _off_scope_exclusion(off_scopes: list[tuple[int, int | None]]) -> list[dict[str, Any]]:
    """``must_not`` clauses matching every document of a scope that has voiceprints off.

    One clause per organization (with that organization's off users in a ``terms``), so the
    clause count stays small however many users switched off.
    """
    from app.services.opensearch_service.client import _speaker_org_filter_clauses

    users_by_org: dict[int | None, list[int]] = {}
    for uid, org in off_scopes:
        users_by_org.setdefault(org, []).append(uid)
    return [
        {"bool": {"filter": [{"terms": {"user_id": users}}, *_speaker_org_filter_clauses(org)]}}
        for org, users in users_by_org.items()
    ]
