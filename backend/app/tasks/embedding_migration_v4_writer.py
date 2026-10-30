"""Result writer for the v3->v4 speaker embedding migration pipeline.

Bulk-writes aggregated speaker embedding documents to the v4 index.
"""

import datetime
import logging

from app.core.embedding_models import get_speaker_index_v4
from app.services.speaker_analysis_models import SegmentResult
from app.tasks.embedding_migration_v4_scope import _file_skipped_by_policy
from app.tasks.migration_pipeline import PreparedFile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bulk-write helper
# ---------------------------------------------------------------------------


def _bulk_write_v4_embeddings(speaker_docs: list[dict]) -> int:
    """Bulk-index a list of speaker embedding documents to the v4 index."""
    from app.services.opensearch_service import get_opensearch_client

    if not speaker_docs:
        return 0

    client = get_opensearch_client()
    if not client:
        logger.error("OpenSearch client unavailable for bulk write")
        return 0

    v4_index = get_speaker_index_v4()
    bulk_body: list[dict] = []
    for doc in speaker_docs:
        doc_id = doc.pop("_id")
        bulk_body.append({"index": {"_index": v4_index, "_id": doc_id}})
        bulk_body.append(doc)

    try:
        response = client.bulk(body=bulk_body)
        errors = response.get("errors", False)
        if errors:
            failed_count = 0
            for item in response.get("items", []):
                err = item.get("index", {}).get("error")
                if err:
                    logger.error(f"Bulk index error: {err}")
                    failed_count += 1
            return len(speaker_docs) - failed_count
        return len(speaker_docs)
    except Exception as e:
        logger.error(f"Bulk write to v4 index failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# Result writer for the unified pipeline
# ---------------------------------------------------------------------------


def _embedding_result_writer(
    prepared: PreparedFile,
    results_by_model: dict[str, list[SegmentResult]],
) -> int:
    """Aggregate embedding results and bulk-write to OpenSearch v4 index.

    This is the result_writer callback for process_batch_pipelined().
    """
    embedding_results = results_by_model.get("embedding", [])
    if not embedding_results:
        return 0

    # Privacy policy (#1280): the last stop before the write, asked fresh. Counted and
    # reported at finalize; the file still counts as processed so the migration can finish.
    if _file_skipped_by_policy(prepared.media_file_id):
        logger.info(
            "v4 migration: file %s skipped by privacy policy (no voiceprint written)",
            prepared.media_file_id,
        )
        return 0

    # Group embeddings by speaker
    speaker_embeddings: dict[int, list] = {}
    for sr in embedding_results:
        speaker_embeddings.setdefault(sr.speaker_id, []).append(sr.value)

    # Aggregate and build docs
    docs: list[dict] = []
    now = datetime.datetime.now(datetime.UTC).isoformat()
    speaker_profiles = prepared.extra.get("speaker_profiles", {})

    # Build speaker lookup for metadata
    speaker_by_id = {sp.id: sp for sp in prepared.speakers}

    for speaker_id, embs in speaker_embeddings.items():
        if not embs:
            continue

        # Use static aggregate method
        import numpy as np

        if len(embs) == 1:
            aggregated = embs[0]
            norm = np.linalg.norm(aggregated)
            if norm > 0:
                aggregated = aggregated / norm
        else:
            stacked = np.vstack(embs)
            aggregated = np.mean(stacked, axis=0)
            norm = np.linalg.norm(aggregated)
            if norm > 0:
                aggregated = aggregated / norm

        speaker = speaker_by_id.get(speaker_id)
        if not speaker:
            continue

        docs.append(
            {
                "_id": str(speaker.uuid),
                "speaker_id": speaker.id,
                "speaker_uuid": str(speaker.uuid),
                "profile_id": speaker.profile_id,
                "profile_uuid": speaker_profiles.get(speaker.id),
                "user_id": prepared.user_id,
                "name": speaker.name,
                "display_name": None,
                "collection_ids": [],
                "media_file_id": prepared.media_file_id,
                "segment_count": len(embs),
                "created_at": now,
                "updated_at": now,
                "embedding": aggregated.tolist(),
                # Org files only: a personal document carries no field (the personal gate).
                **(
                    {"organization_id": prepared.organization_id}
                    if prepared.organization_id is not None
                    else {}
                ),
            }
        )

    if docs:
        written = _bulk_write_v4_embeddings(docs)
        # Issue #657, defect 5: the return value used to be ignored here, so
        # a total bulk-write failure (_bulk_write_v4_embeddings returns 0 on
        # any exception, or on a client-unavailable early return) still
        # reported `len(docs)` written. process_batch_pipelined's caller
        # unconditionally calls on_file_success() after this returns, so
        # that file was marked done with NOTHING actually indexed. Raise
        # instead — process_batch_pipelined's except clause routes a raised
        # exception to on_file_failure(), which is the real outcome here.
        if written < len(docs):
            raise RuntimeError(
                f"v4 bulk write wrote {written}/{len(docs)} speaker embedding "
                f"document(s) for media_file_id={prepared.media_file_id}"
            )

    return len(docs)
