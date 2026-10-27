"""Rebuild the planes that carry a voice-matched speaker NAME when a voiceprint switch changes.

A speaker label printed into a search chunk, a digest or a facts roster comes from
``utils.speaker_labels.canonical_speaker_label_for_row``, which leaves a voiceprint-derived name
out while the file's voiceprints are off (plan D5) and puts it back when they are on again. The
documents already in the index were written under the old answer, so a switch change has to
re-index the files whose label that suggestion decides, or the stale name keeps being searchable
(and shown in results and chat) until some unrelated mutation touches the file.

This module only SELECTS those files and hands them to the existing debounced dispatch
(``search/reindex_dispatch.queue_transcript_reindex`` plus ``dispatch_file_facts``, the pair
``speaker_label_backfill`` uses); it owns no queue of its own. The re-index reads the policy when
it RUNS, so a switch flipped back before it fires rebuilds the same labels and costs nothing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import exists
from sqlalchemy import or_

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Speaker
from app.utils.speaker_labels import DEFAULT_SUGGESTION_CONFIDENCE_THRESHOLD
from app.utils.speaker_labels import EMBEDDING_SUGGESTION_SOURCES

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def files_labelled_by_voiceprints(
    db: Session, *, user_id: int | None = None, organization_id: int | None = None
) -> list[tuple[int, str, int]]:
    """``(id, uuid, owner)`` of completed files with a speaker named only by a voiceprint match.

    Args:
        db: Session, read only.
        user_id: Limit to the files this person owns (their own switch changed).
        organization_id: Limit to the files stamped with this organization (its switch changed).
            Both omitted: every file (the instance switch changed).
    """
    voice_named = exists().where(
        Speaker.media_file_id == MediaFile.id,
        Speaker.suggestion_source.in_(sorted(EMBEDDING_SUGGESTION_SOURCES)),
        Speaker.verified.isnot(True),
        Speaker.suggested_name.isnot(None),
        Speaker.suggested_name != "",
        Speaker.confidence >= DEFAULT_SUGGESTION_CONFIDENCE_THRESHOLD,
        or_(Speaker.display_name.is_(None), Speaker.display_name == ""),
    )
    query = db.query(MediaFile.id, MediaFile.uuid, MediaFile.user_id).filter(
        MediaFile.status == FileStatus.COMPLETED, voice_named
    )
    if user_id is not None:
        query = query.filter(MediaFile.user_id == user_id)
    if organization_id is not None:
        query = query.filter(MediaFile.organization_id == organization_id)
    return [(int(r[0]), str(r[1]), int(r[2])) for r in query.order_by(MediaFile.id).all()]


def queue_label_reindex(
    db: Session, *, user_id: int | None = None, organization_id: int | None = None
) -> int:
    """Queue a debounced re-index for each file the changed switch can relabel.

    Never raises: the switch change has already committed and must not turn into a failed
    request because a broker was down. A file that could not be queued is logged; the next
    mutation of it, or a reindex, rebuilds it.

    Returns:
        The number of files for which a re-index is queued or already pending.
    """
    from app.services.search.reindex_dispatch import ReindexDispatch
    from app.services.search.reindex_dispatch import queue_transcript_reindex
    from app.tasks.ingest_artifacts_task import dispatch_file_facts

    try:
        files = files_labelled_by_voiceprints(db, user_id=user_id, organization_id=organization_id)
    except Exception:
        logger.exception("privacy: could not list the files a voiceprint switch relabels")
        return 0
    handled = 0
    for file_id, file_uuid, owner_id in files:
        result = queue_transcript_reindex(file_id=file_id, file_uuid=file_uuid, user_id=owner_id)
        if result is ReindexDispatch.FAILED:
            logger.warning("privacy: could not queue the re-index of file %s", file_uuid)
            continue
        handled += 1
        # The facts roster fingerprints the resolved speaker label, so a changed label
        # regenerates it (the same pairing the provenance backfill uses).
        dispatch_file_facts(file_id)
    logger.info("privacy: queued %d of %d file(s) for relabelling", handled, len(files))
    return handled
