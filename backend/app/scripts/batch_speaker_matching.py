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

import numpy as np

from app.db.base import get_db
from app.models.media import Speaker
from app.models.media import SpeakerMatch
from app.services.opensearch_service import get_speaker_embedding
from app.services.speaker_embedding_service import SpeakerEmbeddingService
from app.services.speaker_matching_service import SpeakerMatchingService
from app.services.speaker_rename_tracker import SpeakerRenameTracker
from app.utils.speaker_labels import canonical_speaker_label_for_row

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def batch_process_speaker_matches():
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

        # Get all speakers with embeddings
        speakers = db.query(Speaker).all()

        logger.info(f"Processing {len(speakers)} speakers for matches...")

        processed = 0
        matches_found = 0

        for speaker in speakers:
            # Get embedding from OpenSearch
            embedding = get_speaker_embedding(speaker.id)

            if embedding:
                # Find and store matches
                found_matches = matching_service.find_and_store_speaker_matches(
                    speaker.id, np.array(embedding), speaker.user_id, threshold=0.5
                )

                if found_matches:
                    matches_found += len(found_matches)
                    logger.info(
                        f"Found {len(found_matches)} matches for speaker {speaker.id} ({speaker.name})"
                    )

                    # Update suggested name if high confidence match found
                    for match in found_matches:
                        if (
                            match["confidence"] >= 0.75
                            and match["display_name"]
                            and not speaker.suggested_name
                        ):
                            # Captured before the write (issue #605) — a
                            # confident suggestion moves the canonical chunk
                            # label on its own, with no `display_name` write.
                            before = canonical_speaker_label_for_row(speaker)
                            speaker.suggested_name = match["display_name"]
                            speaker.confidence = match["confidence"]
                            speaker.suggestion_source = "voice_match"
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

        # Show some statistics
        match_count = db.query(SpeakerMatch).count()
        logger.info(f"Total speaker matches in database: {match_count}")

    except Exception as e:
        logger.error(f"Error in batch processing: {e}")
        db.rollback()
        tracker.discard()
    finally:
        db.close()


if __name__ == "__main__":
    batch_process_speaker_matches()
