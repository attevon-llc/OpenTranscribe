"""Clearing a file's derived data ahead of a reprocess (whole transcript or per stage)."""

import logging

from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.services.speaker_cleanup import SpeakerImpact
from app.services.speaker_cleanup import capture_speaker_impact
from app.services.speaker_cleanup import reconcile_speaker_impact
from app.utils.speaker_identity_source import clear_suggestion

logger = logging.getLogger(__name__)


def _capture_file_speakers(db: Session, media_file_id: int) -> SpeakerImpact:
    """What deleting a file's speakers for a re-run leaves stale (issue #1214).

    The re-run indexes the file again itself, so no reindex is requested here.
    """
    from app.models.speaker import Speaker

    speaker_ids = [
        int(row[0]) for row in db.query(Speaker.id).filter(Speaker.media_file_id == media_file_id)
    ]
    return capture_speaker_impact(db, speaker_ids, removed=True, reindex=False)


def clear_existing_transcription_data(db: Session, media_file: MediaFile) -> None:
    """
    Clear existing transcription data for reprocessing.

    Args:
        db: Database session
        media_file: MediaFile to clear data for
    """
    try:
        # Clear transcript-related fields that exist on the MediaFile model
        media_file.summary_data = None  # type: ignore[assignment]
        media_file.summary_opensearch_id = None  # type: ignore[assignment]  # Clear OpenSearch summary ID for regeneration
        media_file.summary_status = "pending"  # type: ignore[assignment]  # Reset summary status for regeneration
        media_file.translated_text = None  # type: ignore[assignment]
        media_file.waveform_data = None  # type: ignore[assignment]  # Clear waveform data for regeneration

        # Clear existing transcript segments
        from app.models.media import Analytics
        from app.models.media import TranscriptSegment
        from app.models.speaker import Speaker
        from app.services.speaker_correction_carryover import stash_file_corrections

        # The user's corrections of these speakers are kept by the audio they judged,
        # read while the speakers and their segments still exist (issue #1233).
        stash_file_corrections(db, int(media_file.id))
        # Read before the deletes: their CASCADE forgets which clusters and profiles
        # averaged the speakers in.
        speaker_impact = _capture_file_speakers(db, int(media_file.id))

        # Bulk, like the speaker delete below: with autoflush off, per-row ORM deletes
        # were still pending when the speakers went, and the segments' foreign key
        # refused the speaker delete for any file that had speakers.
        db.query(TranscriptSegment).filter(TranscriptSegment.media_file_id == media_file.id).delete(
            synchronize_session=False
        )

        # Clear any existing speaker data.
        db.query(Speaker).filter(Speaker.media_file_id == media_file.id).delete(
            synchronize_session=False
        )

        # Clear any existing analytics data
        existing_analytics = (
            db.query(Analytics).filter(Analytics.media_file_id == media_file.id).first()
        )
        if existing_analytics:
            db.delete(existing_analytics)

        db.commit()

        # Prune the OpenSearch summary plane to match the now-cleared column
        # (issue #963) — a full transcription reprocess clears summary_data
        # unconditionally above and the new transcript may never gain a
        # fresh summary before this file is searched again.
        from app.tasks.search_file_field_tasks import index_file_summary

        index_file_summary.delay(int(media_file.id))

        logger.info(f"Cleared existing transcription data for file {media_file.id}")

        # Voiceprints, clusters, profiles: the shared speaker cleanup (issue #1214). A
        # residual is logged at ERROR there; the reprocess the user asked for goes ahead.
        reconcile_speaker_impact(db, speaker_impact)

    except Exception as e:
        logger.exception(f"Error clearing transcription data for file {media_file.id}: {e}")
        db.rollback()
        raise


def clear_selective_data(db: Session, media_file: MediaFile, stages: list[str]) -> None:
    """Clear data only for selected pipeline stages."""
    from app.models.media import Analytics
    from app.models.media import TranscriptSegment
    from app.models.speaker import Speaker
    from app.services.speaker_rename_tracker import SpeakerRenameTracker
    from app.utils.speaker_labels import canonical_speaker_label_for_row

    # Only the "speaker_llm" branch below writes to it — declared here so the
    # `except` path can safely `discard()` regardless of which branch ran.
    tracker = SpeakerRenameTracker()
    rediarize_impact: SpeakerImpact | None = None

    try:
        if "transcription" in stages:
            # Full transcription clear - use existing function
            clear_existing_transcription_data(db, media_file)
            return  # Already clears everything

        if "rediarize" in stages:
            from app.services.speaker_correction_carryover import stash_file_corrections

            # The user's corrections of these speakers are kept by the audio they judged
            # (issue #1233) — read FIRST, while the segments still name their speakers.
            stash_file_corrections(db, int(media_file.id))
            # Null out speaker_id on segments first to avoid FK constraint violations,
            # then delete speakers. Transcript text/words are preserved.
            db.query(TranscriptSegment).filter(
                TranscriptSegment.media_file_id == media_file.id
            ).update({TranscriptSegment.speaker_id: None}, synchronize_session=False)

            rediarize_impact = _capture_file_speakers(db, int(media_file.id))
            db.query(Speaker).filter(Speaker.media_file_id == media_file.id).delete(
                synchronize_session=False
            )
            # Also clear analytics since speaker stats change
            existing_analytics = (
                db.query(Analytics).filter(Analytics.media_file_id == media_file.id).first()
            )
            if existing_analytics:
                db.delete(existing_analytics)

        if "analytics" in stages:
            existing_analytics = (
                db.query(Analytics).filter(Analytics.media_file_id == media_file.id).first()
            )
            if existing_analytics:
                db.delete(existing_analytics)

        if "speaker_llm" in stages:
            # Reset suggested names on speakers. Nulling a confident suggestion
            # moves the canonical label back to the raw diarizer name with no
            # `display_name` write at all — one of five writers with zero
            # chunk-plane dispatch until issue #605 (the chunk plane kept
            # offering the cleared suggestion in the search facet and chat's
            # speaker scope indefinitely). Per-speaker "after" values differ
            # (each speaker's raw name), so this batches through the tracker
            # rather than a single dispatch call.
            existing_speakers = (
                db.query(Speaker).filter(Speaker.media_file_id == media_file.id).all()
            )
            for speaker in existing_speakers:
                before = canonical_speaker_label_for_row(speaker)
                clear_suggestion(speaker)
                tracker.record(int(media_file.id), before, canonical_speaker_label_for_row(speaker))

        if "summarization" in stages:
            media_file.summary_data = None
            media_file.summary_opensearch_id = None
            media_file.summary_status = "pending"

        if "topic_extraction" in stages:
            # Clear AI-generated topic suggestions
            from app.models.topic import TopicSuggestion

            existing_suggestions = (
                db.query(TopicSuggestion)
                .filter(TopicSuggestion.media_file_id == media_file.id)
                .all()
            )
            for suggestion in existing_suggestions:
                db.delete(suggestion)

        db.commit()
        tracker.flush(db)
        if rediarize_impact is not None:
            # After the commit, never before: a rolled-back clear must keep its voices.
            reconcile_speaker_impact(db, rediarize_impact)

        if "summarization" in stages:
            # Prune the OpenSearch summary plane to match the now-cleared
            # column (issue #963).
            from app.tasks.search_file_field_tasks import index_file_summary

            index_file_summary.delay(int(media_file.id))

        logger.info(f"Cleared selective data for stages {stages} on file {media_file.id}")
    except Exception as e:
        logger.exception(f"Error clearing selective data for file {media_file.id}: {e}")
        db.rollback()
        tracker.discard()
        raise
