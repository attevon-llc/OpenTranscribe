"""Per-file processing: turn a file's speaker embeddings into matches or stored voiceprints."""

import logging
from typing import TYPE_CHECKING
from typing import Any

import numpy as np

from app.models.speaker import Speaker

if TYPE_CHECKING:
    from app.services.speaker_matching_service import SpeakerMatchingService

logger = logging.getLogger(__name__)


def process_speaker_segments(
    self: "SpeakerMatchingService",
    audio_path: str,
    media_file_id: int,
    user_id: int,
    segments: list[dict[str, Any]],
    speaker_mapping: dict[str, int],
    accessible_profile_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """
    Process speaker segments and match to profiles.

    Args:
        audio_path: Path to audio file
        media_file_id: Media file ID
        user_id: User ID
        segments: Transcript segments
        speaker_mapping: Mapping of speaker labels to IDs
        accessible_profile_ids: Optional set of accessible profile IDs for shared matching

    Returns:
        List of speaker match results
    """
    # Tenant scope for all stores/queries follows the file's org.
    organization_id = self._media_file_org_id(media_file_id)

    # Privacy policy (#1280): no embedding is even extracted for a scope that is off.
    if not self._voiceprints_allowed(user_id, organization_id):
        logger.info("Speaker matching for file %s skipped by privacy policy", media_file_id)
        return []

    # Extract embeddings for all speakers
    if self.embedding_service is None:
        raise ValueError("Embedding service is required for processing speaker segments")
    speaker_embeddings = self.embedding_service.extract_embeddings_for_segments(
        audio_path, segments, speaker_mapping
    )

    results = []

    for speaker_id, embeddings in speaker_embeddings.items():
        result = self._process_single_speaker(
            speaker_id,
            embeddings,
            user_id,
            media_file_id,
            accessible_profile_ids=accessible_profile_ids,
            organization_id=organization_id,
        )
        if result:
            results.append(result)

    self.db.commit()
    self._rename_tracker.flush(self.db)
    return results


def process_speaker_embeddings_native(
    self: "SpeakerMatchingService",
    media_file_id: int,
    user_id: int,
    native_embeddings: dict[int, np.ndarray],
    accessible_profile_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Process pre-computed native speaker embeddings (from PyAnnote centroids).

    Accepts already L2-normalized 256-dim WeSpeaker centroids keyed by
    database speaker ID.  Skips audio-based extraction entirely.

    Args:
        media_file_id: Media file ID.
        user_id: User ID.
        native_embeddings: Mapping of speaker DB ID -> L2-normalized embedding.
        accessible_profile_ids: Optional set of accessible profile IDs for shared matching.

    Returns:
        List of speaker match results.
    """
    # Tenant scope for all stores/queries follows the file's org.
    organization_id = self._media_file_org_id(media_file_id)

    # Privacy policy (#1280): the centroids are discarded, never matched or stored.
    if not self._voiceprints_allowed(user_id, organization_id):
        logger.info("Native speaker matching for file %s skipped by privacy policy", media_file_id)
        return []

    results = []
    for speaker_id, embedding in native_embeddings.items():
        result = self._process_single_speaker(
            speaker_id,
            [embedding],
            user_id,
            media_file_id,
            accessible_profile_ids=accessible_profile_ids,
            organization_id=organization_id,
        )
        if result:
            results.append(result)

    self.db.commit()
    self._rename_tracker.flush(self.db)
    return results


def process_single_speaker(
    self: "SpeakerMatchingService",
    speaker_id: int,
    embeddings: list[np.ndarray],
    user_id: int,
    media_file_id: int,
    accessible_profile_ids: set[int] | None = None,
    organization_id: int | None = None,
) -> dict[str, Any] | None:
    """
    Process a single speaker's embeddings and find matches.

    Args:
        speaker_id: Speaker ID
        embeddings: List of embeddings for this speaker
        user_id: User ID
        media_file_id: Media file ID
        organization_id: Tenant scope (the file's org; None = personal)

    Returns:
        Speaker processing result or None if failed
    """

    if not embeddings:
        logger.warning(f"No valid embeddings for speaker {speaker_id}")
        return None

    # A no-match result STORES the voiceprint, so the policy is asked again here rather
    # than trusting that every caller already did.
    if not self._voiceprints_allowed(user_id, organization_id):
        return None

    # Aggregate embeddings for this speaker
    try:
        if self.embedding_service is not None:
            aggregated_embedding = self.embedding_service.aggregate_embeddings(embeddings)
        elif len(embeddings) == 1:
            # Native path: single pre-computed centroid, already L2-normalized
            aggregated_embedding = embeddings[0]
        else:
            # Native path: multiple embeddings, inline mean + L2-normalize
            stacked = np.stack(embeddings)
            mean_vec = np.mean(stacked, axis=0)
            norm = np.linalg.norm(mean_vec)
            if norm < 1e-8:
                logger.warning(f"Zero mean embedding for speaker {speaker_id}")
                return None
            aggregated_embedding = mean_vec / norm
    except Exception as e:
        logger.error(f"Error aggregating embeddings for speaker {speaker_id}: {e}")
        return None

    speaker = self.db.query(Speaker).filter(Speaker.id == speaker_id).first()
    if not speaker:
        return None

    # Try to match to known speakers with display names
    match = self.match_speaker_to_known_speakers(
        aggregated_embedding,
        user_id,
        accessible_profile_ids=accessible_profile_ids,
        organization_id=organization_id,
    )

    if match:
        return self._handle_speaker_match(
            speaker,
            match,
            aggregated_embedding,
            user_id,
            media_file_id,
            organization_id=organization_id,
        )
    else:
        return self._handle_no_speaker_match(
            speaker,
            aggregated_embedding,
            user_id,
            media_file_id,
            organization_id=organization_id,
        )
