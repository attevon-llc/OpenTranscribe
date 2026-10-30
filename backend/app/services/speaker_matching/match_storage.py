"""Speaker-to-speaker match records: the kNN query, the stored pairs, and reading them back."""

import logging
from typing import TYPE_CHECKING
from typing import Any

import numpy as np
from sqlalchemy import func

from app.core.constants import SPEAKER_CONFIDENCE_MEDIUM
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import Speaker
from app.models.media import SpeakerMatch
from app.services.opensearch_service import find_matching_speaker
from app.services.speaker_matching.thresholds import ConfidenceLevel
from app.utils.cosine_space import raw_cosine_from_opensearch_score

if TYPE_CHECKING:
    from app.services.speaker_matching_service import SpeakerMatchingService

logger = logging.getLogger(__name__)


def build_speaker_match_query(
    self: "SpeakerMatchingService",
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
        *_speaker_org_filter_clauses(organization_id, user_ids=[user_id]),
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


def create_speaker_match_record(
    self: "SpeakerMatchingService", speaker1_id: int, speaker2_id: int, score: float
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
        for r in self.db.query(Speaker.id).filter(Speaker.id.in_([speaker1_id, speaker2_id])).all()
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


def build_match_result(
    self: "SpeakerMatchingService", matched_speaker: Speaker, score: float
) -> dict[str, Any]:
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
        "speaker_uuid": str(matched_speaker.uuid),
        "media_file_id": matched_speaker.media_file_id,
        "confidence": score,
        "confidence_level": self.get_confidence_level(score),
    }


def process_match_hit(
    self: "SpeakerMatchingService", hit: dict[str, Any], new_speaker_id: int, threshold: float
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
    score = raw_cosine_from_opensearch_score(hit["_score"])
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


def perform_opensearch_query(
    self: "SpeakerMatchingService",
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
    from app.core.embedding_models import get_speaker_index
    from app.services.opensearch_service import opensearch_client

    query = self._build_speaker_match_query(
        embedding, user_id, new_speaker_id, organization_id=organization_id
    )
    if opensearch_client is None:
        logger.error("OpenSearch client is not available")
        return None
    return opensearch_client.search(index=get_speaker_index(), body=query)  # type: ignore[no-any-return]


def find_and_store_speaker_matches(
    self: "SpeakerMatchingService",
    new_speaker_id: int,
    embedding: np.ndarray,
    user_id: int,
    threshold: float = SPEAKER_CONFIDENCE_MEDIUM,
    organization_id: OrgScope = UNSCOPED,
) -> list[dict[str, Any]]:
    """
    Find all similar speakers and store matches in the database.

    Args:
        new_speaker_id: ID of the newly processed speaker
        embedding: Speaker embedding vector
        user_id: User ID
        threshold: Minimum similarity threshold
        organization_id: Optional hint only. The tenant is derived from the new
            speaker's file, which gates both kNN queries so speaker-match records
            never pair voiceprints across tenants; a hint naming a different
            organization can narrow the answer and never widen it.

    Returns:
        List of found matches
    """
    matches: list[dict[str, Any]] = []

    allowed, organization_id = self._scope_gate(user_id, organization_id, speaker_id=new_speaker_id)
    if not allowed:
        return matches

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


def get_speaker_matches(self: "SpeakerMatchingService", speaker_id: int) -> list[dict[str, Any]]:
    """
    Get all matches for a given speaker.

    Args:
        speaker_id: Speaker ID

    Returns:
        List of matched speakers with details
    """
    matches: list[dict[str, Any]] = []

    try:
        # Privacy policy (#1280): stored match pairs are voiceprint-derived, so they are
        # hidden (not deleted) while the speaker's file scope has voiceprints off.
        from app.services.privacy.redact_view import PrivacyView

        owner = self.db.query(Speaker.media_file_id).filter(Speaker.id == speaker_id).scalar()
        if owner is None or not PrivacyView(self.db).voiceprints(int(owner)).enabled:
            return matches

        # Query matches where speaker is either speaker1 or speaker2
        speaker_matches = (
            self.db.query(SpeakerMatch)
            .filter(
                (SpeakerMatch.speaker1_id == speaker_id) | (SpeakerMatch.speaker2_id == speaker_id)
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
