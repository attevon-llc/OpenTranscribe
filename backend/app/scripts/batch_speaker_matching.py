#!/usr/bin/env python3
"""
Batch process existing speakers to find and store cross-video matches.
This script can be run manually to process speakers that were added before
the matching system was implemented.
"""

import sys

# Add the app directory to Python path
sys.path.insert(0, "/app")

import logging
from typing import Any

import numpy as np
from sqlalchemy import ColumnElement
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import SPEAKER_CONFIDENCE_MEDIUM
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerMatch
from app.services.embedding_mode_service import voiceprint_model_id
from app.services.opensearch_service import get_speaker_embedding
from app.services.privacy.purge_file_guard import unprotected_file_pred
from app.services.privacy.redact_view import PrivacyView
from app.services.speaker_embedding_service import SpeakerEmbeddingService
from app.services.speaker_identity_rules import blocked_profile_names
from app.services.speaker_matching_service import SpeakerMatchingService
from app.services.speaker_rename_tracker import SpeakerRenameTracker
from app.utils.speaker_identity_detail import ORIGIN_VOICE_MATCH
from app.utils.speaker_identity_detail import voiceprint_detail
from app.utils.speaker_identity_source import record_suggestion
from app.utils.speaker_labels import canonical_speaker_label_for_row

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _on_unprotected_file() -> ColumnElement[bool]:
    """``Speaker`` of a file neither held nor quarantined (``purge_file_guard``)."""
    return Speaker.media_file_id.in_(select(MediaFile.id).where(unprotected_file_pred()))


def speakers_to_match(db: Session) -> tuple[list[Speaker], int]:
    """The speakers this run may match, and how many it leaves out because their file is frozen.

    A legal hold or a quarantine freezes a file's speakers as they stand (``hold_guard``): no match
    record or name suggestion is written for them.
    """
    on_unprotected = _on_unprotected_file()
    skipped = int(db.query(Speaker).filter(~on_unprotected).count())
    return db.query(Speaker).filter(on_unprotected).all(), skipped


def still_unprotected(db: Session, speaker: Speaker) -> bool:
    """Whether the speaker's file is still neither held nor quarantined (a hold can land mid-run)."""
    return bool(
        db.query(Speaker.id).filter(Speaker.id == speaker.id, _on_unprotected_file()).first()
    )


def batch_process_speaker_matches() -> None:
    """Process all speakers to find and store matches."""
    db = next(get_db())

    # Accumulates chunk-plane label changes from the `suggested_name`/`confidence`
    # writes below — a suggestion crossing the canonical-label threshold (0.75)
    # moves what the chunk index would write with no `display_name` write at all
    # (issue #605), so this must be tracked the same way the other writers of
    # these fields are. Flushed after each commit, so a rolled-back batch never
    # reaches the index.
    tracker = SpeakerRenameTracker()

    try:
        # Initialize services
        embedding_service = SpeakerEmbeddingService()
        matching_service = SpeakerMatchingService(db, embedding_service)

        speakers, skipped_protected = speakers_to_match(db)

        logger.info(f"Processing {len(speakers)} speakers for matches...")

        processed = 0
        matches_found = 0
        skipped_by_policy = 0
        lookup_failures = 0

        # Privacy policy (#1280): a speaker whose file's owner or organization has voiceprints
        # off is not matched, and no match record or suggestion is written for it.
        privacy = PrivacyView(db)
        privacy.preload_files(int(s.media_file_id) for s in speakers)

        for speaker in speakers:
            if not still_unprotected(db, speaker):
                skipped_protected += 1
                continue
            if not privacy.voiceprints(int(speaker.media_file_id)).enabled:
                skipped_by_policy += 1
                continue

            # Get embedding from OpenSearch
            embedding = get_speaker_embedding(str(speaker.uuid))

            if embedding:
                # Find and store matches
                diagnostics: dict[str, Any] = {}
                found_matches = matching_service.find_and_store_speaker_matches(
                    speaker.id,
                    np.array(embedding),
                    speaker.user_id,
                    threshold=SPEAKER_CONFIDENCE_MEDIUM,
                    diagnostics=diagnostics,
                )
                if diagnostics.get("opensearch_failed"):
                    lookup_failures += 1

                if found_matches:
                    matches_found += len(found_matches)
                    logger.info(
                        f"Found {len(found_matches)} matches for speaker {speaker.id} ({speaker.name})"
                    )

                    # Update suggested name if high confidence match found — never one
                    # the user unassigned this speaker from (issue #1233).
                    refused = blocked_profile_names(db, [int(speaker.id)])[int(speaker.id)]
                    for match in found_matches:
                        if (
                            match["confidence"] >= 0.75
                            and match["display_name"]
                            and not speaker.suggested_name
                            and str(match["display_name"]).casefold() not in refused
                        ):
                            # Captured before the write (issue #605) — a
                            # confident suggestion moves the canonical chunk
                            # label on its own, with no `display_name` write.
                            before = canonical_speaker_label_for_row(speaker)
                            if not record_suggestion(
                                speaker,
                                source="voice_match",
                                name=match["display_name"],
                                confidence=match["confidence"],
                                detail=voiceprint_detail(
                                    ORIGIN_VOICE_MATCH,
                                    confidence=match["confidence"],
                                    threshold=0.75,
                                    matched_speaker_uuid=match.get("speaker_uuid"),
                                    embedding_model=voiceprint_model_id(),
                                    origin_task="batch_speaker_matching",
                                ),
                            ):
                                break  # a person verified this speaker: no voice suggestion
                            tracker.record(
                                int(speaker.media_file_id) if speaker.media_file_id else None,
                                before,
                                canonical_speaker_label_for_row(speaker),
                            )
                            db.flush()
                            break

                processed += 1

                if processed % 10 == 0:
                    logger.info(f"Processed {processed}/{len(speakers)} speakers...")
                    db.commit()
                    tracker.flush(db)

        db.commit()
        tracker.flush(db)

        logger.info("Batch processing complete!")
        logger.info(f"Processed: {processed} speakers")
        logger.info(f"Matches found: {matches_found}")
        logger.info(f"Skipped by privacy policy: {skipped_by_policy} speakers")
        logger.info(f"Skipped, file held or quarantined: {skipped_protected} speakers")
        if lookup_failures:
            logger.error(
                f"OpenSearch lookup FAILED for {lookup_failures} speakers (not 'no match')"
            )

        # Show some statistics
        match_count = db.query(SpeakerMatch).count()
        logger.info(f"Total speaker matches in database: {match_count}")

    except Exception:
        # Script boundary: undo the half-done batch, then let the process fail so a cron or
        # operator sees a non-zero exit instead of a "successful" run that matched nothing.
        logger.exception("Error in batch processing")
        db.rollback()
        tracker.discard()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    batch_process_speaker_matches()
