"""Digest-plane regeneration after a speaker rename (#383 addendum-G1).

Split out of ``rename_propagation_task``: the chunk-plane rewrites and the
dispatch entry point stay there; this module holds the batched Celery task that
rebuilds ``file_facts`` and the digest/summary planes for renamed files.
``rename_propagation_task._dispatch_digest_regeneration`` queues it.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from app.core.celery import celery_app
from app.core.queues import CPUPriority

logger = logging.getLogger(__name__)


# Bounds a bulk rename / profile merge to a fixed number of digest-regeneration
# Celery tasks rather than one per file. A cluster promotion or a batch-verify
# can touch hundreds of files in one pass (`SpeakerClusteringService`); without
# this a single user action would fan out hundreds of tiny CPU-queue tasks,
# each paying its own DB round trip and OpenSearch bulk call for one document.
_DIGEST_REGEN_BATCH_SIZE = 20


@celery_app.task(
    bind=True,
    name="regenerate_rename_digests",
    priority=CPUPriority.USER_TRIGGERED,
    max_retries=2,
    default_retry_delay=15,
)
def regenerate_rename_digests(
    self: Any,
    file_uuids: list[str],
    new_name: str | None = None,
    speaker_id: int | None = None,
) -> dict[str, Any]:
    """Regenerate ``file_facts`` and the digest-plane documents for renamed files.

    The #383 addendum-G1 trigger this module's docstring points at. A rename's
    fingerprint self-invalidates the ``file_facts`` row the moment the new name
    lands in Postgres (``source_fingerprint`` covers the *resolved* speaker
    display name — ``services/ingest_artifacts/service.py``), so nothing about
    the regeneration logic itself needed to change. What was missing was a
    dispatch site: nothing queued the regeneration, so a renamed speaker's
    digest prose stayed stale until an unrelated full reindex happened to touch
    the file.

    ⚠️ **Digest plane only — this never touches the chunk plane's vectors.**
    ``propagate_speaker_rename`` documents a deliberate trade: re-embedding a
    renamed file's chunks costs hundreds of model calls for a cosmetic edit, so
    the chunk vector is left stale on purpose and only the roster/keyword
    fields are rewritten. Reaching the digest via the full
    ``TranscriptIndexingService.index_transcript_chunks`` would silently undo
    that trade for every rename (it re-chunks and re-embeds everything).
    ``_index_digest_plane`` is the one place that already does exactly
    "regenerate ``file_facts`` via the fingerprint short-circuit, then reindex
    the digest documents" (its own docstring, addendum G1) — called directly
    here rather than reimplemented, per this repo's one-implementation rule.

    Args:
        file_uuids: Media file UUIDs whose renamed speaker(s) invalidated the
            file's digest. This task IS the batching unit
            ``_dispatch_digest_regeneration`` uses to bound a bulk rename or
            profile merge to a fixed number of Celery tasks rather than one
            per file — see ``_DIGEST_REGEN_BATCH_SIZE``.
        new_name: The display name the rename applied, carried through only so
            the completion WebSocket message can say what changed.
        speaker_id: Postgres id of the renamed speaker, same reason.

    Returns:
        Dict with counts. Never raises: a digest is derived enrichment, and the
        rename that triggered this has already committed and already
        propagated to the chunk plane regardless of what happens here.
    """
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.services.search.embedding_provenance import active_embedding_model
    from app.services.search.indexing_service import TranscriptIndexingService
    from app.services.search.pipelines import is_neural_pipeline_available
    from app.tasks.search_indexing_task import extract_file_index_metadata
    from app.utils.websocket_notify import send_ws_event_for_file

    uuids = sorted({str(u) for u in (file_uuids or []) if u})
    if not uuids:
        return {"status": "skipped", "reason": "no_files", "regenerated": 0, "errors": 0}

    indexing_service = TranscriptIndexingService()
    use_neural = is_neural_pipeline_available()
    provenance = active_embedding_model() if use_neural else None
    now = datetime.datetime.now(datetime.UTC).isoformat()

    regenerated_uuids: list[str] = []
    errors = 0
    owners: dict[int, list[str]] = {}

    for file_uuid in uuids:
        try:
            with session_scope() as db:
                media_file = db.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()
                if media_file is None:
                    continue
                file_id = int(media_file.id)
                user_id = int(media_file.user_id)
                meta = extract_file_index_metadata(db, media_file, file_id)

            base_metadata: dict[str, Any] = {
                "user_id": user_id,
                "title": meta["title"],
                "tags": meta["tag_names"],
                "upload_time": meta["upload_time"],
                "language": meta["language"],
                "content_type": meta["content_type"],
                "duration": meta["duration"],
                "file_size": meta["file_size"],
                "collection_ids": meta["collection_ids"],
                "accessible_user_ids": meta["accessible_user_ids"] or [user_id],
                "indexed_at": now,
                "embedding_model": provenance,
                **(
                    {}
                    if meta.get("organization_id") is None
                    else {"organization_id": meta["organization_id"]}
                ),
            }

            indexing_service._index_digest_plane(
                file_id=file_id,
                file_uuid=file_uuid,
                base_metadata=base_metadata,
                use_neural=use_neural,
            )
            # The summary plane carries the same `speakers` roster, and the summary leg
            # filters on it (#1188); a stale roster makes a speaker-filtered search match
            # the old name. Rebuilt from Postgres, so it sees the committed rename.
            indexing_service._index_summary_plane(
                file_id=file_id,
                file_uuid=file_uuid,
                base_metadata=base_metadata,
                use_neural=use_neural,
            )
            regenerated_uuids.append(file_uuid)
            owners.setdefault(user_id, []).append(file_uuid)
        except Exception as exc:  # noqa: BLE001 — a digest miss must not fail a rename
            logger.warning(f"Could not regenerate digest for file {file_uuid} after rename: {exc}")
            errors += 1

    logger.info(
        f"Rename-triggered digest regeneration: {len(regenerated_uuids)} file(s) refreshed, "
        f"{errors} error(s), {len(uuids)} requested"
    )

    # One notification per owner, not per file — a bulk rename/profile merge
    # touches many files at once and the speakers page needs one "propagation
    # finished" signal to invalidate its cache and clear a progress indicator,
    # not one toast per file.
    from app.services.takedown_service import filter_suppressed_file_uuids

    for user_id, files_for_user in owners.items():
        try:
            # A bulk rename/profile merge can span files this recipient no longer
            # sees (one of them may have been quarantined between the rename commit
            # and this background regeneration). The payload's `file_uuids` list AND
            # the `regenerated` count it feeds into the "N files updated" toast must
            # both reflect only what this recipient may see (issue #908) — the
            # `regenerated` count is recomputed from the filtered list, never the
            # original, so the number itself cannot disclose that a hidden file was
            # touched. An all-quarantined batch sends nothing at all.
            visible = filter_suppressed_file_uuids(files_for_user, user_id)
            if not visible:
                continue
            send_ws_event_for_file(
                user_id=user_id,
                notification_type="speaker_rename_propagation",
                data={
                    "status": "completed",
                    "new_name": new_name,
                    "speaker_id": speaker_id,
                    "file_uuids": visible,
                    "regenerated": len(visible),
                    "errors": errors,
                    "total": len(uuids),
                },
                file_uuids=visible,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, matches every sibling dispatch here
            logger.warning(
                f"Could not send rename-propagation WS notification to user {user_id}: {exc}"
            )

    return {
        "status": "success",
        "regenerated": len(regenerated_uuids),
        "errors": errors,
        "total": len(uuids),
    }
