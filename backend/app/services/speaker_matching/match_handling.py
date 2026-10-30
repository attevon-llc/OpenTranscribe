"""Applying the outcome of matching one speaker: a match was found, or none was."""

import logging
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any

import numpy as np

from app.core.constants import SPEAKER_CONFIDENCE_MEDIUM
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.services import embedding_mode_service
from app.services.opensearch_service import add_speaker_embedding
from app.services.speaker_matching.thresholds import MATCHING_ORIGIN_TASK
from app.services.speaker_matching.thresholds import ConfidenceLevel
from app.utils.speaker_identity_detail import ORIGIN_VOICE_MATCH
from app.utils.speaker_identity_detail import voiceprint_detail
from app.utils.speaker_identity_source import record_machine_identity
from app.utils.speaker_identity_source import record_suggestion
from app.utils.speaker_labels import canonical_speaker_label_for_row

if TYPE_CHECKING:
    from app.services.speaker_matching_service import SpeakerMatchingService

logger = logging.getLogger(__name__)


def _apply_auto_accept(
    speaker: Speaker, match: dict[str, Any], match_detail: dict[str, Any]
) -> None:
    """Give the speaker the matched identity; a voiceprint wrote it, not a person."""
    logger.info(f"Auto-accepting match for speaker {int(speaker.id)} -> {match['suggested_name']}")
    previous_display_name = speaker.display_name
    speaker.display_name = match["suggested_name"]
    speaker.verified = True  # type: ignore[assignment]
    # Assign to profile if this match came from a profile
    if match.get("profile_id"):
        speaker.profile_id = match["profile_id"]
    # A voiceprint wrote this identity, not a person (issue #1280): recorded before the
    # tracker computes the "after" label, which depends on it while voiceprints are off.
    record_machine_identity(
        speaker, previous_display_name=previous_display_name, detail=match_detail
    )


def _store_voiceprint_and_matches(
    self: "SpeakerMatchingService",
    speaker: Speaker,
    match: dict[str, Any],
    aggregated_embedding: np.ndarray,
    user_id: int,
    media_file_id: int,
    organization_id: int | None,
) -> None:
    """Store the speaker's voiceprint FIRST, then find and store its matches with others."""
    add_speaker_embedding(
        speaker_id=int(speaker.id),
        speaker_uuid=str(speaker.uuid),
        user_id=user_id,
        name=str(speaker.name),
        embedding=aggregated_embedding.tolist(),
        profile_id=int(speaker.profile_id) if speaker.profile_id else None,
        profile_uuid=str(speaker.profile.uuid) if speaker.profile else None,
        display_name=str(speaker.display_name) if speaker.display_name else match["suggested_name"],
        media_file_id=media_file_id,
        organization_id=organization_id,
    )

    # Find and store matches with other speakers
    self.find_and_store_speaker_matches(
        int(speaker.id),
        aggregated_embedding,
        user_id,
        threshold=SPEAKER_CONFIDENCE_MEDIUM,
        organization_id=organization_id,
    )


def _blend_into_profile_centroid(
    self: "SpeakerMatchingService", speaker: Speaker, aggregated_embedding: np.ndarray
) -> None:
    """Weighted incremental average of the new voice into the profile's stored centroid."""
    from app.services.opensearch_service import get_profile_embedding
    from app.services.opensearch_service import store_profile_embedding

    profile = self.db.query(SpeakerProfile).filter(SpeakerProfile.id == speaker.profile_id).first()
    if not profile:
        return
    # Weighted incremental average with existing profile embedding.
    #
    # The count is DERIVED from the profile's linked speakers,
    # never read back from the column and incremented. The old
    # `count = profile.embedding_count; count += 1` grew
    # monotonically on every auto-accept, including re-matches of
    # a speaker already linked to this profile — which is what a
    # reprocess produces. Observed on the dev stack: a profile
    # reading `embedding_count = 7` against **3** linked speakers.
    # Because the blend weights the stored centroid by `count`,
    # an inflated count silently under-weights new voice evidence
    # (here by >2x) and the profile freezes. Deriving it also
    # makes this path converge on the authoritative definition in
    # `profile_embedding_service._process_profile_with_embeddings`
    # (`embedding_count = len(embeddings)`) instead of drifting
    # away from it until an admin runs a full recalculation.
    existing = get_profile_embedding(str(profile.uuid))
    linked_count = self._linked_speaker_count(int(profile.id))
    # `speaker.profile_id` was assigned and flushed above, so the
    # count already includes this speaker. The stored centroid
    # represents the others.
    prior_count = max(linked_count - 1, 0)
    count = max(linked_count, 1)
    new_emb = aggregated_embedding.tolist()
    if existing and prior_count > 0:
        old = np.array(existing)
        added = np.array(new_emb)
        merged = (old * prior_count + added) / (prior_count + 1)
        norm = np.linalg.norm(merged)
        if norm > 0:
            merged = merged / norm
        new_emb = merged.tolist()
    store_profile_embedding(
        profile_id=int(profile.id),
        profile_uuid=str(profile.uuid),
        profile_name=str(profile.name),
        embedding=new_emb,
        speaker_count=count,
        user_id=int(profile.user_id),
        # Profile docs mirror the profile ROW's tenant scope
        organization_id=profile.organization_id,
    )
    profile.embedding_count = count  # type: ignore[assignment]
    profile.last_embedding_update = datetime.now(UTC)  # type: ignore[assignment]
    self.db.commit()


def _sync_profile_to_index(self: "SpeakerMatchingService", speaker: Speaker) -> None:
    """Sync profile assignment to OpenSearch after the document has been created/updated."""
    from app.services.opensearch_service import update_speaker_profile

    profile_uuid = None
    if speaker.profile_id:
        profile = (
            self.db.query(SpeakerProfile).filter(SpeakerProfile.id == speaker.profile_id).first()
        )
        if profile:
            profile_uuid = str(profile.uuid)

    update_speaker_profile(
        speaker_uuid=str(speaker.uuid),
        profile_id=int(speaker.profile_id) if speaker.profile_id else None,
        profile_uuid=profile_uuid,
        verified=bool(speaker.verified),
    )


def handle_speaker_match(
    self: "SpeakerMatchingService",
    speaker: Speaker,
    match: dict[str, Any],
    aggregated_embedding: np.ndarray,
    user_id: int,
    media_file_id: int,
    organization_id: int | None = None,
) -> dict[str, Any]:
    """
    Handle case where speaker match is found.

    Args:
        speaker: Speaker object
        match: Match result dictionary
        aggregated_embedding: Aggregated embedding
        user_id: User ID
        media_file_id: Media file ID
        organization_id: Tenant scope (the file's org; None = personal).
            Stamped on the per-file speaker doc and used to gate the
            follow-up match queries.

    Returns:
        Speaker processing result
    """
    # Captured before ANY write below (issue #605) — a suggestion crossing
    # the confidence threshold moves the canonical label on its own, with
    # no `display_name` write at all, so "before" has to be read first or
    # a suggestion-only bump (no auto-accept) would dispatch nothing.
    before = canonical_speaker_label_for_row(speaker)

    # A speaker the user unassigned from this identity is neither suggested it nor
    # linked to it (issue #1233). A speaker created by this very run has no
    # corrections yet; one being re-matched (a re-embed) may.
    if self._match_is_blocked(speaker, match):
        match = {
            **match,
            "auto_accept": False,
            "confidence": 0.0,
            "suggested_name": None,
            "profile_id": None,
        }

    match_detail = self._match_detail(match)
    self._record_match_suggestion(speaker, match, match_detail)

    # If high confidence, auto-apply the suggestion
    if match["auto_accept"]:
        _apply_auto_accept(speaker, match, match_detail)

    # One record call covers both paths above: a suggestion-only bump (no
    # `display_name` write) and a full auto-accept. Captured while the old
    # value still exists (issue #432) — after the commit Postgres only
    # knows the new one. `canonical_speaker_label_for_row` is the SAME
    # resolver the chunk-index writers use, so "after" is exactly what a
    # reindex would write (issue #605; this used to compute `display_name
    # or name`, which stopped agreeing the moment a confident suggestion
    # was the reason the label moved).
    self._rename_tracker.record(
        int(speaker.media_file_id), before, canonical_speaker_label_for_row(speaker)
    )

    self.db.flush()

    # Store embedding FIRST so incremental profile update can find it
    if aggregated_embedding is not None and aggregated_embedding.size > 0:
        _store_voiceprint_and_matches(
            self, speaker, match, aggregated_embedding, user_id, media_file_id, organization_id
        )

    # Incrementally update profile embedding using the embedding we already have
    if match["auto_accept"] and match.get("profile_id"):
        if speaker.profile_id and aggregated_embedding is not None:
            try:
                _blend_into_profile_centroid(self, speaker, aggregated_embedding)
            except Exception as e:
                logger.warning(f"Incremental profile update failed (non-fatal): {e}")

        # Propagate profile assignment to other similar speakers
        if speaker.profile_id:
            self._propagate_profile_assignment(int(speaker.id), int(speaker.profile_id), user_id)

    # Sync profile assignment to OpenSearch after the document has been created/updated
    if match["auto_accept"] and match.get("profile_id"):
        _sync_profile_to_index(self, speaker)

    return {
        "speaker_id": speaker.id,
        "speaker_label": speaker.name,
        "suggested_name": match["suggested_name"],
        "confidence": match["confidence"],
        "status": "matched",
    }


def handle_no_speaker_match(
    self: "SpeakerMatchingService",
    speaker: Speaker,
    aggregated_embedding: np.ndarray,
    user_id: int,
    media_file_id: int,
    organization_id: int | None = None,
) -> dict[str, Any]:
    """
    Handle case where no speaker match is found.

    Args:
        speaker: Speaker object
        aggregated_embedding: Aggregated embedding
        user_id: User ID
        media_file_id: Media file ID
        organization_id: Tenant scope (the file's org; None = personal).
            Stamped on the per-file speaker doc and used to gate the
            follow-up match queries.

    Returns:
        Speaker processing result
    """
    # No match found, store embedding for future matching
    if aggregated_embedding is not None and aggregated_embedding.size > 0:
        embedding_list = aggregated_embedding.tolist()
        logger.info(
            f"Storing embedding for speaker {int(speaker.id)}: length={len(embedding_list)}, first_few={embedding_list[:3]}"
        )
        add_speaker_embedding(
            speaker_id=int(speaker.id),
            speaker_uuid=str(speaker.uuid),
            user_id=user_id,
            name=str(speaker.name),
            embedding=embedding_list,
            profile_id=int(speaker.profile_id) if speaker.profile_id else None,
            profile_uuid=str(speaker.profile.uuid) if speaker.profile else None,
            display_name=str(speaker.display_name) if speaker.display_name else None,
            media_file_id=media_file_id,
            organization_id=organization_id,
        )

        # Find and store matches with other speakers
        found_matches = self.find_and_store_speaker_matches(
            int(speaker.id),
            aggregated_embedding,
            user_id,
            threshold=SPEAKER_CONFIDENCE_MEDIUM,
            organization_id=organization_id,
        )

        if found_matches:
            logger.info(f"Found {len(found_matches)} matches for speaker {int(speaker.id)}")
            # A >=0.75 suggestion moves the canonical label, so a name (or a speaker) the
            # user ruled out for this voice — carried over a re-run of its file — is never
            # suggested (issue #1233).
            refused_names, partners = self._refusals(int(speaker.id))
            # If there are high-confidence matches from verified speakers, suggest them
            for match in found_matches:
                if (
                    str(match.get("display_name") or "").casefold() in refused_names
                    or int(match.get("speaker_id") or 0) in partners
                ):
                    continue
                if match["confidence"] >= ConfidenceLevel.HIGH and match["display_name"]:
                    # No `display_name` write on this path at all — a
                    # suggestion crossing the resolver's confidence
                    # threshold moves the canonical label on its own, and
                    # nothing dispatched propagation for it until #605.
                    before = canonical_speaker_label_for_row(speaker)
                    if not record_suggestion(
                        speaker,
                        source="voice_match",
                        name=match["display_name"],
                        confidence=match["confidence"],
                        detail=voiceprint_detail(
                            ORIGIN_VOICE_MATCH,
                            confidence=match["confidence"],
                            threshold=ConfidenceLevel.HIGH,
                            matched_speaker_uuid=match.get("speaker_uuid"),
                            embedding_model=embedding_mode_service.voiceprint_model_id(),
                            origin_task=MATCHING_ORIGIN_TASK,
                        ),
                    ):
                        break  # a person verified this speaker: no voice suggestion
                    self._rename_tracker.record(
                        int(speaker.media_file_id),
                        before,
                        canonical_speaker_label_for_row(speaker),
                    )
                    self.db.flush()
                    break

        return {
            "speaker_id": speaker.id,
            "speaker_label": speaker.name,
            "profile_match": None,
            "status": "new",
        }
    else:
        logger.warning(f"Invalid embedding for speaker {speaker.id}, skipping storage")
        return {
            "speaker_id": speaker.id,
            "speaker_label": speaker.name,
            "profile_match": None,
            "status": "failed",
        }
