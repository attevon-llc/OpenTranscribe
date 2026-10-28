"""One-time correction of speaker labels that came from an LLM suggestion.

Before the owner decision of 2026-10-12, ``canonical_speaker_label`` treated any
``suggested_name`` at confidence >= 0.75 as the speaker's name, whoever produced it. A speaker
the LLM speaker-ID task had *guessed* at was therefore indexed (chunks, digests, summaries,
facts) under the guess. The rule now accepts a suggestion only when a voiceprint match produced
it (``utils/speaker_labels.EMBEDDING_SUGGESTION_SOURCES``), so those documents hold a label
Postgres no longer resolves to.

A row with NO source is selected too. Migration ``v439`` has already given its voiceprint
source to every legacy row that can prove one, so what is left here is a suggestion of unknown
provenance, and its label correctly falls back to the diarizer's.

``search_index_maintenance`` only finds files with NO chunks, so it cannot see a merely-wrong
value (``api/CLAUDE.md``); this module is the arm that does, wired into that same task
(``search_maintenance_task._run_search_maintenance``) rather than into a mechanism of its own.

It is bounded and resumable, like the ``file_facts`` backfill beside it: each tick handles
``BATCH_SIZE`` files past a persisted id cursor, and a ``SystemSettings`` flag records
completion so a finished deployment pays one flag read per tick. The condition that selects a
file (an LLM suggestion that used to count) never clears by itself -- the suggestion legitimately
stays on the speaker for the UI -- so the cursor, not the query, is what makes the arm finite.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import exists
from sqlalchemy import or_
from sqlalchemy import select

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.services.system_settings_service import get_setting
from app.services.system_settings_service import get_setting_int
from app.services.system_settings_service import set_setting
from app.utils.speaker_labels import DEFAULT_SUGGESTION_CONFIDENCE_THRESHOLD
from app.utils.speaker_labels import EMBEDDING_SUGGESTION_SOURCES

logger = logging.getLogger(__name__)

#: Files corrected per maintenance tick. Each costs a full re-index (embeddings), so the cap is
#: what keeps a large library from flooding the cpu queue in one beat tick.
BATCH_SIZE = 100

COMPLETE_FLAG_KEY = "speaker_label_provenance_backfill_complete"
CURSOR_KEY = "speaker_label_provenance_backfill_after_id"


def _stale_speaker_clause() -> Any:
    """Speakers whose old canonical label was a non-voiceprint suggestion."""
    return (
        Speaker.suggested_name.isnot(None),
        Speaker.suggested_name != "",
        Speaker.confidence >= DEFAULT_SUGGESTION_CONFIDENCE_THRESHOLD,
        or_(Speaker.display_name.is_(None), Speaker.display_name == ""),
        or_(
            Speaker.suggestion_source.is_(None),
            Speaker.suggestion_source.notin_(sorted(EMBEDDING_SUGGESTION_SOURCES)),
        ),
    )


def backfill_llm_suggested_labels(db: Any, stats: dict[str, Any]) -> None:
    """Re-index (and refresh the persisted display name of) the next batch of affected files.

    Args:
        db: Open session; the cursor/flag writes commit with the caller's scope.
        stats: The maintenance stats dict, annotated in place with
            ``speaker_label_backfill`` (``"complete"`` or the number of files queued this tick).
    """
    if get_setting(db, COMPLETE_FLAG_KEY) == "true":
        stats["speaker_label_backfill"] = "complete"
        return

    from app.services.search.reindex_dispatch import ReindexDispatch
    from app.services.search.reindex_dispatch import queue_transcript_reindex
    from app.services.speaker_status_service import SpeakerStatusService
    from app.tasks.ingest_artifacts_task import dispatch_file_facts

    after_id = get_setting_int(db, CURSOR_KEY, 0)
    has_segments = exists(
        select(TranscriptSegment.id).where(TranscriptSegment.media_file_id == MediaFile.id)
    )
    has_stale_speaker = exists(
        select(Speaker.id).where(Speaker.media_file_id == MediaFile.id, *_stale_speaker_clause())
    )
    files = (
        db.query(MediaFile.id, MediaFile.uuid, MediaFile.user_id)
        .filter(
            MediaFile.id > after_id,
            MediaFile.status == FileStatus.COMPLETED,
            has_segments,
            has_stale_speaker,
        )
        .order_by(MediaFile.id)
        .limit(BATCH_SIZE)
        .all()
    )
    if not files:
        set_setting(
            db,
            COMPLETE_FLAG_KEY,
            "true",
            "One-time re-index of files whose speaker label came from an LLM suggestion "
            "finished (owner decision 2026-10-12)",
        )
        stats["speaker_label_backfill"] = "complete"
        logger.info("speaker label backfill complete -- flag set, will not run again")
        return

    handled_through = after_id
    queued = 0
    for file_id, file_uuid, user_id in files:
        # The persisted column the Speaker API echoes was written from the old rule. It
        # describes the data, so it is policy-free (voiceprint_names=True): hiding is decided
        # per read, never written down (#1280 D5).
        for speaker in (
            db.query(Speaker).filter(Speaker.media_file_id == file_id, *_stale_speaker_clause())
        ).all():
            speaker.resolved_display_name = SpeakerStatusService.compute_speaker_status(
                speaker, voiceprint_names=True
            )["resolved_display_name"]
        result = queue_transcript_reindex(
            file_id=int(file_id), file_uuid=str(file_uuid), user_id=int(user_id)
        )
        if result is ReindexDispatch.FAILED:
            # Stop here rather than skip it: the cursor stays before this file, so the next
            # tick retries it instead of leaving a wrong label that nothing will revisit.
            logger.warning("speaker label backfill: could not queue file %s; will retry", file_uuid)
            break
        queued += 1
        handled_through = int(file_id)
        # Facts fingerprint the resolved speaker, so a changed label regenerates them.
        dispatch_file_facts(int(file_id))
    if handled_through != after_id:
        set_setting(
            db,
            CURSOR_KEY,
            str(handled_through),
            "Resume cursor of the speaker-label provenance backfill",
        )
    stats["speaker_label_backfill"] = queued
    logger.info("speaker label backfill: queued %d of %d file(s)", queued, len(files))
