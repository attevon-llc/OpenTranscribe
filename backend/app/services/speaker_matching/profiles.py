"""Profiles: create, assign a speaker, refresh the reference embedding, collections, occurrences."""

import logging
import uuid
from typing import TYPE_CHECKING
from typing import Any

import numpy as np

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCollectionMember
from app.models.speaker import SpeakerProfile
from app.services.permission_service import org_scope_pred
from app.services.privacy.redact_view import PrivacyView
from app.services.takedown_service import exclude_quarantined
from app.utils.speaker_identity_detail import ORIGIN_PROFILE_ASSIGNED
from app.utils.speaker_identity_source import MACHINE_IDENTITY_SOURCES
from app.utils.speaker_identity_source import record_human_identity
from app.utils.speaker_labels import canonical_speaker_label_for_row

if TYPE_CHECKING:
    from app.services.speaker_matching_service import SpeakerMatchingService

logger = logging.getLogger(__name__)


def create_speaker_profile(
    self: "SpeakerMatchingService",
    name: str,
    user_id: int,
    description: str | None = None,
    initial_embedding: np.ndarray | None = None,
    organization_id: int | None = None,
) -> SpeakerProfile:
    """
    Create a new speaker profile.

    Args:
        name: Speaker name
        user_id: User ID
        description: Optional description
        initial_embedding: Optional initial embedding
        organization_id: Tenant scope the profile is created in (None =
            personal). Stamped on the DB row AND mirrored onto the
            OpenSearch profile doc so org queries can match it.

    Returns:
        Created SpeakerProfile
    """
    profile = SpeakerProfile(
        user_id=user_id,
        name=name,
        description=description,
        uuid=str(uuid.uuid4()),
        organization_id=organization_id,
    )

    self.db.add(profile)
    self.db.flush()

    # Add initial embedding to OpenSearch if provided
    if initial_embedding is not None:
        from app.services.opensearch_service import store_profile_embedding

        store_profile_embedding(
            profile_id=int(profile.id),
            profile_uuid=str(profile.uuid),
            profile_name=name,
            embedding=initial_embedding.tolist(),
            speaker_count=1,
            user_id=user_id,
            organization_id=organization_id,
        )

    return profile


def assign_speaker_to_profile(
    self: "SpeakerMatchingService",
    speaker_id: int,
    profile_id: int,
    confidence: float | None = None,
    *,
    user_id: int | None = None,
) -> Speaker:
    """
    Assign a speaker instance to a profile.

    Args:
        speaker_id: Speaker instance ID
        profile_id: Speaker profile ID
        confidence: Optional confidence score
        user_id: The person assigning it, recorded as who made the identity.

    Returns:
        Updated Speaker instance
    """
    speaker = self.db.query(Speaker).filter(Speaker.id == speaker_id).first()
    if not speaker:
        raise ValueError(f"Speaker {speaker_id} not found")

    # Captured before ANY write below (issue #605) — `confidence` alone can
    # move the canonical chunk-index label across the suggestion threshold
    # with no `display_name` write at all, the same shape as the other
    # writers of this field in this class (`_handle_speaker_match`,
    # `_handle_no_speaker_match`).
    before = canonical_speaker_label_for_row(speaker)

    # The user assigned this profile to this one speaker by hand: that overrides an
    # earlier unassignment from it (issue #1233). The profile embeddings it moves
    # between are rebuilt by the update task the endpoint queues.
    from app.services.speaker_cleanup import lift_profile_blacklist

    lift_profile_blacklist(self.db, int(speaker_id), int(profile_id))
    speaker.profile_id = profile_id  # type: ignore[assignment]
    speaker.verified = True  # type: ignore[assignment]
    # before the tracker computes the "after" label
    record_human_identity(speaker, user_id=user_id, origin=ORIGIN_PROFILE_ASSIGNED)
    if confidence is not None:
        speaker.confidence = confidence  # type: ignore[assignment]

    self._rename_tracker.record(
        int(speaker.media_file_id) if speaker.media_file_id else None,
        before,
        canonical_speaker_label_for_row(speaker),
    )

    # Commit-then-flush, matching `process_speaker_segments`/
    # `process_speaker_embeddings_native` above: a rolled-back assignment
    # must never reach the chunk index, so the tracker flushes only after
    # this method's own write is durable. This is the sole write in the
    # call — the endpoint's own `db.commit()` right after this call is a
    # harmless no-op on an already-committed session.
    self.db.commit()
    self._rename_tracker.flush(self.db)
    return speaker  # type: ignore[no-any-return]


def update_speaker_embeddings(
    self: "SpeakerMatchingService", profile_id: int, audio_paths: list[str]
) -> bool:
    """
    Update speaker profile embeddings with new reference audio.

    Args:
        profile_id: Speaker profile ID
        audio_paths: List of audio file paths

    Returns:
        Success status
    """
    try:
        profile = self.db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()

        if not profile:
            logger.error(f"Speaker profile {profile_id} not found")
            return False

        # Extract reference embedding
        if not self._voiceprints_allowed(int(profile.user_id), profile.organization_id):
            logger.info("Profile %s reference embedding skipped by privacy policy", profile_id)
            return False
        if self.embedding_service is None:
            logger.error("Embedding service is not available")
            return False
        embedding = self.embedding_service.extract_reference_embedding(audio_paths)

        if embedding is None:
            logger.error("Failed to extract reference embedding")
            return False

        # Update in OpenSearch
        from app.services.opensearch_service import store_profile_embedding

        store_profile_embedding(
            profile_id=profile_id,
            profile_uuid=str(profile.uuid),
            profile_name=str(profile.name),
            embedding=embedding.tolist(),
            speaker_count=1,
            user_id=int(profile.user_id),
            # Profile docs mirror the profile ROW's tenant scope
            organization_id=profile.organization_id,
        )

        return True

    except Exception as e:
        logger.error(f"Error updating speaker embeddings: {e}")
        return False


def add_speaker_to_collection(
    self: "SpeakerMatchingService", profile_id: int, collection_id: int
) -> bool:
    """
    Add a speaker profile to a collection.

    Args:
        profile_id: Speaker profile ID
        collection_id: Collection ID

    Returns:
        Success status
    """
    try:
        # Check if already exists
        existing = (
            self.db.query(SpeakerCollectionMember)
            .filter(
                SpeakerCollectionMember.collection_id == collection_id,
                SpeakerCollectionMember.speaker_profile_id == profile_id,
            )
            .first()
        )

        if existing:
            return True

        member = SpeakerCollectionMember(collection_id=collection_id, speaker_profile_id=profile_id)

        self.db.add(member)
        self.db.commit()

        return True

    except Exception as e:
        logger.error(f"Error adding speaker to collection: {e}")
        self.db.rollback()
        return False


def find_speaker_occurrences(
    self: "SpeakerMatchingService",
    profile_id: int,
    user_id: int,
    *,
    include_quarantined: bool = False,
    organization_id: OrgScope = UNSCOPED,
) -> list[dict[str, Any]]:
    """
    Find all media files where a speaker profile appears.

    Args:
        profile_id: Speaker profile ID
        user_id: User ID
        include_quarantined: Admin "see all" — skip the takedown exclusion below.
        organization_id: Tenant gate on the occurrence's file (int = that org,
            None = personal files only, ``UNSCOPED`` = no gate).

    Returns:
        List of media file information
    """
    # Get all speaker instances for this profile. `Speaker.media_file_id` is never
    # NULL, so an inner join to `MediaFile` here drops no legitimate row — it only
    # exists to let the quarantine exclusion filter on it. Without this a taken-down
    # file's speaker kept appearing in a profile's occurrence list (filename, title,
    # upload time) even though the file itself 404s everywhere under `files/`.
    query = (
        self.db.query(Speaker)
        .join(MediaFile, Speaker.media_file_id == MediaFile.id)
        .filter(Speaker.profile_id == profile_id, Speaker.user_id == user_id)
    )
    query = exclude_quarantined(query, include_quarantined=include_quarantined)
    tenant_pred = org_scope_pred(MediaFile.organization_id, organization_id)
    if tenant_pred is not None:
        query = query.filter(tenant_pred)
    speakers = query.all()

    # A link a voiceprint made is hidden while its FILE's voiceprints are off (issue #1280),
    # so the file is not an occurrence of the profile; the scope is the speaker's file's,
    # never the caller's argument. A person's own link is never hidden.
    view = PrivacyView(self.db)
    speakers = [
        s
        for s in speakers
        if s.identity_source not in MACHINE_IDENTITY_SOURCES
        or view.voiceprints(int(s.media_file_id)).enabled
    ]

    occurrences = []
    for speaker in speakers:
        media_file = speaker.media_file
        # `upload_time` is nullable: the server_default covers only an INSERT that omits
        # the column, so a backfilled or explicitly-updated row can hold NULL. Report it
        # as null rather than raising — one such row must not take out the whole
        # occurrences list for a profile.
        occurrences.append(
            {
                "media_file_id": media_file.id,
                "filename": media_file.filename,
                "title": media_file.title or media_file.filename,
                "upload_time": media_file.upload_time.isoformat()
                if media_file.upload_time
                else None,
                "speaker_label": speaker.name,
                "confidence": speaker.confidence,
                "verified": speaker.verified,
            }
        )

    # Sort by upload time (newest first). `or ""` keeps an unknown upload time at the
    # END under reverse=True; `str(None)` would sort "None" ahead of every ISO date.
    occurrences.sort(key=lambda x: str(x["upload_time"] or ""), reverse=True)

    return occurrences
