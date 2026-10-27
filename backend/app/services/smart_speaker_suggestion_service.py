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

from sqlalchemy.orm import Session

from app.core.constants import get_speaker_index
from app.models.media import MediaFile
from app.models.media import Speaker

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


def _dedupe_rank_and_limit(
    suggestions: list["ConsolidatedSuggestion"], max_suggestions: int
) -> list["ConsolidatedSuggestion"]:
    """Keep the best suggestion per (type, name), profile matches first, capped per type.

    Unlabeled diarizer names (``SPEAKER_XX``) are never offered as a suggestion.
    """
    unique: dict[str, ConsolidatedSuggestion] = {}
    for s in suggestions:
        key = f"{s.suggestion_type}:{s.name.lower()}"
        if key not in unique or s.confidence > unique[key].confidence:
            unique[key] = s

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
    return filtered


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
    def consolidate_suggestions_batch(
        speakers: list[Speaker],
        user_id: int,
        file_org_map: dict[int, int | None],
        *,
        voiceprints_off_file_ids: frozenset[int] | set[int],
        confidence_threshold: float = 0.5,
        max_suggestions: int = 10,
    ) -> dict[int, list[ConsolidatedSuggestion]]:
        """Batch-generate suggestions for multiple speakers with minimal OS round-trips.

        Instead of one embedding read, profile probe and kNN per speaker:
        1. One mget for all speaker embeddings
        2. One search to check if profiles exist
        3. One msearch for all kNN profile queries

        **Takes no ``Session``.** Phases 2-4 below are three OpenSearch round trips;
        holding a caller-owned transaction across them is the
        ``session-param-slow-work`` defect. The one thing this needed the DB for —
        each speaker's file-level tenant scope — is now resolved by the caller's
        short read phase (``_media_file_org_ids``) and passed in as ``file_org_map``.

        Args:
            speakers: Speakers to generate suggestions for. Only already-loaded
                columns are read, so no lazy load (and therefore no new
                transaction) can be triggered from here.
            user_id: Owner, for the profile-document filter.
            file_org_map: ``media_file_id -> organization_id`` for every file the
                ``speakers`` span, from ``_media_file_org_ids``.
            voiceprints_off_file_ids: Files whose owner or organization has voiceprints
                switched off (``PrivacyView.voiceprints_off_file_ids``). Their speakers get
                no embedding read and no kNN, only the LLM suggestion. Required, with no
                default, so a caller cannot forget the policy and fail open.

        Returns dict mapping speaker.id (int) -> list[ConsolidatedSuggestion].
        """
        from app.services.opensearch_service import get_speaker_embeddings_batch
        from app.services.opensearch_service import msearch_profile_knn_batch
        from app.services.opensearch_service import opensearch_client

        result: dict[int, list[ConsolidatedSuggestion]] = {int(s.id): [] for s in speakers}
        if not speakers:
            return result

        # Phase 1: Collect LLM suggestions from data already loaded on the rows
        # (no query, no OS call). Each speaker's tenant scope — its file's org,
        # for the gated kNN phase below — comes from the caller-supplied
        # ``file_org_map``; speakers in the list may span files.
        uuid_to_id: dict[str, int] = {}
        org_by_uuid: dict[str, int | None] = {}
        for speaker in speakers:
            sid = int(speaker.id)
            suuid = str(speaker.uuid)
            if int(speaker.media_file_id) not in voiceprints_off_file_ids:
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
        embeddings_map = get_speaker_embeddings_batch(list(uuid_to_id.keys())) if uuid_to_id else {}

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

            result[sid] = _dedupe_rank_and_limit(result[sid], max_suggestions)

        return result

    @staticmethod
    def without_refused(
        db: Session, suggestions_by_speaker: dict[int, list[ConsolidatedSuggestion]]
    ) -> dict[int, list[ConsolidatedSuggestion]]:
        """Drop every suggestion of an identity the user ruled out for that speaker.

        A speaker unassigned from (or that rejected) a profile is never offered it again —
        the same rule retroactive matching applies (issue #1233). A short read, no
        OpenSearch: call it from the read phase, after the batch's round trips.
        """
        from app.services.speaker_cleanup import blocked_profile_names

        refused = blocked_profile_names(db, list(suggestions_by_speaker))
        return {
            sid: [s for s in items if s.name.casefold() not in refused.get(sid, set())]
            for sid, items in suggestions_by_speaker.items()
        }

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
