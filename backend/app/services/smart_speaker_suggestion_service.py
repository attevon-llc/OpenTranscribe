"""
Smart Speaker Suggestion Service.

This service implements intelligent speaker identification suggestions that:
- Prioritize verified speaker profiles
- Consolidate duplicate speaker names into single suggestions
- Filter out unlabeled speakers (SPEAKER_XX format)
- Handle all business logic for speaker suggestions server-side

The service generates two types of suggestions:
1. LLM suggestions (from import process, stored in suggested_name)
2. Profile suggestions (speaker-to-profile matching via KNN)

The service is designed to minimize frontend complexity by providing pre-filtered,
consolidated suggestions that the frontend can display directly without additional
processing or filtering.
"""

import logging
from dataclasses import dataclass
from typing import Any
from typing import Optional

import numpy as np
from sqlalchemy.orm import Session

from app.core.constants import get_speaker_index
from app.models.media import MediaFile
from app.models.media import Speaker
from app.services.opensearch_service import get_speaker_embedding
from app.services.profile_embedding_service import ProfileEmbeddingService

logger = logging.getLogger(__name__)


def _media_file_org_ids(db: Session, media_file_ids: set[int]) -> dict[int, int | None]:
    """Resolve the tenant scope (organization_id) of a set of media files.

    Per-file speaker operations are gated by the FILE's org (None = personal /
    community), so suggestion queries for a speaker must carry the org of the
    file that speaker belongs to.
    """
    if not media_file_ids:
        return {}
    rows = (
        db.query(MediaFile.id, MediaFile.organization_id)
        .filter(MediaFile.id.in_(media_file_ids))
        .all()
    )
    return {int(fid): (int(org) if org is not None else None) for fid, org in rows}


def _determine_confidence_level(confidence: float, embedding_count: int = 1) -> tuple[str, bool]:
    """
    Determine the reason string and auto_accept flag based on confidence level.

    Args:
        confidence: Confidence score (0.0 to 1.0)
        embedding_count: Number of embeddings/recordings (for profile matches)

    Returns:
        Tuple of (reason string, auto_accept boolean)
    """
    if confidence >= 0.95:
        return (
            f"Excellent voice match (verified across {embedding_count} recordings)",
            True,
        )
    elif confidence >= 0.85:
        return (
            f"Very strong voice match (verified across {embedding_count} recordings)",
            True,
        )
    elif confidence >= 0.75:
        return (
            f"Strong voice match (verified across {embedding_count} recordings)",
            True,
        )
    else:
        return (
            f"Moderate voice match (verified across {embedding_count} recordings)",
            False,
        )


def _check_opensearch_profiles_exist(
    opensearch_client: Any,
    user_id: int,
    accessible_profile_ids: set[int] | None = None,
) -> bool:
    """
    Check if any profile documents exist for the user/scope in OpenSearch.

    Returns False if index doesn't exist or no profiles found, True otherwise.
    """
    if not opensearch_client.indices.exists(index=get_speaker_index()):
        logger.info("Speakers index does not exist yet, skipping profile suggestion search")
        return False

    user_filter: dict[str, Any]
    if accessible_profile_ids is not None:
        user_filter = {"terms": {"profile_id": list(accessible_profile_ids)}}
    else:
        user_filter = {"term": {"user_id": user_id}}

    profile_check_query = {
        "size": 1,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"document_type": "profile"}},
                    user_filter,
                ]
            }
        },
    }

    try:
        profile_check = opensearch_client.search(
            index=get_speaker_index(), body=profile_check_query
        )
        if profile_check["hits"]["total"]["value"] == 0:
            logger.info(
                f"No profile documents found for user {user_id}, skipping profile KNN search"
            )
            return False
        return True
    except Exception as e:
        logger.warning(f"Profile document check failed: {e}, proceeding with KNN query")
        return True  # Proceed with query if check fails


def _execute_profile_knn_search(
    opensearch_client: Any,
    embedding: np.ndarray,
    user_id: int,
    threshold: float,
    accessible_profile_ids: set[int] | None = None,
    organization_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Execute kNN search for profile matches and return filtered results.

    Returns list of profile match dictionaries with similarity scores.
    """
    from app.services.opensearch_service import _speaker_org_filter_clauses

    if accessible_profile_ids is not None:
        must_filters = [
            {"term": {"document_type": "profile"}},
            {"terms": {"profile_id": list(accessible_profile_ids)}},
        ]
    else:
        must_filters = [
            {"term": {"document_type": "profile"}},
            {"term": {"user_id": user_id}},
        ]
    # Tenant gate on profile docs (org term, else exclude org-stamped).
    must_filters.extend(_speaker_org_filter_clauses(organization_id))

    query = {
        "size": 10,
        "query": {
            "knn": {
                "embedding": {
                    "vector": embedding.tolist(),
                    "k": 10,
                    "filter": {"bool": {"must": must_filters}},
                }
            }
        },
    }

    response = opensearch_client.search(index=get_speaker_index(), body=query)
    matches = []

    for hit in response["hits"]["hits"]:
        score = 2.0 * hit["_score"] - 1.0  # Convert OS cosinesimil to raw cosine
        source = hit["_source"]
        if score >= threshold:
            matches.append(
                {
                    "profile_id": source.get("profile_id"),
                    "profile_name": source.get("profile_name"),
                    "speaker_count": source.get("speaker_count"),
                    "similarity": score,
                }
            )

    return matches


def _convert_profile_match_to_suggestion(
    match: dict[str, Any],
) -> Optional["ConsolidatedSuggestion"]:
    """
    Convert a profile match dictionary to a ConsolidatedSuggestion.

    Returns None if required fields are missing.
    """
    profile_id = match.get("profile_id")
    profile_name = match.get("profile_name")

    if not profile_id or not profile_name:
        return None

    confidence = match["similarity"]
    embedding_count = match.get("speaker_count", 1)
    reason, auto_accept = _determine_confidence_level(confidence, embedding_count)

    return ConsolidatedSuggestion(
        name=profile_name,
        confidence=confidence,
        suggestion_type="profile",
        profile_id=profile_id,
        embedding_count=embedding_count,
        reason=reason,
        auto_accept=auto_accept,
    )


@dataclass
class ConsolidatedSuggestion:
    """
    Represents a consolidated speaker suggestion with metadata.

    This dataclass encapsulates all information needed by the frontend
    to display speaker suggestions without additional processing.
    """

    name: str
    confidence: float
    suggestion_type: str  # 'profile' or 'llm_analysis'
    profile_id: int | None = None
    embedding_count: int = 0
    reason: str = ""
    auto_accept: bool = False


class SmartSpeakerSuggestionService:
    """
    Advanced speaker suggestion service with intelligent filtering and consolidation.

    This service implements all business logic for speaker suggestions including:
    - Profile-based voice matching using consolidated embeddings
    - Name-based deduplication and consolidation
    - Automatic filtering of unlabeled speakers (SPEAKER_XX)
    - Confidence-based suggestion ranking and auto-accept logic

    All methods are static as this service is stateless and operates on provided data.
    """

    @staticmethod
    def consolidate_suggestions(
        speaker_id: int,
        user_id: int,
        db: Session,
        confidence_threshold: float = 0.5,
        max_suggestions: int = 10,
        accessible_profile_ids: set[int] | None = None,
    ) -> list[ConsolidatedSuggestion]:
        """
        Generate two types of speaker suggestions: LLM and Profile.

        This implements the two-type suggestion system:
        1. LLM suggestions (from import process, stored in suggested_name)
        2. Profile suggestions (speaker-to-profile matching)

        Args:
            speaker_id: ID of the speaker to find suggestions for
            user_id: ID of the user
            db: Database session
            confidence_threshold: Minimum confidence threshold
            max_suggestions: Maximum number of suggestions to return

        Returns:
            List of consolidated suggestions with clear type labels for frontend display.
        """
        suggestions: list[ConsolidatedSuggestion] = []

        # Get the speaker from database to check for LLM suggestions
        speaker = db.query(Speaker).filter(Speaker.id == speaker_id).first()
        if not speaker:
            logger.warning(f"Speaker {speaker_id} not found")
            return suggestions

        # Step 1: LLM suggestions (from speaker identification task)
        # Uses suggestion_source column to distinguish LLM analysis from voice matching
        if (
            speaker.suggested_name
            and speaker.confidence
            and speaker.suggestion_source == "llm_analysis"
        ):
            llm_suggestion = ConsolidatedSuggestion(
                name=speaker.suggested_name,  # type: ignore[arg-type]
                confidence=float(speaker.confidence),
                suggestion_type="llm_analysis",
                reason="AI identified from transcript context",
                auto_accept=float(speaker.confidence) >= 0.75,
            )
            suggestions.append(llm_suggestion)
            logger.info(
                f"Added LLM suggestion: {speaker.suggested_name} "
                f"(confidence: {speaker.confidence:.2f})"
            )

        # Get speaker embedding for profile matching using UUID
        speaker_embedding = get_speaker_embedding(str(speaker.uuid))
        if not speaker_embedding:
            logger.warning(f"No embedding found for speaker {speaker.uuid}")
            return suggestions

        embedding_array = np.array(speaker_embedding)

        # Tenant gate: suggestions for this speaker are scoped to its file's org.
        organization_id = _media_file_org_ids(db, {int(speaker.media_file_id)}).get(
            int(speaker.media_file_id)
        )

        # Step 2: Profile suggestions (speaker-to-profile matching)
        profile_suggestions = SmartSpeakerSuggestionService._get_profile_suggestions_optimized(
            embedding_array,
            user_id,
            db,
            confidence_threshold,
            accessible_profile_ids=accessible_profile_ids,
            organization_id=organization_id,
        )
        suggestions.extend(profile_suggestions)

        # Step 3: Deduplicate by name
        unique_suggestions: dict[str, ConsolidatedSuggestion] = {}
        for suggestion in suggestions:
            dedup_key = f"{suggestion.suggestion_type}:{suggestion.name.lower()}"
            if (
                dedup_key not in unique_suggestions
                or suggestion.confidence > unique_suggestions[dedup_key].confidence
            ):
                unique_suggestions[dedup_key] = suggestion

        # Step 4: Sort by type priority and confidence
        final_suggestions = list(unique_suggestions.values())
        final_suggestions.sort(
            key=lambda s: (
                0 if s.suggestion_type == "profile" else 1,  # Profile > LLM
                -s.confidence,  # Higher confidence first within each type
            )
        )

        # Step 5: Limit per type and filter out unlabeled speakers
        type_counts: dict[str, int] = {}
        filtered_suggestions = []
        for suggestion in final_suggestions:
            if suggestion.name.startswith("SPEAKER_"):
                continue
            stype = suggestion.suggestion_type
            if type_counts.get(stype, 0) >= max_suggestions:
                continue
            type_counts[stype] = type_counts.get(stype, 0) + 1
            filtered_suggestions.append(suggestion)

        logger.info(
            f"Returning {len(filtered_suggestions)} suggestions for speaker {speaker_id} "
            f"(LLM: {len([s for s in filtered_suggestions if s.suggestion_type == 'llm_analysis'])}, "
            f"Profile: {len([s for s in filtered_suggestions if s.suggestion_type == 'profile'])})"
        )
        return filtered_suggestions

    @staticmethod
    def _get_profile_suggestions_optimized(
        embedding: np.ndarray,
        user_id: int,
        db: Session,
        threshold: float,
        accessible_profile_ids: set[int] | None = None,
        organization_id: int | None = None,
    ) -> list[ConsolidatedSuggestion]:
        """Get suggestions from speaker profiles using OpenSearch native similarity."""
        try:
            from app.services.opensearch_service import opensearch_client

            if not opensearch_client:
                logger.warning("OpenSearch client not initialized for profile suggestions")
                return []

            # Check if profiles exist to avoid KNN query on empty sets
            if not _check_opensearch_profiles_exist(
                opensearch_client,
                user_id,
                accessible_profile_ids=accessible_profile_ids,
            ):
                return []

            # Execute kNN search for profile matches
            try:
                opensearch_matches = _execute_profile_knn_search(
                    opensearch_client,
                    embedding,
                    user_id,
                    threshold,
                    accessible_profile_ids=accessible_profile_ids,
                    organization_id=organization_id,
                )
            except Exception as e:
                logger.error(
                    f"Error in OpenSearch profile similarity search <_get_profile_suggestions_optimized - hits>: {e}"
                )
                return []

            # Convert matches to suggestions
            suggestions = []
            for match in opensearch_matches:
                suggestion = _convert_profile_match_to_suggestion(match)
                if suggestion:
                    suggestions.append(suggestion)

            return suggestions

        except Exception as e:
            logger.error(f"Error in optimized profile suggestions: {e}")
            # Fallback to original method
            return SmartSpeakerSuggestionService._get_profile_suggestions(
                embedding,
                user_id,
                db,
                threshold,
                accessible_profile_ids=accessible_profile_ids,
                organization_id=organization_id,
            )

    @staticmethod
    def _get_profile_suggestions(
        embedding: np.ndarray,
        user_id: int,
        db: Session,
        threshold: float,
        accessible_profile_ids: set[int] | None = None,
        organization_id: int | None = None,
    ) -> list[ConsolidatedSuggestion]:
        """Get suggestions from speaker profiles"""
        suggestions = []

        try:
            # Use ProfileEmbeddingService to find matching profiles
            profile_matches = ProfileEmbeddingService.calculate_profile_similarity(
                db,
                embedding.tolist(),
                user_id,
                threshold=threshold,
                accessible_profile_ids=accessible_profile_ids,
                organization_id=organization_id,
            )

            for match in profile_matches:
                # Convert match to suggestion format expected by helper
                suggestion_match = {
                    "profile_id": match["profile_id"],
                    "profile_name": match["profile_name"],
                    "speaker_count": match["embedding_count"],
                    "similarity": match["similarity"],
                }
                suggestion = _convert_profile_match_to_suggestion(suggestion_match)
                if suggestion:
                    suggestions.append(suggestion)

        except Exception as e:
            logger.error(f"Error getting profile suggestions: {e}")

        return suggestions

    @staticmethod
    def consolidate_suggestions_batch(
        speakers: list[Speaker],
        user_id: int,
        db: Session,
        confidence_threshold: float = 0.5,
        max_suggestions: int = 10,
    ) -> dict[int, list[ConsolidatedSuggestion]]:
        """Batch-generate suggestions for multiple speakers with minimal OS round-trips.

        Replaces N calls to consolidate_suggestions() with:
        1. One mget for all speaker embeddings
        2. One search to check if profiles exist
        3. One msearch for all kNN profile queries

        Returns dict mapping speaker.id (int) -> list[ConsolidatedSuggestion].
        """
        from app.services.opensearch_service import get_speaker_embeddings_batch
        from app.services.opensearch_service import msearch_profile_knn_batch
        from app.services.opensearch_service import opensearch_client

        result: dict[int, list[ConsolidatedSuggestion]] = {int(s.id): [] for s in speakers}
        if not speakers:
            return result

        # Phase 1: Collect LLM suggestions (pure DB, no OS calls)
        # Also resolve each speaker's tenant scope (its file's org) for the
        # gated kNN phase below — speakers in the list may span files.
        file_org_map = _media_file_org_ids(db, {int(s.media_file_id) for s in speakers})
        uuid_to_id: dict[str, int] = {}
        org_by_uuid: dict[str, int | None] = {}
        for speaker in speakers:
            sid = int(speaker.id)
            suuid = str(speaker.uuid)
            uuid_to_id[suuid] = sid
            org_by_uuid[suuid] = file_org_map.get(int(speaker.media_file_id))

            if (
                speaker.suggested_name
                and speaker.confidence
                and speaker.suggestion_source == "llm_analysis"
            ):
                result[sid].append(
                    ConsolidatedSuggestion(
                        name=speaker.suggested_name,  # type: ignore[arg-type]
                        confidence=float(speaker.confidence),
                        suggestion_type="llm_analysis",
                        reason="AI identified from transcript context",
                        auto_accept=float(speaker.confidence) >= 0.75,
                    )
                )

        # Phase 2: Batch-fetch all speaker embeddings (1 mget)
        embeddings_map = get_speaker_embeddings_batch(list(uuid_to_id.keys()))

        # Phase 3: Check if profiles exist at all (1 search, same for all speakers)
        profiles_exist = False
        if embeddings_map and opensearch_client:
            profiles_exist = _check_opensearch_profiles_exist(opensearch_client, user_id)

        # Phase 4: Batch kNN profile search (1 msearch per tenant scope).
        # Each speaker's profile search is gated by ITS file's org, so group
        # the embeddings by org (normally a single group) before searching.
        profile_matches_map: dict[str, list[dict[str, Any]]] = {}
        if profiles_exist and embeddings_map:
            by_org: dict[int | None, dict[str, list[float]]] = {}
            for suuid, emb in embeddings_map.items():
                by_org.setdefault(org_by_uuid.get(suuid), {})[suuid] = emb
            for org_id, org_embeddings in by_org.items():
                profile_matches_map.update(
                    msearch_profile_knn_batch(
                        speaker_embeddings=org_embeddings,
                        user_id=user_id,
                        threshold=confidence_threshold,
                        organization_id=org_id,
                    )
                )

        # Phase 5: Assemble per-speaker suggestions
        for speaker in speakers:
            sid = int(speaker.id)
            suuid = str(speaker.uuid)

            for match in profile_matches_map.get(suuid, []):
                suggestion = _convert_profile_match_to_suggestion(match)
                if suggestion:
                    result[sid].append(suggestion)

            # Deduplicate by (type, name)
            unique: dict[str, ConsolidatedSuggestion] = {}
            for s in result[sid]:
                key = f"{s.suggestion_type}:{s.name.lower()}"
                if key not in unique or s.confidence > unique[key].confidence:
                    unique[key] = s

            # Sort and filter
            final = sorted(
                unique.values(),
                key=lambda s: (0 if s.suggestion_type == "profile" else 1, -s.confidence),
            )
            type_counts: dict[str, int] = {}
            filtered: list[ConsolidatedSuggestion] = []
            for s in final:
                if s.name.startswith("SPEAKER_"):
                    continue
                if type_counts.get(s.suggestion_type, 0) >= max_suggestions:
                    continue
                type_counts[s.suggestion_type] = type_counts.get(s.suggestion_type, 0) + 1
                filtered.append(s)

            result[sid] = filtered

        return result

    @staticmethod
    def format_for_api(suggestions: list[ConsolidatedSuggestion]) -> list[dict[str, Any]]:
        """Format consolidated suggestions for API response with clear type labels"""
        formatted = []

        for suggestion in suggestions:
            formatted_suggestion = {
                "name": suggestion.name,
                "confidence": suggestion.confidence,
                "confidence_percentage": f"{suggestion.confidence:.0%}",
                "suggestion_type": suggestion.suggestion_type,
                "reason": suggestion.reason,
                "auto_accept": suggestion.auto_accept,
                "embedding_count": suggestion.embedding_count,
            }

            # Add type-specific fields and labels
            if suggestion.suggestion_type == "profile":
                formatted_suggestion.update(
                    {
                        "profile_id": suggestion.profile_id,
                        "is_verified_profile": True,
                        "type_label": "Profile Match",
                        "type_description": "Verified speaker profile",
                    }
                )
            elif suggestion.suggestion_type == "llm_analysis":
                formatted_suggestion.update(
                    {
                        "is_verified_profile": False,
                        "type_label": "AI Analysis",
                        "type_description": "Context-based identification",
                    }
                )

            formatted.append(formatted_suggestion)

        return formatted
