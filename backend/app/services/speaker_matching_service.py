import logging
import uuid
from datetime import UTC
from typing import Any

import numpy as np
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.constants import SPEAKER_CONFIDENCE_HIGH
from app.core.constants import SPEAKER_CONFIDENCE_LOW
from app.core.constants import SPEAKER_CONFIDENCE_MEDIUM
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCollectionMember
from app.models.media import SpeakerMatch
from app.models.media import SpeakerProfile
from app.services.opensearch_service import add_speaker_embedding
from app.services.opensearch_service import find_matching_speaker
from app.services.speaker_embedding_service import SpeakerEmbeddingService
from app.services.speaker_rename_tracker import SpeakerRenameTracker

logger = logging.getLogger(__name__)


class ConfidenceLevel:
    """Confidence level thresholds for speaker matching"""

    HIGH = SPEAKER_CONFIDENCE_HIGH  # Auto-accept (green)
    MEDIUM = SPEAKER_CONFIDENCE_MEDIUM  # Requires validation (yellow)
    LOW = SPEAKER_CONFIDENCE_LOW  # Requires user input (red)


class SpeakerMatchingService:
    """Service for matching speakers across media files with confidence levels.

    Tenant scope (cloud edition): every kNN query and OpenSearch write this
    service performs is gated on ``organization_id``:

    * **Per-file speaker docs** (voiceprints of a speaker instance in one media
      file) follow the FILE's ``organization_id`` — resolved from the DB via
      ``_media_file_org_id`` and passed to both the store calls and the match
      queries, matching what the transcription pipeline stamps in the v4
      staging path (``ctx.organization_id``).
    * **Profile docs** (cross-video profile centroids) mirror the
      ``SpeakerProfile.organization_id`` row value at write time and are
      queried with the current file's org, so cross-video matching works
      within an org without leaking into personal scope or other orgs.

    Community edition: files and profiles carry ``organization_id`` NULL, so
    every resolved scope is None, docs stay org-less, and the personal gate
    (``must_not exists organization_id``) is a behavior-preserving no-op.
    """

    def __init__(self, db: Session, embedding_service: SpeakerEmbeddingService | None):
        self.db = db
        self.embedding_service = embedding_service
        # Auto-accepted matches rename speakers whose chunks may already be
        # indexed — the cloud-ASR path extracts embeddings on the GPU worker in
        # parallel with chunk indexing, and rediarize re-matches a long-completed
        # file. Recorded here, dispatched after the commit (issue #432).
        self._rename_tracker = SpeakerRenameTracker()

    def _media_file_org_id(self, media_file_id: int | None) -> int | None:
        """Resolve the tenant scope of a per-file speaker operation.

        The org of a speaker instance is defined by the media file it belongs
        to (``media_file.organization_id``), never by caller-supplied state.

        Args:
            media_file_id: Media file ID (None-safe).

        Returns:
            The file's organization_id, or None for personal/community files.
        """
        if media_file_id is None:
            return None
        row = self.db.query(MediaFile.organization_id).filter(MediaFile.id == media_file_id).first()
        return int(row[0]) if row and row[0] is not None else None

    def get_confidence_level(self, confidence: float) -> str:
        """
        Determine the confidence level for UI display.

        Args:
            confidence: Confidence score (0-1)

        Returns:
            'high', 'medium', or 'low'
        """
        if confidence >= ConfidenceLevel.HIGH:
            return "high"
        elif confidence >= ConfidenceLevel.MEDIUM:
            return "medium"
        else:
            return "low"

    def match_speaker_to_known_speakers(
        self,
        embedding: np.ndarray,
        user_id: int,
        accessible_profile_ids: set[int] | None = None,
        organization_id: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Match a speaker embedding to known speakers, prioritizing profile embeddings
        for better cross-file recognition accuracy.

        Args:
            embedding: Speaker embedding vector
            user_id: User ID
            accessible_profile_ids: Optional set of profile IDs to search within
                (for shared profile scope). If None, filters by user_id.
            organization_id: Tenant scope of the file being matched (None =
                personal). Gates both the profile kNN and the individual
                speaker kNN so cross-org voiceprints never match.

        Returns:
            Dictionary with speaker info and confidence, or None
        """
        # First, try to match against profile embeddings for better accuracy
        profile_match = self.match_speaker_to_profiles(
            embedding,
            user_id,
            accessible_profile_ids=accessible_profile_ids,
            organization_id=organization_id,
        )

        if profile_match and profile_match["confidence"] >= ConfidenceLevel.MEDIUM:
            return profile_match

        # Fallback to individual speaker matching in OpenSearch
        match = find_matching_speaker(
            embedding.tolist(),
            user_id,
            threshold=ConfidenceLevel.MEDIUM,  # Minimum threshold
            organization_id=organization_id,
        )

        if not match:
            return None

        # Get the speaker from database
        speaker = (
            self.db.query(Speaker)
            .filter(
                Speaker.id == match["speaker_id"],
                Speaker.user_id == user_id,
                Speaker.verified.is_(True),
                Speaker.display_name.isnot(None),
            )
            .first()
        )

        if not speaker:
            return None

        confidence = match.get("confidence", 0.5)

        return {
            "speaker_id": speaker.id,
            "suggested_name": speaker.display_name,
            "confidence": confidence,
            "confidence_level": self.get_confidence_level(confidence),
            "auto_accept": confidence >= ConfidenceLevel.HIGH,
            "source": "individual_embedding",
        }

    def match_speaker_to_profiles(
        self,
        embedding: np.ndarray,
        user_id: int,
        accessible_profile_ids: set[int] | None = None,
        organization_id: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Match a speaker embedding against consolidated profile embeddings.
        This provides better accuracy than individual speaker matching.

        Args:
            embedding: Speaker embedding vector
            user_id: User ID
            accessible_profile_ids: Optional set of profile IDs to search within
                (for shared profile scope). If None, filters by user_id.
            organization_id: Tenant scope of the file being matched (None =
                personal). Profile docs mirror ``SpeakerProfile.organization_id``
                so only same-org (or, for None, personal) profiles can match.

        Returns:
            Dictionary with profile match info and confidence, or None
        """
        try:
            from app.services.profile_embedding_service import ProfileEmbeddingService

            # Get profile matches using the ProfileEmbeddingService
            profile_matches = ProfileEmbeddingService.calculate_profile_similarity(
                embedding.tolist(),
                user_id,
                threshold=ConfidenceLevel.LOW,
                accessible_profile_ids=accessible_profile_ids,
                organization_id=organization_id,
            )

            if not profile_matches:
                return None

            # Return the best match
            best_match = profile_matches[0]
            confidence = best_match["similarity"]

            # Verify profile exists and is accessible
            if accessible_profile_ids is not None:
                profile = (
                    self.db.query(SpeakerProfile)
                    .filter(
                        SpeakerProfile.id == best_match["profile_id"],
                        SpeakerProfile.id.in_(accessible_profile_ids),
                    )
                    .first()
                )
            else:
                profile = (
                    self.db.query(SpeakerProfile)
                    .filter(
                        SpeakerProfile.id == best_match["profile_id"],
                        SpeakerProfile.user_id == user_id,
                    )
                    .first()
                )

            if not profile:
                return None

            # Try to get a representative speaker from this profile if one exists
            # This is optional - the profile itself is sufficient for matching
            profile_speaker = (
                self.db.query(Speaker)
                .filter(
                    Speaker.profile_id == profile.id,  # Use integer profile.id
                    Speaker.user_id == user_id,
                    Speaker.verified.is_(True),
                    Speaker.display_name.isnot(None),
                )
                .first()
            )

            # Use profile_speaker.id if available, otherwise None
            # The system will handle None speaker_id correctly
            speaker_id = profile_speaker.id if profile_speaker else None

            return {
                "speaker_id": speaker_id,
                "suggested_name": best_match["profile_name"],
                "confidence": confidence,
                "confidence_level": self.get_confidence_level(confidence),
                "auto_accept": confidence >= ConfidenceLevel.HIGH,
                "profile_id": profile.id,  # Return integer ID for database operations
                "profile_uuid": str(profile.uuid),  # Also return UUID for API responses
                "source": "profile_embedding",
                "embedding_count": best_match["embedding_count"],
            }

        except Exception as e:
            logger.error(f"Error matching speaker to profiles: {e}")
            return None

    def find_unlabeled_speaker_matches(
        self, embedding: np.ndarray, user_id: int, exclude_speaker_id: int
    ) -> list[dict[str, Any]]:
        """
        Find matches to unlabeled speakers across videos (for pre-labeling suggestions).

        Args:
            embedding: Speaker embedding vector
            user_id: User ID
            exclude_speaker_id: Speaker ID to exclude from results

        Returns:
            List of matched speakers with video source information
        """
        matches: list[dict[str, Any]] = []
        logger.info(f"Finding unlabeled matches for speaker {exclude_speaker_id}, user {user_id}")

        try:
            from app.core.constants import get_speaker_index
            from app.services.opensearch_service import _speaker_org_filter_clauses
            from app.services.opensearch_service import opensearch_client

            # Tenant gate follows the reference speaker's file org.
            source_speaker = self.db.query(Speaker).filter(Speaker.id == exclude_speaker_id).first()
            organization_id = self._media_file_org_id(
                int(source_speaker.media_file_id) if source_speaker else None
            )

            query = {
                "size": 10,  # Get more potential matches for unlabeled speakers
                "query": {
                    "knn": {
                        "embedding": {
                            "vector": embedding.tolist(),
                            "k": 10,
                            "filter": {
                                "bool": {
                                    "filter": [
                                        {"term": {"user_id": user_id}},
                                        *_speaker_org_filter_clauses(organization_id),
                                        {
                                            "bool": {
                                                "must_not": [
                                                    {"term": {"speaker_id": exclude_speaker_id}},
                                                    {
                                                        "exists": {"field": "document_type"}
                                                    },  # Exclude profile documents
                                                ]
                                            }
                                        },
                                    ]
                                }
                            },
                        }
                    }
                },
            }

            if opensearch_client is None:
                logger.error("OpenSearch client is not available")
                return matches
            response = opensearch_client.search(index=get_speaker_index(), body=query)

            logger.info(
                f"OpenSearch response for speaker {exclude_speaker_id}: {len(response['hits']['hits'])} hits"
            )

            # Collect candidate IDs from hits, then batch-fetch
            candidate_ids: set[int] = set()
            candidate_scores: dict[int, float] = {}
            for hit in response["hits"]["hits"]:
                confidence = 2.0 * hit["_score"] - 1.0  # Convert OS cosinesimil to raw cosine
                if (
                    confidence >= ConfidenceLevel.HIGH
                ):  # ConfidenceLevel.HIGH threshold for high-confidence matches only
                    # Safely access speaker_id - skip if not present (e.g., profile documents)
                    source = hit.get("_source", {})
                    match_speaker_id = source.get("speaker_id")
                    if not match_speaker_id:
                        continue

                    candidate_ids.add(match_speaker_id)
                    candidate_scores[match_speaker_id] = confidence

            # Batch-fetch all candidate speakers (avoids N+1 per-hit queries)
            if candidate_ids:
                from sqlalchemy.orm import joinedload

                candidates = (
                    self.db.query(Speaker)
                    .options(joinedload(Speaker.media_file))
                    .filter(Speaker.id.in_(candidate_ids))
                    .all()
                )
                for matched_speaker in candidates:
                    if matched_speaker.media_file:
                        conf = candidate_scores[int(matched_speaker.id)]
                        matches.append(
                            {
                                "speaker_id": int(matched_speaker.id),
                                "speaker_name": matched_speaker.name,
                                "display_name": matched_speaker.display_name,
                                "media_file_id": matched_speaker.media_file_id,
                                "media_file_title": matched_speaker.media_file.title
                                or matched_speaker.media_file.filename,
                                "confidence": conf,
                                "confidence_level": self.get_confidence_level(conf),
                                "verified": matched_speaker.verified,
                                "is_cross_video_suggestion": True,
                            }
                        )

            # Sort by confidence (highest first)
            matches.sort(key=lambda x: x["confidence"], reverse=True)

        except Exception as e:
            logger.error(f"Error finding unlabeled speaker matches: {e}")

        logger.info(f"Returning {len(matches)} unlabeled matches for speaker {exclude_speaker_id}")
        return matches

    def match_speaker_to_profile(
        self, embedding: np.ndarray, user_id: int, organization_id: int | None = None
    ) -> dict[str, Any] | None:
        """
        Match a speaker embedding to an existing profile.

        Args:
            embedding: Speaker embedding vector
            user_id: User ID
            organization_id: Tenant scope of the file being matched (None =
                personal) — gates the speaker kNN.

        Returns:
            Dictionary with profile info and confidence, or None
        """
        # Search for matching speaker in OpenSearch
        match = find_matching_speaker(
            embedding.tolist(),
            user_id,
            threshold=ConfidenceLevel.MEDIUM,  # Minimum threshold
            organization_id=organization_id,
        )

        if not match:
            return None

        # Check if matched speaker has a profile
        profile_id = match.get("profile_id")
        if not profile_id:
            return None

        # Get the speaker profile from database
        profile = (
            self.db.query(SpeakerProfile)
            .filter(
                SpeakerProfile.id == profile_id,
                SpeakerProfile.user_id == user_id,
            )
            .first()
        )

        if not profile:
            return None

        confidence = match.get("confidence", 0.5)

        return {
            "profile_id": profile.id,
            "profile_name": profile.name,
            "confidence": confidence,
            "confidence_level": self.get_confidence_level(confidence),
            "auto_accept": confidence >= ConfidenceLevel.HIGH,
        }

    def create_speaker_profile(
        self,
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
        self, speaker_id: int, profile_id: int, confidence: float | None = None
    ) -> Speaker:
        """
        Assign a speaker instance to a profile.

        Args:
            speaker_id: Speaker instance ID
            profile_id: Speaker profile ID
            confidence: Optional confidence score

        Returns:
            Updated Speaker instance
        """
        speaker = self.db.query(Speaker).filter(Speaker.id == speaker_id).first()
        if not speaker:
            raise ValueError(f"Speaker {speaker_id} not found")

        speaker.profile_id = profile_id  # type: ignore[assignment]
        speaker.verified = True  # type: ignore[assignment]
        if confidence is not None:
            speaker.confidence = confidence  # type: ignore[assignment]

        self.db.flush()
        return speaker  # type: ignore[no-any-return]

    def process_speaker_segments(
        self,
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
        # Extract embeddings for all speakers
        if self.embedding_service is None:
            raise ValueError("Embedding service is required for processing speaker segments")
        speaker_embeddings = self.embedding_service.extract_embeddings_for_segments(
            audio_path, segments, speaker_mapping
        )

        # Tenant scope for all stores/queries follows the file's org.
        organization_id = self._media_file_org_id(media_file_id)

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
        self,
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

    def _process_single_speaker(
        self,
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

    def _linked_speaker_count(self, profile_id: int) -> int:
        """Count the speakers currently assigned to *profile_id*.

        This is the profile's embedding population, and therefore the weight its
        stored centroid carries. It is queried rather than remembered so the
        incremental update cannot drift from the number a full recalculation
        would produce.

        Args:
            profile_id: Profile whose members to count.

        Returns:
            Number of ``Speaker`` rows pointing at the profile.
        """
        return (
            self.db.query(func.count(Speaker.id)).filter(Speaker.profile_id == profile_id).scalar()
            or 0
        )

    def _handle_speaker_match(
        self,
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
        # Found a matching speaker - store the suggestion only if high confidence
        speaker.confidence = match["confidence"]
        if match["confidence"] >= ConfidenceLevel.HIGH:  # Only suggest if ≥75% confidence
            speaker.suggested_name = match["suggested_name"]
            speaker.suggestion_source = (
                "profile_match" if match.get("profile_id") else "voice_match"  # type: ignore[assignment]
            )

        # If high confidence, auto-apply the suggestion
        if match["auto_accept"]:
            logger.info(
                f"Auto-accepting match for speaker {int(speaker.id)} -> {match['suggested_name']}"
            )
            # The string the chunk plane holds, captured while it still exists
            # (issue #432) — after the commit Postgres only knows the new name.
            self._rename_tracker.record(
                int(speaker.media_file_id),
                str(speaker.display_name or speaker.name or ""),
                match["suggested_name"],
            )
            speaker.display_name = match["suggested_name"]
            speaker.verified = True  # type: ignore[assignment]
            # Assign to profile if this match came from a profile
            if match.get("profile_id"):
                speaker.profile_id = match["profile_id"]

        self.db.flush()

        # Store embedding FIRST so incremental profile update can find it
        if aggregated_embedding is not None and aggregated_embedding.size > 0:
            add_speaker_embedding(
                speaker_id=int(speaker.id),
                speaker_uuid=str(speaker.uuid),
                user_id=user_id,
                name=str(speaker.name),
                embedding=aggregated_embedding.tolist(),
                profile_id=int(speaker.profile_id) if speaker.profile_id else None,
                profile_uuid=str(speaker.profile.uuid) if speaker.profile else None,
                display_name=str(speaker.display_name)
                if speaker.display_name
                else match["suggested_name"],
                media_file_id=media_file_id,
                organization_id=organization_id,
            )

            # Find and store matches with other speakers
            self.find_and_store_speaker_matches(
                int(speaker.id),
                aggregated_embedding,
                user_id,
                threshold=0.5,
                organization_id=organization_id,
            )

        # Incrementally update profile embedding using the embedding we already have
        if match["auto_accept"] and match.get("profile_id"):
            if speaker.profile_id and aggregated_embedding is not None:
                try:
                    from app.services.opensearch_service import get_profile_embedding
                    from app.services.opensearch_service import store_profile_embedding

                    profile = (
                        self.db.query(SpeakerProfile)
                        .filter(SpeakerProfile.id == speaker.profile_id)
                        .first()
                    )
                    if profile:
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
                            import numpy as np

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
                        from datetime import datetime

                        profile.last_embedding_update = datetime.now(UTC)  # type: ignore[assignment]
                        self.db.commit()
                except Exception as e:
                    logger.warning(f"Incremental profile update failed (non-fatal): {e}")

            # Propagate profile assignment to other similar speakers
            if speaker.profile_id:
                self._propagate_profile_assignment(
                    int(speaker.id), int(speaker.profile_id), user_id
                )

        # Sync profile assignment to OpenSearch after the document has been created/updated
        if match["auto_accept"] and match.get("profile_id"):
            from app.services.opensearch_service import update_speaker_profile

            profile_uuid = None
            if speaker.profile_id:
                profile = (
                    self.db.query(SpeakerProfile)
                    .filter(SpeakerProfile.id == speaker.profile_id)
                    .first()
                )
                if profile:
                    profile_uuid = str(profile.uuid)

            update_speaker_profile(
                speaker_uuid=str(speaker.uuid),
                profile_id=int(speaker.profile_id) if speaker.profile_id else None,
                profile_uuid=profile_uuid,
                verified=bool(speaker.verified),
            )

        return {
            "speaker_id": speaker.id,
            "speaker_label": speaker.name,
            "suggested_name": match["suggested_name"],
            "confidence": match["confidence"],
            "status": "matched",
        }

    def _handle_no_speaker_match(
        self,
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
                threshold=0.5,
                organization_id=organization_id,
            )

            if found_matches:
                logger.info(f"Found {len(found_matches)} matches for speaker {int(speaker.id)}")
                # If there are high-confidence matches from verified speakers, suggest them
                for match in found_matches:
                    if match["confidence"] >= ConfidenceLevel.HIGH and match["display_name"]:
                        speaker.suggested_name = match["display_name"]
                        speaker.confidence = match["confidence"]
                        speaker.suggestion_source = "voice_match"  # type: ignore[assignment]
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

    def find_speaker_occurrences(self, profile_id: int, user_id: int) -> list[dict[str, Any]]:
        """
        Find all media files where a speaker profile appears.

        Args:
            profile_id: Speaker profile ID
            user_id: User ID

        Returns:
            List of media file information
        """
        # Get all speaker instances for this profile
        speakers = (
            self.db.query(Speaker)
            .filter(Speaker.profile_id == profile_id, Speaker.user_id == user_id)
            .all()
        )

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

    def _get_speaker_embedding_for_propagation(
        self, matched_speaker_id: int
    ) -> tuple[list[float] | None, str | None]:
        """
        Get speaker embedding for profile propagation.

        Args:
            matched_speaker_id: ID of the speaker to get embedding for

        Returns:
            Tuple of (embedding, speaker_uuid) or (None, None) if not found
        """
        from app.db.base import get_db
        from app.services.opensearch_service import get_speaker_embedding

        db = next(get_db())
        try:
            matched_speaker = db.query(Speaker).filter(Speaker.id == matched_speaker_id).first()
            if not matched_speaker:
                logger.warning(f"Speaker {matched_speaker_id} not found, skipping propagation")
                return None, None

            embedding = get_speaker_embedding(str(matched_speaker.uuid))
            if not embedding:
                logger.warning(
                    f"No embedding found for speaker {matched_speaker.uuid}, skipping propagation"
                )
                return None, None

            return embedding, str(matched_speaker.uuid)
        finally:
            db.close()

    def _should_propagate_to_speaker(self, speaker: Speaker) -> bool:
        """
        Check if a speaker should receive profile propagation.

        Args:
            speaker: Speaker to check

        Returns:
            True if speaker should be updated with profile
        """
        if speaker.profile_id:
            return False
        if not speaker.display_name:
            return False
        return not speaker.display_name.startswith("SPEAKER_")

    def _update_speakers_in_opensearch(
        self, updated_speakers: list[Speaker], profile_id: int
    ) -> None:
        """
        Update speakers in OpenSearch after profile propagation.

        Args:
            updated_speakers: List of speakers to update
            profile_id: Profile ID assigned to speakers
        """
        from app.services.opensearch_service import update_speaker_profile

        profile_uuid = None
        if profile_id:
            profile = self.db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()
            if profile:
                profile_uuid = str(profile.uuid)

        for speaker in updated_speakers:
            update_speaker_profile(
                speaker_uuid=str(speaker.uuid),
                profile_id=int(speaker.profile_id) if speaker.profile_id else None,
                profile_uuid=profile_uuid,
                verified=bool(speaker.verified),
            )

    def _propagate_profile_assignment(self, matched_speaker_id: int, profile_id: int, user_id: int):
        """
        When a speaker is assigned to a profile, find and update other similar speakers
        with the same profile assignment.

        Args:
            matched_speaker_id: ID of the speaker that was just assigned to a profile
            profile_id: ID of the profile assigned
            user_id: User ID for scoping
        """
        try:
            embedding, _ = self._get_speaker_embedding_for_propagation(matched_speaker_id)
            if not embedding:
                return

            from app.services.similarity_service import SimilarityService

            # Propagation stays within the profile's tenant: an org profile is
            # only propagated to that org's speaker docs, a personal profile
            # only to personal (org-less) docs.
            profile_row = (
                self.db.query(SpeakerProfile.organization_id)
                .filter(SpeakerProfile.id == profile_id)
                .first()
            )
            profile_org_id = (
                int(profile_row[0]) if profile_row and profile_row[0] is not None else None
            )

            similar_matches = SimilarityService.opensearch_similarity_search(
                embedding=embedding,
                user_id=user_id,
                index_name="speakers",
                threshold=0.75,  # High confidence for automatic assignment (matches SPEAKER_CONFIDENCE_HIGH)
                max_results=20,
                exclude_ids=[matched_speaker_id],
                organization_id=profile_org_id,
            )

            # Batch-fetch all candidate speakers (avoids N+1 per-match queries)
            match_by_id = {
                m["speaker_id"]: m["similarity"] for m in similar_matches if m.get("speaker_id")
            }
            candidate_speakers = (
                self.db.query(Speaker).filter(Speaker.id.in_(match_by_id.keys())).all()
                if match_by_id
                else []
            )

            updated_speakers = []
            for speaker in candidate_speakers:
                if not self._should_propagate_to_speaker(speaker):
                    continue

                similarity = match_by_id[int(speaker.id)]
                speaker.profile_id = profile_id  # type: ignore[assignment]
                speaker.verified = True  # type: ignore[assignment]
                speaker.confidence = similarity
                updated_speakers.append(speaker)

                logger.info(
                    f"Propagated profile {profile_id} to similar speaker {speaker.id} "
                    f"(confidence: {similarity:.3f})"
                )

            if not updated_speakers:
                return

            self.db.commit()
            self._update_speakers_in_opensearch(updated_speakers, profile_id)
            logger.info(
                f"Propagated profile {profile_id} to {len(updated_speakers)} similar speakers"
            )

        except Exception as e:
            logger.error(f"Error propagating profile assignment: {e}")
            self.db.rollback()

    def update_speaker_embeddings(self, profile_id: int, audio_paths: list[str]) -> bool:
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

    def add_speaker_to_collection(self, profile_id: int, collection_id: int) -> bool:
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

            member = SpeakerCollectionMember(
                collection_id=collection_id, speaker_profile_id=profile_id
            )

            self.db.add(member)
            self.db.commit()

            return True

        except Exception as e:
            logger.error(f"Error adding speaker to collection: {e}")
            self.db.rollback()
            return False

    def _build_speaker_match_query(
        self,
        embedding: np.ndarray,
        user_id: int,
        exclude_speaker_id: int,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Build OpenSearch query for finding speaker matches.

        Args:
            embedding: Speaker embedding vector
            user_id: User ID
            exclude_speaker_id: Speaker ID to exclude from results
            organization_id: Tenant scope (None = personal) — org term when
                set, else the personal ``must_not exists`` gate.

        Returns:
            OpenSearch query dictionary
        """
        from app.services.opensearch_service import _speaker_org_filter_clauses

        filters = [
            {"term": {"user_id": user_id}},
            *_speaker_org_filter_clauses(organization_id),
            {
                "bool": {
                    "must_not": [
                        {"term": {"speaker_id": exclude_speaker_id}},
                        {"exists": {"field": "document_type"}},  # Exclude profile documents
                    ]
                }
            },
        ]

        return {
            "size": 20,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": embedding.tolist(),
                        "k": 20,
                        "filter": {"bool": {"filter": filters}},
                    }
                }
            },
        }

    def _create_speaker_match_record(
        self, speaker1_id: int, speaker2_id: int, score: float
    ) -> bool:
        """
        Create a new speaker match record in the database.

        Args:
            speaker1_id: First speaker ID (smaller)
            speaker2_id: Second speaker ID (larger)
            score: Confidence score

        Returns:
            True if match was created successfully
        """
        existing_ids = {
            r[0]
            for r in self.db.query(Speaker.id)
            .filter(Speaker.id.in_([speaker1_id, speaker2_id]))
            .all()
        }
        if speaker1_id not in existing_ids or speaker2_id not in existing_ids:
            logger.debug(
                f"Skipping speaker match - speaker1_id {speaker1_id} exists: "
                f"{speaker1_id in existing_ids}, speaker2_id {speaker2_id} exists: "
                f"{speaker2_id in existing_ids}"
            )
            return False

        try:
            speaker_match = SpeakerMatch(
                speaker1_id=speaker1_id,
                speaker2_id=speaker2_id,
                confidence=score,
            )
            self.db.add(speaker_match)
            self.db.flush()
            return True
        except Exception as e:
            logger.warning(
                f"Failed to create speaker match between {speaker1_id} and {speaker2_id}: {e}"
            )
            self.db.rollback()
            return False

    def _build_match_result(self, matched_speaker: Speaker, score: float) -> dict[str, Any]:
        """
        Build a match result dictionary from a speaker.

        Args:
            matched_speaker: Speaker object
            score: Confidence score

        Returns:
            Match result dictionary
        """
        return {
            "speaker_id": matched_speaker.id,
            "speaker_name": matched_speaker.name,
            "display_name": matched_speaker.display_name,
            "media_file_id": matched_speaker.media_file_id,
            "confidence": score,
            "confidence_level": self.get_confidence_level(score),
        }

    def _process_match_hit(
        self, hit: dict[str, Any], new_speaker_id: int, threshold: float
    ) -> dict[str, Any] | None:
        """
        Process a single OpenSearch match hit.

        Args:
            hit: OpenSearch hit dictionary
            new_speaker_id: ID of the new speaker
            threshold: Minimum similarity threshold

        Returns:
            Match result dictionary or None if processing failed
        """
        score = 2.0 * hit["_score"] - 1.0  # Convert OS cosinesimil to raw cosine
        if score < threshold:
            return None

        match_speaker_id = hit.get("_source", {}).get("speaker_id")
        if not match_speaker_id:
            return None

        speaker1_id = min(new_speaker_id, match_speaker_id)
        speaker2_id = max(new_speaker_id, match_speaker_id)

        existing_match = (
            self.db.query(SpeakerMatch)
            .filter(
                SpeakerMatch.speaker1_id == speaker1_id,
                SpeakerMatch.speaker2_id == speaker2_id,
            )
            .first()
        )

        if existing_match:
            if score > existing_match.confidence:
                existing_match.confidence = score
                existing_match.updated_at = func.now()  # type: ignore[assignment]
            return None

        if not self._create_speaker_match_record(speaker1_id, speaker2_id, score):
            return None

        matched_speaker = self.db.query(Speaker).filter(Speaker.id == match_speaker_id).first()
        if matched_speaker:
            return self._build_match_result(matched_speaker, score)
        return None

    def _perform_opensearch_query(
        self,
        embedding: np.ndarray,
        user_id: int,
        new_speaker_id: int,
        organization_id: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Perform OpenSearch query for speaker matches.

        Args:
            embedding: Speaker embedding vector
            user_id: User ID
            new_speaker_id: ID of the new speaker
            organization_id: Tenant scope (None = personal)

        Returns:
            OpenSearch response or None if client unavailable
        """
        from app.core.constants import get_speaker_index
        from app.services.opensearch_service import opensearch_client

        query = self._build_speaker_match_query(
            embedding, user_id, new_speaker_id, organization_id=organization_id
        )
        if opensearch_client is None:
            logger.error("OpenSearch client is not available")
            return None
        return opensearch_client.search(index=get_speaker_index(), body=query)  # type: ignore[no-any-return]

    def find_and_store_speaker_matches(
        self,
        new_speaker_id: int,
        embedding: np.ndarray,
        user_id: int,
        threshold: float = 0.5,
        organization_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Find all similar speakers and store matches in the database.

        Args:
            new_speaker_id: ID of the newly processed speaker
            embedding: Speaker embedding vector
            user_id: User ID
            threshold: Minimum similarity threshold
            organization_id: Tenant scope of the new speaker's file (None =
                personal). Gates both kNN queries so speaker-match records
                never pair voiceprints across tenants.

        Returns:
            List of found matches
        """
        matches: list[dict[str, Any]] = []

        try:
            search_results = find_matching_speaker(
                embedding.tolist(),
                user_id,
                threshold=threshold,
                exclude_speaker_ids=[new_speaker_id],
                organization_id=organization_id,
            )

            if not search_results:
                return matches

            response = self._perform_opensearch_query(
                embedding, user_id, new_speaker_id, organization_id=organization_id
            )
            if response is None:
                return matches

            for hit in response["hits"]["hits"]:
                match_result = self._process_match_hit(hit, new_speaker_id, threshold)
                if match_result:
                    matches.append(match_result)

            self.db.flush()

        except Exception as e:
            logger.error(f"Error finding and storing speaker matches: {e}")

        return matches

    def get_speaker_matches(self, speaker_id: int) -> list[dict[str, Any]]:
        """
        Get all matches for a given speaker.

        Args:
            speaker_id: Speaker ID

        Returns:
            List of matched speakers with details
        """
        matches: list[dict[str, Any]] = []

        try:
            # Query matches where speaker is either speaker1 or speaker2
            speaker_matches = (
                self.db.query(SpeakerMatch)
                .filter(
                    (SpeakerMatch.speaker1_id == speaker_id)
                    | (SpeakerMatch.speaker2_id == speaker_id)
                )
                .all()
            )

            # Collect matched IDs and batch-fetch (avoids N+1 per-match queries)
            match_map: dict[int, float] = {}
            for match in speaker_matches:
                matched_speaker_id = (
                    match.speaker2_id if match.speaker1_id == speaker_id else match.speaker1_id
                )
                conf = float(match.confidence)
                if conf >= ConfidenceLevel.HIGH:
                    match_map[int(matched_speaker_id)] = conf

            if match_map:
                from sqlalchemy.orm import joinedload

                matched_speakers = (
                    self.db.query(Speaker)
                    .options(joinedload(Speaker.media_file))
                    .filter(Speaker.id.in_(match_map.keys()))
                    .all()
                )
                for matched_speaker in matched_speakers:
                    if matched_speaker.media_file:
                        conf = match_map[int(matched_speaker.id)]
                        matches.append(
                            {
                                "speaker_id": matched_speaker.id,
                                "speaker_name": matched_speaker.name,
                                "display_name": matched_speaker.display_name,
                                "media_file_id": matched_speaker.media_file_id,
                                "media_file_title": matched_speaker.media_file.title
                                or matched_speaker.media_file.filename,
                                "confidence": conf,
                                "confidence_level": self.get_confidence_level(conf),
                                "verified": matched_speaker.verified,
                            }
                        )

            # Sort by confidence (highest first)
            matches.sort(key=lambda x: x["confidence"], reverse=True)

        except Exception as e:
            logger.error(f"Error getting speaker matches: {e}")

        return matches
