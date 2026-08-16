import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Response
from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth import get_current_admin_user
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.media import TranscriptSegment
from app.models.user import User
from app.schemas.media import Speaker as SpeakerSchema
from app.schemas.media import SpeakerUpdate
from app.services.opensearch_service import update_speaker_display_name
from app.services.permission_service import PermissionService
from app.services.speaker_status_service import SpeakerStatusService
from app.utils.error_handlers import ErrorHandler
from app.utils.uuid_helpers import get_speaker_by_uuid

logger = logging.getLogger(__name__)

# Whitelist of fields that can be updated via the speaker update endpoint
SPEAKER_UPDATABLE_FIELDS = {"name", "display_name", "suggested_name", "verified"}

# Speaker suggestion constants
SPEAKER_SUGGESTION_MIN_CONFIDENCE = 0.5
SPEAKER_SUGGESTION_MAX_COUNT = 5

router = APIRouter()


# =============================================================================
# STATIC ROUTES FIRST - These must come before parameterized routes
# =============================================================================


@router.post("", response_model=SpeakerSchema)
def create_speaker(
    speaker: SpeakerUpdate,
    media_file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Create a new speaker for a specific media file
    """
    from app.utils.uuid_helpers import get_file_by_uuid_with_permission

    # Get media file by UUID and verify permission (tenant-gated via ctx.org_id)
    media_file = get_file_by_uuid_with_permission(
        db,
        media_file_uuid,
        current_user.id,
        is_admin=current_user.is_admin,
        organization_id=ctx.org_id,
    )

    # Generate a UUID for the new speaker
    speaker_uuid = str(uuid.uuid4())

    new_speaker = Speaker(
        name=speaker.name,
        display_name=speaker.display_name,
        uuid=speaker_uuid,
        user_id=current_user.id,
        media_file_id=media_file.id,  # Use internal integer ID
        verified=speaker.verified if speaker.verified is not None else False,
    )

    # If display_name is provided, mark as verified
    if speaker.display_name and speaker.display_name.strip():
        new_speaker.verified = True  # type: ignore[assignment]

    db.add(new_speaker)
    db.commit()
    db.refresh(new_speaker)

    # Add computed status fields
    SpeakerStatusService.add_computed_status(new_speaker)

    return new_speaker


# --- Helper functions for list_speakers ---


def _filter_speakers_query(
    query: Any, verified_only: bool, for_filter: bool, file_id: int | None
) -> Any:
    """Apply filters to the speakers query."""
    if verified_only:
        query = query.filter(Speaker.verified)

    if for_filter:
        query = query.filter(
            Speaker.display_name.isnot(None),
            Speaker.display_name != "",
            ~Speaker.display_name.op("~")(r"^SPEAKER_\d+$"),
        )

    if file_id is not None:
        query = query.filter(Speaker.media_file_id == file_id)

    return query


def _sort_speakers(speakers: list[Speaker]) -> list[Speaker]:
    """Sort speakers by SPEAKER_XX numbering for consistent ordering."""

    def get_speaker_number(speaker: Speaker) -> int:
        match = re.match(r"^SPEAKER_(\d+)$", str(speaker.name))
        return int(match.group(1)) if match else 999

    # Always sort by original speaker number first, regardless of verification status
    # This ensures SPEAKER_01, SPEAKER_02, SPEAKER_03... order is maintained
    speakers.sort(key=lambda s: get_speaker_number(s))
    return speakers


def _get_unique_speakers_for_filter(
    db: Session, current_user: User, organization_id: OrgScope = UNSCOPED
) -> list[dict[str, Any]]:
    """
    Get unique speakers by display name for filter use with media file counts.

    Includes speakers from both owned and shared files so that shared-collection
    speakers appear in the filter sidebar. ``organization_id`` tenant-gates the
    accessible-file resolution (default-deny across scopes).

    Uses DISTINCT ON to pick a deterministic representative speaker per display
    name (the one with the lowest id), then counts media files per display name.

    Returns list of dicts with uuid, name, display_name, and media_count.
    """
    from sqlalchemy import func
    from sqlalchemy import select

    from app.services.permission_service import PermissionService

    # Build base filters — admins see all speakers, others see accessible files only
    base_filter = [
        Speaker.display_name.isnot(None),
        Speaker.display_name != "",
        ~Speaker.display_name.op("~")(r"^SPEAKER_\d+$"),
    ]
    if not current_user.is_admin:
        accessible_sq = PermissionService.get_accessible_file_ids_subquery(
            db, current_user.id, organization_id=organization_id
        )
        base_filter.append(Speaker.media_file_id.in_(select(accessible_sq)))

    # Step 1: Get one representative speaker per display_name using DISTINCT ON
    # This gives deterministic UUID selection (lowest id wins)
    representative_speakers = (
        db.query(Speaker)
        .filter(*base_filter)
        .distinct(Speaker.display_name)
        .order_by(Speaker.display_name, Speaker.id)
        .all()
    )

    # Step 2: Get media file counts per display_name in a single query
    count_rows = (
        db.query(
            Speaker.display_name,
            func.count(func.distinct(Speaker.media_file_id)).label("media_count"),
        )
        .filter(*base_filter)
        .group_by(Speaker.display_name)
        .all()
    )
    media_counts = {row.display_name: row.media_count for row in count_rows}

    # Step 3: Combine and sort by media_count descending, then display_name
    results = [
        {
            "uuid": str(speaker.uuid),
            "name": speaker.name,
            "display_name": speaker.display_name,
            "media_count": media_counts.get(speaker.display_name, 0),
        }
        for speaker in representative_speakers
    ]
    results.sort(key=lambda r: (-r["media_count"], r["display_name"] or ""))

    return results


def _resolve_file_uuid_to_id(
    file_uuid: str | None,
    current_user: User,
    db: Session,
    organization_id: OrgScope = UNSCOPED,
) -> int | None:
    """Convert file UUID to internal ID if provided (tenant-gated lookup)."""
    if not file_uuid:
        return None
    from app.utils.uuid_helpers import get_file_by_uuid_with_permission

    media_file = get_file_by_uuid_with_permission(
        db,
        file_uuid,
        current_user.id,
        is_admin=current_user.is_admin,
        organization_id=organization_id,
    )
    return media_file.id


def _get_segment_counts_for_speakers(speaker_ids: list[int], db: Session) -> dict[int, int]:
    """Pre-calculate segment counts for all speakers in one query."""
    from sqlalchemy import func

    if not speaker_ids:
        return {}

    count_results = (
        db.query(TranscriptSegment.speaker_id, func.count(TranscriptSegment.id))
        .filter(TranscriptSegment.speaker_id.in_(speaker_ids))
        .group_by(TranscriptSegment.speaker_id)
        .all()
    )
    return {speaker_id: count for speaker_id, count in count_results}


def _get_first_segment_timestamps_for_speakers(
    speaker_ids: list[int], file_id: int, db: Session
) -> dict[int, list[dict[str, Any]]]:
    """Get the first 4 segment timestamps per speaker with global segment index.

    Uses SQL window functions for efficient single-pass computation:
    - ROW_NUMBER() OVER (PARTITION BY speaker_id ORDER BY start_time) for per-speaker rank
    - ROW_NUMBER() OVER (ORDER BY start_time) for global segment position

    Returns:
        Dict mapping speaker_id to list of timestamp dicts with uuid, start_time,
        and segment_index (0-based global position within the file).
    """
    from sqlalchemy import text

    if not speaker_ids:
        return {}

    # Use a raw SQL query with window functions for efficiency
    # This gets all segments for the file, ranks them per-speaker and globally,
    # then filters to only the first 4 per speaker
    sql = text("""
        WITH ranked AS (
            SELECT
                ts.speaker_id,
                ts.uuid,
                ts.start_time,
                ROW_NUMBER() OVER (
                    PARTITION BY ts.speaker_id ORDER BY ts.start_time
                ) AS speaker_rank,
                ROW_NUMBER() OVER (
                    ORDER BY ts.start_time
                ) - 1 AS segment_index
            FROM transcript_segment ts
            WHERE ts.media_file_id = :file_id
              AND ts.speaker_id = ANY(:speaker_ids)
        )
        SELECT speaker_id, uuid, start_time, segment_index
        FROM ranked
        WHERE speaker_rank <= 4
        ORDER BY speaker_id, start_time
    """)

    results = db.execute(sql, {"file_id": file_id, "speaker_ids": speaker_ids}).fetchall()

    timestamps: dict[int, list[dict[str, Any]]] = {}
    for row in results:
        sid = row[0]
        if sid not in timestamps:
            timestamps[sid] = []
        timestamps[sid].append(
            {
                "uuid": str(row[1]),
                "start_time": float(row[2]),
                "segment_index": int(row[3]),
            }
        )

    return timestamps


def _get_profile_suggestions(raw_cross_video_matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract profile suggestions from cross-video matches.

    Filters out LLM suggestions since those are displayed separately in the UI.
    All non-LLM suggestions are profile suggestions.
    """
    return [
        {
            "name": match["name"],
            "confidence": match["confidence"],
            "confidence_percentage": match["confidence_percentage"],
            "suggestion_type": match["suggestion_type"],
            "reason": match.get("reason", ""),
        }
        for match in raw_cross_video_matches
        if match.get("suggestion_type") != "llm_analysis"
        and float(match.get("confidence", 0)) >= 0.50
        and match.get("name") is not None
        and str(match.get("name", "")).strip()
        and match.get("suggestion_type")
    ]


def _compute_suggested_name(speaker: Speaker) -> str | None:
    """Return the speaker's suggested name if available."""
    return str(speaker.suggested_name) if speaker.suggested_name else None


def _get_suggestion_source(speaker: Speaker) -> str | None:
    """Determine suggestion source for frontend display."""
    if not (speaker.suggested_name and speaker.confidence):
        return None

    # Use the persisted suggestion_source column if available
    if speaker.suggestion_source:
        return str(speaker.suggestion_source)

    # Legacy fallback for speakers created before suggestion_source column
    if hasattr(speaker, "_suggestion_source"):
        return str(speaker._suggestion_source)

    # Default assumption for legacy data: embedding match
    return "voice_match"


def _compute_display_flags(
    speaker: Speaker,
    suggested_name: str | None,
    suggestion_source: str | None,
    profile_suggestions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pre-compute frontend display flags."""
    has_llm_suggestion = bool(
        suggested_name and speaker.confidence and suggestion_source == "llm_analysis"
    )
    total_suggestions = (1 if has_llm_suggestion else 0) + len(profile_suggestions)
    show_suggestions_section = has_llm_suggestion or len(profile_suggestions) > 0

    # Compute best profile suggestion confidence for input styling
    profile_confidence = 0.0
    if profile_suggestions:
        profile_confidence = max(s["confidence"] for s in profile_suggestions)

    is_high_confidence = bool(
        profile_confidence >= 0.75 and suggested_name and not speaker.display_name
    )
    is_medium_confidence = bool(
        profile_confidence >= 0.5
        and profile_confidence < 0.75
        and suggested_name
        and not speaker.display_name
    )

    # Pre-compute placeholder text
    if is_high_confidence:
        input_placeholder = suggested_name
    elif suggested_name and suggestion_source == "metadata_hint":
        input_placeholder = f"From metadata: {suggested_name}"
    elif suggested_name:
        input_placeholder = f"Suggested: {suggested_name}"
    else:
        input_placeholder = f"Label {speaker.name}"

    # Pre-compute profile badge visibility
    show_profile_badge = bool(
        speaker.profile
        and speaker.display_name
        and speaker.display_name.strip()
        and not speaker.display_name.startswith("SPEAKER_")
    )

    return {
        "has_llm_suggestion": has_llm_suggestion,
        "total_suggestions": total_suggestions,
        "show_suggestions_section": show_suggestions_section,
        "is_high_confidence": is_high_confidence,
        "is_medium_confidence": is_medium_confidence,
        "input_placeholder": input_placeholder,
        "show_profile_badge": show_profile_badge,
    }


def _is_labeled_speaker(speaker: Speaker) -> bool:
    """Check if a speaker is labeled (has a non-SPEAKER_ display name)."""
    return bool(
        speaker.display_name
        and speaker.display_name.strip()
        and not speaker.display_name.startswith("SPEAKER_")
    )


def _build_speaker_dict(
    speaker: Speaker,
    current_user: User,
    suggested_name: str | None,
    suggestion_source: str | None,
    profile_suggestions: list[dict[str, Any]],
    cross_video_matches: list[dict[str, Any]],
    display_flags: dict[str, Any],
    segment_count: int,
    segment_timestamps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the speaker dictionary for API response."""
    # Extract cross-reference alignment data stored in attribute_confidence JSONB
    attr_conf: dict[str, Any] = dict(speaker.attribute_confidence or {})
    gender_alignment = attr_conf.get("alignment")  # "match", "mismatch", or None
    gender_alignment_hint = attr_conf.get("alignment_hint")  # e.g., "Joe Rogan"
    metadata_hints: list[dict[str, Any]] = attr_conf.get("metadata_hints", [])

    # Return only numeric confidence values; string fields are exposed separately
    numeric_confidence: dict[str, Any] | None = (
        {
            k: v
            for k, v in attr_conf.items()
            if isinstance(v, (int, float))
            and k not in ("alignment", "alignment_hint", "metadata_hints")
        }
        if attr_conf
        else None
    )

    # `speaker.created_at` is nullable — a server_default fills the column only for an
    # INSERT that omits it, so a raw-SQL insert, a backfill or an explicit UPDATE can leave
    # NULL. This response is a plain dict, so an unknown creation time is reported as null
    # rather than raising AssertionError (which is also a no-op under `python -O`, leaving
    # the AttributeError on `.isoformat()` as the real failure mode).
    speaker_dict: dict[str, Any] = {
        "uuid": str(speaker.uuid),
        "name": speaker.name,
        "display_name": speaker.display_name or "",  # Handle nulls in backend
        "suggested_name": suggested_name,
        "verified": speaker.verified,
        "user_id": str(current_user.uuid),  # Use user UUID
        "confidence": speaker.confidence,
        "suggestion_source": suggestion_source,
        "created_at": speaker.created_at.isoformat() if speaker.created_at else None,
        "media_file_id": str(speaker.media_file.uuid)
        if speaker.media_file
        else speaker.media_file_id,
        "profile": None,
        "profile_suggestions": profile_suggestions,
        "cross_video_matches": cross_video_matches,
        "needsCrossMediaCall": _is_labeled_speaker(speaker),
        "segment_count": segment_count,
        "segment_timestamps": segment_timestamps or [],
        # AI-predicted speaker attributes
        "predicted_gender": speaker.predicted_gender,
        "predicted_age_range": speaker.predicted_age_range,
        "attribute_confidence": numeric_confidence,
        "attributes_predicted_at": speaker.attributes_predicted_at.isoformat()
        if speaker.attributes_predicted_at
        else None,
        # Cross-reference alignment data from metadata hints
        "gender_alignment": gender_alignment,
        "gender_alignment_hint": gender_alignment_hint,
        "metadata_hints": metadata_hints,
        # Pre-computed display flags
        **display_flags,
    }

    # Add profile information if speaker is assigned to a profile
    if speaker.profile_id and speaker.profile:
        speaker_dict["profile"] = {
            "uuid": str(speaker.profile.uuid) if speaker.profile.uuid else None,
            "name": speaker.profile.name,
            "description": speaker.profile.description,
        }

    return speaker_dict


def _process_single_speaker(
    speaker: Speaker,
    current_user: User,
    segment_count: int,
    db: Session,
    segment_timestamps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Process a single speaker and build its response dictionary."""
    from app.services.smart_speaker_suggestion_service import SmartSpeakerSuggestionService

    # Compute status information using SpeakerStatusService
    status_info = SpeakerStatusService.compute_speaker_status(speaker)
    # Use setattr to avoid mypy Column type issues
    speaker.computed_status = status_info["computed_status"]  # type: ignore[assignment]
    speaker.status_text = status_info["status_text"]  # type: ignore[assignment]
    speaker.status_color = status_info["status_color"]  # type: ignore[assignment]
    speaker.resolved_display_name = status_info["resolved_display_name"]  # type: ignore[assignment]
    speaker.profile_name = status_info["profile_name"]  # type: ignore[assignment,attr-defined]
    speaker.profile_status = status_info["profile_status"]  # type: ignore[assignment,attr-defined]

    # Get smart, consolidated speaker suggestions
    smart_suggestions = SmartSpeakerSuggestionService.consolidate_suggestions(
        speaker_id=speaker.id,
        user_id=current_user.id,
        db=db,
        confidence_threshold=SPEAKER_SUGGESTION_MIN_CONFIDENCE,
        max_suggestions=SPEAKER_SUGGESTION_MAX_COUNT,
    )

    # Format suggestions for API response
    raw_cross_video_matches = SmartSpeakerSuggestionService.format_for_api(smart_suggestions)

    # Get profile suggestions (all non-LLM suggestions)
    profile_suggestions = _get_profile_suggestions(raw_cross_video_matches)

    # Cross-video matches: only populated via cross-media API for labeled speakers
    cross_video_matches: list[dict[str, Any]] = []

    # Compute suggested name
    suggested_name = _compute_suggested_name(speaker)

    # Get suggestion source
    suggestion_source = _get_suggestion_source(speaker)

    # Compute display flags
    display_flags = _compute_display_flags(
        speaker, suggested_name, suggestion_source, profile_suggestions
    )

    # Build the speaker dictionary
    return _build_speaker_dict(
        speaker,
        current_user,
        suggested_name,
        suggestion_source,
        profile_suggestions,
        cross_video_matches,
        display_flags,
        segment_count,
        segment_timestamps,
    )


def _process_single_speaker_with_suggestions(
    speaker: Speaker,
    current_user: User,
    segment_count: int,
    smart_suggestions: list,
    segment_timestamps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Process a single speaker with pre-fetched suggestions (avoids N+1 OpenSearch)."""
    from app.services.smart_speaker_suggestion_service import SmartSpeakerSuggestionService

    status_info = SpeakerStatusService.compute_speaker_status(speaker)
    speaker.computed_status = status_info["computed_status"]  # type: ignore[assignment]
    speaker.status_text = status_info["status_text"]  # type: ignore[assignment]
    speaker.status_color = status_info["status_color"]  # type: ignore[assignment]
    speaker.resolved_display_name = status_info["resolved_display_name"]  # type: ignore[assignment]
    speaker.profile_name = status_info["profile_name"]  # type: ignore[assignment,attr-defined]
    speaker.profile_status = status_info["profile_status"]  # type: ignore[assignment,attr-defined]

    raw_cross_video_matches = SmartSpeakerSuggestionService.format_for_api(smart_suggestions)
    profile_suggestions = _get_profile_suggestions(raw_cross_video_matches)
    cross_video_matches: list[dict[str, Any]] = []
    suggested_name = _compute_suggested_name(speaker)
    suggestion_source = _get_suggestion_source(speaker)
    display_flags = _compute_display_flags(
        speaker, suggested_name, suggestion_source, profile_suggestions
    )

    return _build_speaker_dict(
        speaker,
        current_user,
        suggested_name,
        suggestion_source,
        profile_suggestions,
        cross_video_matches,
        display_flags,
        segment_count,
        segment_timestamps,
    )


def _create_no_cache_response(content: list[dict[str, Any]]) -> JSONResponse:
    """Create a JSONResponse with cache-busting headers."""
    return JSONResponse(
        content=content,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("", response_model=None)
def list_speakers(
    verified_only: bool = False,
    file_uuid: str | None = None,
    for_filter: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> JSONResponse | list[dict[str, Any]]:
    """
    List all speakers for the current user with intelligent suggestions.

    This endpoint provides comprehensive speaker data including:
    - Basic speaker information and verification status
    - Automatic profile assignments when speakers are labeled
    - Smart profile suggestions via embedding similarity
    - Pre-filtered, consolidated suggestions ready for frontend display

    All business logic for speaker suggestions and filtering is handled server-side.
    The frontend receives clean, ready-to-display data without needing additional processing.

    Args:
        verified_only (bool): If true, return only verified speakers.
        file_uuid (Optional[str]): If provided, return only speakers associated with this file.
        for_filter (bool): If true, return only speakers with distinct display names for filtering.

    Returns:
        List[dict]: Speaker objects with embedded suggestion data and profile information.
    """
    try:
        from sqlalchemy.orm import joinedload

        # Fast path: filter mode only needs aggregated display names
        # Skip loading all speaker objects, profiles, and media files
        if for_filter:
            return _get_unique_speakers_for_filter(db, current_user, organization_id=ctx.org_id)

        # Convert file_uuid to file_id if provided (tenant-gated via ctx.org_id)
        file_id = _resolve_file_uuid_to_id(file_uuid, current_user, db, organization_id=ctx.org_id)

        query = db.query(Speaker).options(
            joinedload(Speaker.profile), joinedload(Speaker.media_file)
        )
        # Admins see all speakers; when file_id is provided, permission was
        # already checked by _resolve_file_uuid_to_id; otherwise scope to owner.
        if current_user.is_admin:
            pass  # Admins see all speakers
        elif file_id is not None:
            pass  # Viewing specific file — permission already checked
        else:
            query = query.filter(Speaker.user_id == current_user.id)
        query = _filter_speakers_query(query, verified_only, False, file_id)
        speakers = query.all()
        speakers = _sort_speakers(speakers)

        # Pre-calculate segment counts for all speakers in one query
        speaker_ids = [s.id for s in speakers]
        segment_counts = _get_segment_counts_for_speakers(speaker_ids, db)

        # Pre-fetch first 4 segment timestamps per speaker for jump-to-timestamp UI
        all_timestamps: dict[int, list[dict[str, Any]]] = {}
        if file_id is not None and speaker_ids:
            all_timestamps = _get_first_segment_timestamps_for_speakers(speaker_ids, file_id, db)

        # Batch-fetch all speaker suggestions (3 OS calls total vs 3N before).
        # The tenant scope each suggestion query is gated by is resolved HERE,
        # against the request session, and handed over as plain data — the batch
        # helper deliberately takes no ``Session`` so its three OpenSearch round
        # trips cannot be reached with a transaction one frame up.
        from app.services.smart_speaker_suggestion_service import SmartSpeakerSuggestionService
        from app.services.smart_speaker_suggestion_service import _media_file_org_ids

        file_org_map = _media_file_org_ids(db, {int(s.media_file_id) for s in speakers})
        batch_suggestions = SmartSpeakerSuggestionService.consolidate_suggestions_batch(
            speakers=speakers,
            user_id=current_user.id,
            file_org_map=file_org_map,
            confidence_threshold=SPEAKER_SUGGESTION_MIN_CONFIDENCE,
            max_suggestions=SPEAKER_SUGGESTION_MAX_COUNT,
        )

        # Process each speaker with pre-fetched suggestions
        result = [
            _process_single_speaker_with_suggestions(
                speaker,
                current_user,
                segment_counts.get(speaker.id, 0),
                batch_suggestions.get(speaker.id, []),
                all_timestamps.get(speaker.id),
            )
            for speaker in speakers
        ]

        return _create_no_cache_response(result)

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. Without this, the broad handler
        # below swallowed the 403 that `_resolve_file_uuid_to_id` raises for a caller with
        # no permission on the requested file and reported it as a 500 — so an
        # authorization denial reached the client as "Internal server error while loading
        # speakers", indistinguishable from a real fault, and was logged at error level.
        # The test that should have caught this asserted `status_code in (403, 500)`, which
        # accepted both (issue #431).
        raise
    except Exception as e:
        logger.error(f"Error in list_speakers: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while loading speakers",
        ) from e


@router.post("/cleanup-orphaned-embeddings", response_model=dict[str, Any])
def cleanup_orphaned_embeddings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """
    Clean up orphaned speaker embeddings in OpenSearch for non-existent MediaFiles.
    """
    try:
        from app.services.opensearch_service import cleanup_orphaned_speaker_embeddings

        deleted_count = cleanup_orphaned_speaker_embeddings(current_user.id)

        return {
            "status": "success",
            "deleted_count": deleted_count,
            "message": f"Cleaned up {deleted_count} orphaned speaker embeddings",
        }
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception(f"Error during cleanup: {e}")
        raise ErrorHandler.internal_error() from e


def _collect_index_debug_documents(user_id: int) -> dict[str, Any]:
    """Read the speaker and profile documents for ``user_id`` out of the search index.

    **Takes no ``Session``.** Two OpenSearch searches (100 hits each) used to run on
    the request session in ``debug_cross_media_data``, so a slow or unreachable
    cluster held the request's Postgres transaction — and therefore ACCESS SHARE on
    every table it had read — for the whole round trip. Returns plain dicts;
    the caller merges them into its report.

    The diagnostic contract is unchanged: a failure is reported in the payload
    (``opensearch_error``) rather than failing the endpoint.
    """
    section: dict[str, Any] = {"opensearch_speakers": [], "opensearch_profiles": []}
    try:
        from app.core.constants import get_speaker_index
        from app.services.opensearch_service import opensearch_client

        if not opensearch_client:
            return section

        speaker_index = get_speaker_index()
        # Query all speaker documents for this user
        query = {
            "size": 100,
            "query": {
                "bool": {
                    "must": [{"term": {"user_id": user_id}}],
                    "must_not": [
                        {"exists": {"field": "document_type"}}
                    ],  # Only speakers, not profiles
                }
            },
        }

        response = opensearch_client.search(index=speaker_index, body=query)
        for hit in response["hits"]["hits"]:
            source = hit["_source"]
            section["opensearch_speakers"].append(
                {
                    "opensearch_id": hit["_id"],
                    "speaker_id": source.get("speaker_id"),
                    "display_name": source.get("display_name"),
                    "profile_id": source.get("profile_id"),
                    "media_file_id": source.get("media_file_id"),
                    "user_id": source.get("user_id"),
                }
            )

        # Query profile documents
        profile_query = {
            "size": 100,
            "query": {
                "bool": {
                    "must": [
                        {"term": {"user_id": user_id}},
                        {"term": {"document_type": "profile"}},
                    ]
                }
            },
        }

        profile_response = opensearch_client.search(index=speaker_index, body=profile_query)
        for hit in profile_response["hits"]["hits"]:
            source = hit["_source"]
            section["opensearch_profiles"].append(
                {
                    "opensearch_id": hit["_id"],
                    "profile_id": source.get("profile_id"),
                    "profile_name": source.get("profile_name"),
                    "speaker_count": source.get("speaker_count"),
                    "user_id": source.get("user_id"),
                }
            )

    except Exception as e:
        # Diagnostic endpoint: the OpenSearch section is optional, and the
        # error is surfaced in the payload rather than failing the report.
        logger.exception("OpenSearch section of the speaker debug report failed")
        section["opensearch_error"] = str(e)

    return section


@router.get("/debug/cross-media-data", response_model=dict[str, Any])
def debug_cross_media_data(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """
    Debug endpoint to examine cross-media matching data in PostgreSQL and OpenSearch.
    """
    try:
        debug_info: dict[str, Any] = {
            "user_id": current_user.id,
            "media_files": [],
            "speakers": [],
            "profiles": [],
            "opensearch_speakers": [],
            "opensearch_profiles": [],
        }

        # Get all media files for this user (project only needed fields)
        media_rows = (
            db.query(MediaFile.id, MediaFile.filename, MediaFile.title, MediaFile.status)
            .filter(MediaFile.user_id == current_user.id)
            .all()
        )
        for mf in media_rows:
            debug_info["media_files"].append(
                {
                    "id": mf.id,
                    "filename": mf.filename,
                    "title": mf.title,
                    "status": mf.status.value if mf.status else None,
                }
            )

        # Get all speakers for this user (project only needed fields)
        speaker_rows = (
            db.query(
                Speaker.id,
                Speaker.name,
                Speaker.display_name,
                Speaker.profile_id,
                Speaker.media_file_id,
                Speaker.verified,
                Speaker.confidence,
            )
            .filter(Speaker.user_id == current_user.id)
            .order_by(Speaker.media_file_id, Speaker.id)
            .all()
        )

        for s in speaker_rows:
            debug_info["speakers"].append(
                {
                    "id": s.id,
                    "name": s.name,
                    "display_name": s.display_name,
                    "profile_id": s.profile_id,
                    "media_file_id": s.media_file_id,
                    "verified": s.verified,
                    "confidence": s.confidence,
                }
            )

        # Get all speaker profiles
        profiles = db.query(SpeakerProfile).filter(SpeakerProfile.user_id == current_user.id).all()
        for profile in profiles:
            debug_info["profiles"].append(
                {
                    "id": profile.id,
                    "name": profile.name,
                    "uuid": profile.uuid,
                    "description": profile.description,
                }
            )

        # Every Postgres read is done. End the request's transaction BEFORE the
        # search-index round trips below: a plain SELECT holds ACCESS SHARE for the
        # life of its transaction, so waiting on a slow cluster here would queue
        # ALTER TABLE (an Alembic upgrade) and pin the vacuum horizon for nothing.
        # This handler performs no writes, so the commit only closes the read.
        db.commit()

        # Get the index-side view. No session is held across it — the helper does
        # not take one.
        debug_info.update(_collect_index_debug_documents(current_user.id))

        # Add summary analysis
        debug_info["analysis"] = {
            "total_media_files": len(debug_info["media_files"]),
            "total_speakers": len(debug_info["speakers"]),
            "total_profiles": len(debug_info["profiles"]),
            "opensearch_speaker_count": len(debug_info["opensearch_speakers"]),
            "opensearch_profile_count": len(debug_info["opensearch_profiles"]),
        }

        return debug_info

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception(f"Error in debug endpoint: {e}")
        raise ErrorHandler.internal_error() from e


@router.get("/debug/cross-media-by-name", response_model=dict[str, Any])
def debug_cross_media_by_name(
    speaker_name: str = Query(..., description="Speaker display name to search for"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """
    Debug endpoint to test cross-media logic for a specific speaker name.
    """
    try:
        # Find all matching speakers
        query = db.query(Speaker).filter(Speaker.display_name == speaker_name)
        if not current_user.is_admin:
            query = query.filter(Speaker.user_id == current_user.id)
        matching_speakers = query.all()

        results: dict[str, Any] = {
            "speakers_found": len(matching_speakers),
            "cross_media_results": [],
        }

        for speaker in matching_speakers:
            # Test the cross-media logic for this speaker
            cross_media_result: dict[str, Any] = {
                "speaker_id": speaker.id,
                "speaker_name": speaker.name,
                "media_file_id": speaker.media_file_id,
                "profile_id": speaker.profile_id,
                "verified": speaker.verified,
                "occurrences": [],
            }

            # Replicate the cross-media logic
            if speaker.profile_id:
                # Speaker has a profile - find all instances of this profile
                profile_q = (
                    db.query(Speaker)
                    .join(MediaFile)
                    .filter(
                        Speaker.profile_id == speaker.profile_id,
                    )
                )
                if not current_user.is_admin:
                    profile_q = profile_q.filter(Speaker.user_id == current_user.id)
                profile_speakers = profile_q.all()

                cross_media_result["method_used"] = "profile_based"
                cross_media_result["profile_speakers_found"] = len(profile_speakers)

                for profile_speaker in profile_speakers:
                    media_file = profile_speaker.media_file
                    if media_file:
                        occurrence = {
                            "speaker_id": profile_speaker.id,
                            "media_file_id": media_file.id,
                            "media_file_title": media_file.title or media_file.filename,
                            "same_speaker": profile_speaker.id == speaker.id,
                        }
                        cross_media_result["occurrences"].append(occurrence)

            else:
                # Speaker has no profile - search by display_name
                similar_q = (
                    db.query(Speaker)
                    .join(MediaFile)
                    .filter(
                        Speaker.display_name == speaker.display_name,
                        Speaker.id != speaker.id,  # Exclude self
                    )
                )
                if not current_user.is_admin:
                    similar_q = similar_q.filter(Speaker.user_id == current_user.id)
                similar_speakers = similar_q.all()

                cross_media_result["method_used"] = "display_name_based"
                cross_media_result["similar_speakers_found"] = len(similar_speakers)

                # Add self first
                if speaker.media_file:
                    cross_media_result["occurrences"].append(
                        {
                            "speaker_id": speaker.id,
                            "media_file_id": speaker.media_file.id,
                            "media_file_title": speaker.media_file.title
                            or speaker.media_file.filename,
                            "same_speaker": True,
                        }
                    )

                # Add similar speakers
                for similar_speaker in similar_speakers:
                    similar_media_file = similar_speaker.media_file
                    if similar_media_file:
                        occurrence = {
                            "speaker_id": similar_speaker.id,
                            "media_file_id": similar_media_file.id,
                            "media_file_title": similar_media_file.title
                            or similar_media_file.filename,
                            "same_speaker": False,
                        }
                        cross_media_result["occurrences"].append(occurrence)

            results["cross_media_results"].append(cross_media_result)

        return results

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception(f"Error in cross-media-by-name debug endpoint: {e}")
        raise ErrorHandler.internal_error() from e


# =============================================================================
# ROUTE ORDERING IS CRITICAL IN FASTAPI
#
# FastAPI matches routes in definition order. More specific routes must be
# defined BEFORE less specific ones, otherwise the generic routes will
# intercept requests meant for specific nested paths.
#
# CORRECT ORDER:
#   1. Static routes (no parameters): /, /cleanup-orphaned-embeddings, etc.
#   2. Nested parameterized routes: /{uuid}/verify, /{uuid}/cross-media
#   3. Single-param routes (catch-all): /{uuid}
#
# WRONG: /{uuid} before /{uuid}/cross-media
#   -> Request to /speakers/abc/cross-media matches /{uuid} with uuid="abc"
# =============================================================================


# --- SECTION: NESTED PARAMETERIZED ROUTES (must come before single-param routes) ---


@router.get("/{speaker_uuid}/cross-media", response_model=list[dict[str, Any]])
def get_speaker_cross_media_occurrences(
    speaker_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> list[dict[str, Any]]:
    """
    Get all media files where this speaker (or their profile) appears.
    """
    try:
        speaker = get_speaker_by_uuid(db, speaker_uuid)
        file_perm = (
            "owner"
            if current_user.is_admin
            else PermissionService.get_file_permission(
                db, int(speaker.media_file_id), current_user.id
            )
        )
        if not file_perm:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

        if speaker.profile_id:
            result = _get_profile_based_occurrences(speaker, current_user, db)
        else:
            result = _get_display_name_based_occurrences(speaker, current_user, db)

        # Sort by confidence (highest first), with same_speaker files prioritized
        result.sort(key=lambda x: (x["same_speaker"], x.get("confidence") or 0.0), reverse=True)

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error getting cross-media occurrences: {e}")
        raise ErrorHandler.internal_error() from e


@router.post("/{speaker_uuid}/verify", response_model=dict[str, Any])
def verify_speaker_identification(
    speaker_uuid: str,
    action: str,  # 'accept', 'reject', 'create_profile'
    profile_uuid: str | None = None,
    profile_name: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """
    Verify or reject speaker identification suggestions.

    Actions:
    - 'accept': Accept suggested profile match
    - 'reject': Reject suggestion and keep unassigned
    - 'create_profile': Create new profile and assign speaker
    """
    try:
        speaker = get_speaker_by_uuid(db, speaker_uuid)
        file_perm = (
            "owner"
            if current_user.is_admin
            else PermissionService.get_file_permission(
                db, int(speaker.media_file_id), current_user.id
            )
        )
        if not file_perm:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

        profile_id = _resolve_profile_uuid_to_id(profile_uuid, current_user, db)

        return _dispatch_verify_action(
            action, speaker, speaker.id, profile_id, profile_name, current_user, db
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error verifying speaker: {e}")
        db.rollback()
        raise ErrorHandler.internal_error() from e


@router.post("/{speaker_uuid}/confirm-gender", response_model=dict[str, Any])
def confirm_speaker_gender(
    speaker_uuid: str,
    gender: str = Query(..., description="Gender value: 'male' or 'female'"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Confirm or set the predicted gender for a speaker."""
    if gender not in ("male", "female"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Gender must be 'male' or 'female'",
        )

    speaker = get_speaker_by_uuid(db, speaker_uuid)
    if not current_user.is_admin and speaker.user_id != current_user.id:
        perm = PermissionService.get_file_permission(
            db, int(speaker.media_file_id), current_user.id
        )
        if not perm or perm == "viewer":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Requires editor permission"
            )

    speaker.predicted_gender = gender  # type: ignore[assignment]
    speaker.gender_confirmed_by_user = True  # type: ignore[assignment]
    db.commit()

    return {
        "speaker_uuid": str(speaker.uuid),
        "predicted_gender": gender,
        "gender_confirmed_by_user": True,
    }


@router.post("/{speaker_uuid}/merge/{target_speaker_uuid}", response_model=SpeakerSchema)
def merge_speakers(
    speaker_uuid: str,
    target_speaker_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Merge two speakers into one (target absorbs source).

    Postgres is updated synchronously so the response is authoritative. Everything
    downstream — averaging the two voiceprints in OpenSearch, deleting the source
    document, recomputing both profiles' consolidated embeddings, clearing the MinIO
    video cache and refreshing analytics for both files — is handed to
    ``process_speaker_merge_background`` (issue #284 A2.6). Inline, that was roughly
    2 + N OpenSearch round trips (N = members of the target profile), a numpy mean,
    a MinIO listing per affected file and two analytics recomputations, all before
    the merge could answer.
    """
    from app.tasks.speaker_merge_task import process_speaker_merge_background

    # Get both speakers by UUID
    source_speaker = get_speaker_by_uuid(db, speaker_uuid)
    target_speaker = get_speaker_by_uuid(db, target_speaker_uuid)

    # Verify ownership or shared editor permission
    if not current_user.is_admin:
        for spk in (source_speaker, target_speaker):
            if spk.user_id != current_user.id:
                perm = PermissionService.get_file_permission(
                    db, int(spk.media_file_id), current_user.id
                )
                if not perm or perm == "viewer":
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Requires editor permission",
                    )

    # Store profile IDs for embedding updates
    source_profile_id = int(source_speaker.profile_id) if source_speaker.profile_id else None
    target_profile_id = int(target_speaker.profile_id) if target_speaker.profile_id else None
    source_speaker_id = source_speaker.id

    # Update all transcript segments from source to target
    db.query(TranscriptSegment).filter(TranscriptSegment.speaker_id == source_speaker.id).update(
        {"speaker_id": target_speaker.id}
    )

    # Get media file IDs that are affected
    affected_media_files = {int(source_speaker.media_file_id), int(target_speaker.media_file_id)}

    # Capture the source identity before the row disappears
    source_speaker_uuid = str(source_speaker.uuid)

    # Delete the source speaker
    db.delete(source_speaker)
    db.commit()
    db.refresh(target_speaker)

    # Hand the OpenSearch / MinIO / analytics work to the background worker
    process_speaker_merge_background.delay(
        source_speaker_uuid=source_speaker_uuid,
        target_speaker_uuid=str(target_speaker.uuid),
        user_id=current_user.id,
        source_speaker_id=source_speaker_id,
        source_profile_id=source_profile_id,
        target_profile_id=target_profile_id,
        media_file_ids=sorted(affected_media_files),
    )
    logger.info(
        f"Queued background merge processing for {source_speaker_uuid} -> {target_speaker.uuid}"
    )

    # Invalidate caches
    try:
        from app.services.redis_cache_service import redis_cache

        redis_cache.invalidate_speakers(current_user.id)
        redis_cache.invalidate_user_files(current_user.id)
    except Exception as e:
        logger.debug(f"Cache invalidation failed (non-critical): {e}")

    # Add computed status fields
    SpeakerStatusService.add_computed_status(target_speaker)

    return target_speaker


# --- SECTION: SINGLE-PARAM ROUTES (catch-all, must come last) ---


@router.get("/{speaker_uuid}", response_model=SpeakerSchema)
def get_speaker(
    speaker_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Get details of a specific speaker with computed status
    """
    speaker = get_speaker_by_uuid(db, speaker_uuid)

    # Verify file-level access (own or shared via collection)
    file_perm = PermissionService.get_file_permission(
        db, int(speaker.media_file_id), current_user.id
    )
    if not file_perm:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    # Add computed status fields
    SpeakerStatusService.add_computed_status(speaker)

    return speaker


# --- Helper functions for update_speaker ---


def _handle_profile_embedding_updates(
    db: Session,
    speaker_id: int,
    old_profile_id: int | None,
    new_profile_id: int | None,
    was_auto_labeled: bool,
    display_name_changed: bool,
) -> None:
    """Handle profile embedding updates when speaker assignments change."""
    try:
        from app.services.profile_embedding_service import ProfileEmbeddingService

        # Case 1: Speaker was auto-labeled and user corrected it (removed from old profile)
        if (
            was_auto_labeled
            and display_name_changed
            and old_profile_id
            and old_profile_id != new_profile_id
        ):
            success = ProfileEmbeddingService.remove_speaker_from_profile_embedding(
                db, speaker_id, old_profile_id
            )
            if success:
                logger.info(
                    f"Removed speaker {speaker_id} from old profile {old_profile_id} after user correction"
                )
            else:
                logger.warning(
                    f"Failed to remove speaker {speaker_id} from old profile {old_profile_id}"
                )

        # Case 2: Speaker was assigned to a new profile
        if new_profile_id and new_profile_id != old_profile_id:
            success = ProfileEmbeddingService.add_speaker_to_profile_embedding(
                db, speaker_id, new_profile_id
            )
            if success:
                logger.info(f"Added speaker {speaker_id} to new profile {new_profile_id}")
            else:
                logger.warning(
                    f"Failed to add speaker {speaker_id} to new profile {new_profile_id}"
                )

        # Case 3: Speaker was removed from a profile (unassigned)
        elif old_profile_id and new_profile_id is None:
            success = ProfileEmbeddingService.remove_speaker_from_profile_embedding(
                db, speaker_id, old_profile_id
            )
            if success:
                logger.info(
                    f"Removed speaker {speaker_id} from profile {old_profile_id} after unassignment"
                )

        # Case 4: Speaker display name changed but same profile - recalculate to ensure accuracy
        elif display_name_changed and new_profile_id and new_profile_id == old_profile_id:
            success = ProfileEmbeddingService.update_profile_embedding(db, new_profile_id)
            if success:
                logger.info(
                    f"Updated profile {new_profile_id} embedding after speaker {speaker_id} name change"
                )

    except Exception as e:
        logger.exception(f"Error updating profile embeddings for speaker {speaker_id}: {e}")
        # Don't fail the operation if embedding update fails


def _update_opensearch_speaker_name(speaker_uuid: str, display_name: str) -> None:
    """Update speaker display name in OpenSearch."""
    try:
        update_speaker_display_name(speaker_uuid, display_name)
    except Exception as e:
        logger.exception(f"Failed to update speaker display name in OpenSearch: {e}")


def _handle_speaker_labeling_workflow(
    db: Session, speaker_id: int, display_name: str
) -> dict[str, int]:
    """
    Handle auto-creation of profiles and retroactive matching when speaker is labeled.

    Takes ``speaker_id`` rather than a ``Speaker``: the caller's read phase has
    already closed, so the row is re-read here against the session that will write
    it. An instance carried over from a closed session would lazy-load — silently
    opening a second transaction — on the first attribute the callees touch.

    Returns:
        dict with 'auto_applied_count' and 'suggested_count' keys
    """
    # Auto-create profile if needed and assign speaker to it
    from app.api.endpoints.speaker_update import auto_create_or_assign_profile

    speaker = db.query(Speaker).filter(Speaker.id == speaker_id).first()
    if not speaker:
        logger.warning(f"Speaker {speaker_id} disappeared before retroactive matching")
        return {"auto_applied_count": 0, "suggested_count": 0}

    auto_create_or_assign_profile(speaker, display_name, db)

    # Commit profile changes before retroactive matching
    db.commit()
    db.refresh(speaker)

    # Then trigger retroactive matching for all other speakers
    from app.api.endpoints.speaker_update import trigger_retroactive_matching

    return trigger_retroactive_matching(speaker, db)


def _clear_video_cache_for_speaker(media_file_id: int) -> None:
    """Clear video cache since speaker labels have changed (affects subtitles).

    **Takes no caller-owned ``Session``.** This runs from
    ``process_speaker_update_background``, which used to hold ONE transaction across
    its whole body — including the storage client construction and the five object
    deletes underneath. It now opens its own session, and only for the single
    filename SELECT the deletes need; the storage client is
    built before that session exists.

    No residual: the filename is resolved in a short session that CLOSES before any
    object is touched, and the deletes go through ``clear_derived_cache``, which takes
    no session at all. The session-taking ``clear_cache_for_media_file`` it used to call
    has been deleted outright.
    """
    try:
        from app.db.session_utils import session_scope
        from app.models.media import MediaFile
        from app.services.minio_service import MinIOService
        from app.services.video_processing_service import VideoProcessingService

        minio_service = MinIOService()
        video_processing_service = VideoProcessingService(minio_service)
        # Read phase: the filename, and nothing else. The scope closes before the
        # five MinIO deletes below, so no transaction spans them.
        with session_scope() as db:
            row = db.query(MediaFile.filename).filter(MediaFile.id == media_file_id).first()
        if row is None:
            logger.warning(f"Media file {media_file_id} not found for cache clearing")
            return
        video_processing_service.clear_derived_cache(media_file_id, str(row[0]))
    except Exception as e:
        logger.exception(f"Warning: Failed to clear video cache after speaker update: {e}")


def _handle_update_profile_action(
    profile_id: int, new_name: str, current_user: User, db: Session
) -> list[tuple[str, str]] | None:
    """Handle 'update_profile' action - update profile name globally.

    Postgres only. The OpenSearch fan-out this used to do inline — one
    ``update_speaker_display_name`` round trip per linked speaker plus a full
    ``ProfileEmbeddingService.update_profile_embedding`` (one kNN fetch per profile
    member, a numpy mean, and an index write) — is deferred to
    ``process_speaker_update_background`` (issue #284 A2.6). Renaming a profile with
    a dozen linked speakers used to cost a dozen-plus synchronous OpenSearch calls
    before the PUT could answer.

    The ``(file_uuid, old_name)`` pairs are collected **before** the overwrite and
    handed back, because a profile rename spans files and this is the last moment
    the previous names exist anywhere: once committed, nothing in Postgres can say
    what the chunk plane was indexed with (issue #405).

    Returns:
        ``None`` when no such profile exists. Otherwise the renames to replay into
        the chunk index — an empty list when the profile had no linked speakers.
        A non-``None`` return also tells the caller a profile was renamed, so it can
        ask the background task to replay the rename into the speaker index.
    """
    profile = (
        db.query(SpeakerProfile)
        .filter(SpeakerProfile.id == profile_id, SpeakerProfile.user_id == current_user.id)
        .first()
    )

    if not profile:
        return None

    profile.name = new_name  # type: ignore[assignment]
    logger.info(f"Updated profile {profile.id} name to '{new_name}' globally")

    # Update all speakers linked to this profile
    linked_query = db.query(Speaker).filter(Speaker.profile_id == profile_id)
    if not current_user.is_admin:
        linked_query = linked_query.filter(Speaker.user_id == current_user.id)
    linked_speakers = linked_query.all()

    # One grouped lookup, not a lazy `speaker.media_file.uuid` per row.
    file_uuids: dict[int, str] = {}
    media_file_ids = {int(s.media_file_id) for s in linked_speakers if s.media_file_id}
    if media_file_ids:
        file_uuids = {
            int(row[0]): str(row[1])
            for row in db.query(MediaFile.id, MediaFile.uuid).filter(
                MediaFile.id.in_(media_file_ids)
            )
        }

    renames: list[tuple[str, str]] = []
    for linked_speaker in linked_speakers:
        old_chunk_name = str(linked_speaker.display_name or linked_speaker.name or "")
        file_uuid = file_uuids.get(int(linked_speaker.media_file_id or 0))
        if file_uuid and old_chunk_name:
            renames.append((file_uuid, old_chunk_name))
        linked_speaker.display_name = new_name  # type: ignore[assignment]

    logger.info(f"Updated {len(linked_speakers)} speakers with new profile name '{new_name}'")
    return renames


def _load_profile_speaker_names(db: Session, profile_id: int) -> list[tuple[str, str]]:
    """Read ``(speaker_uuid, display_name)`` for every speaker linked to a profile.

    Plain data only — no ORM instances — so the caller can close its session before
    pushing the names to the search index. Speakers with no display name are skipped
    here rather than in the push loop, so the returned length is the number of
    documents that will actually be written.
    """
    rows = (
        db.query(Speaker.uuid, Speaker.display_name).filter(Speaker.profile_id == profile_id).all()
    )
    return [(str(uuid_), str(name)) for uuid_, name in rows if name]


def _push_speaker_display_names(rows: list[tuple[str, str]]) -> None:
    """Write ``(speaker_uuid, display_name)`` pairs to the search index.

    **Takes no ``Session``.** One round trip per linked speaker — a dozen for a
    well-used profile — so this must never run with a transaction open. Callers do
    the read in a short session, close it, then call this.
    """
    for speaker_uuid, display_name in rows:
        _update_opensearch_speaker_name(speaker_uuid, display_name)
    logger.info(f"Synced renamed profile to OpenSearch for {len(rows)} speakers")


def _sync_profile_rename_to_opensearch(db: Session, profile_id: int) -> None:
    """Replay a profile rename onto every linked speaker's OpenSearch document.

    Runs in ``process_speaker_update_background`` (issue #284 A2.6), never in the
    request path. Names are re-read from Postgres rather than passed in, so a rename
    that landed while the task was queued still wins.

    Read, **release the transaction**, then push: the fan-out is one OpenSearch write
    per linked speaker, and holding the caller's transaction across them queues every
    ALTER TABLE for as long as the cluster takes. ``db.commit()`` is safe here because
    the only statement this function issues is the SELECT above. The task calls the two
    halves directly so that no session exists at all during the push.

    The profile's consolidated embedding is deliberately NOT recomputed here:
    ``_handle_profile_embedding_updates`` case 4 (display name changed, same profile)
    already calls ``ProfileEmbeddingService.update_profile_embedding`` earlier in the
    same task, and a profile rename always satisfies that case.
    """
    rows = _load_profile_speaker_names(db, profile_id)
    db.commit()
    _push_speaker_display_names(rows)


def _handle_create_new_profile_action(
    speaker: Speaker, new_name: str, current_user: User, db: Session
) -> None:
    """Handle 'create_new_profile' action - create new profile and assign speaker."""
    new_profile = SpeakerProfile(
        user_id=current_user.id,
        name=new_name,
        description=f"Profile for {new_name}",
        uuid=str(uuid.uuid4()),
        # Tenant stamp (issue #262e): the profile groups THIS speaker's voice,
        # so it inherits the speaker's org (stamped by the pipeline from the
        # file); None = personal, always None in the community edition.
        organization_id=int(speaker.organization_id) if speaker.organization_id else None,
    )
    db.add(new_profile)
    db.flush()  # Get the ID

    speaker.profile_id = new_profile.id
    logger.info(
        f"Created new profile {new_profile.id} '{new_name}' and assigned speaker {speaker.id}"
    )


def _handle_profile_action(
    profile_action: str | None,
    speaker_update: SpeakerUpdate,
    speaker: Speaker,
    old_profile_id: int | None,
    current_user: User,
    db: Session,
) -> tuple[int | None, list[tuple[str, str]]]:
    """Handle profile actions (update_profile or create_new_profile).

    Returns:
        ``(renamed_profile_id, chunk_renames)`` — the id of a profile that was
        renamed in place, so the caller can hand the OpenSearch replay to the
        background task (``None`` otherwise), and the ``(file_uuid, old_name)``
        pairs whose chunk documents still carry a pre-rename speaker name.
    """
    if not (profile_action and speaker_update.display_name):
        return None, []

    new_name = speaker_update.display_name.strip()

    if profile_action == "update_profile" and old_profile_id:
        renames = _handle_update_profile_action(old_profile_id, new_name, current_user, db)
        if renames is not None:
            return old_profile_id, renames
    elif profile_action == "create_new_profile":
        _handle_create_new_profile_action(speaker, new_name, current_user, db)
    return None, []


def _get_profile_uuid(speaker: Speaker, db: Session) -> str | None:
    """Get profile UUID from speaker, fetching from DB if necessary."""
    if not speaker.profile_id:
        return None

    if speaker.profile:
        return str(speaker.profile.uuid)

    profile = db.query(SpeakerProfile).filter(SpeakerProfile.id == speaker.profile_id).first()
    return str(profile.uuid) if profile else None


def _load_speaker_profile_sync_payload(
    db: Session, speaker: Speaker, old_profile_id: int | None, display_name_changed: bool
) -> dict[str, Any] | None:
    """Resolve the profile fields a speaker's search document needs, as plain data.

    Returns ``None`` when nothing changed and no write is due — the same early
    return the pre-split ``_update_opensearch_profile_info`` made. Everything the
    write needs (including the profile UUID, which may require a second SELECT) is
    materialised here so the caller can close its session before writing.
    """
    new_profile_id = int(speaker.profile_id) if speaker.profile_id else None

    if old_profile_id == new_profile_id and not display_name_changed:
        return None

    return {
        "speaker_uuid": str(speaker.uuid),
        "profile_id": new_profile_id,
        "profile_uuid": _get_profile_uuid(speaker, db),
        "verified": bool(speaker.verified),
    }


def _push_speaker_profile_info(payload: dict[str, Any] | None) -> None:
    """Write a speaker's profile fields to the search index.

    **Takes no ``Session``.** ``None`` means the read phase decided there was
    nothing to sync.
    """
    if payload is None:
        return

    from app.services.opensearch_service import update_speaker_profile

    update_speaker_profile(**payload)


def _get_media_file_uuid(speaker: Speaker, db: Session) -> str | None:
    """Get media file UUID from speaker."""
    if speaker.media_file:
        return str(speaker.media_file.uuid)

    if speaker.media_file_id:
        row = db.query(MediaFile.uuid).filter(MediaFile.id == speaker.media_file_id).first()
        if row:
            return str(row[0])

    return None


def _send_websocket_notification(speaker: Speaker, current_user: User, db: Session) -> None:
    """Send WebSocket notification for speaker update (best-effort)."""
    try:
        import asyncio

        from app.api.websockets import publish_notification

        notification_data = {
            "speaker_id": str(speaker.uuid),
            "media_file_id": _get_media_file_uuid(speaker, db),
            "display_name": speaker.display_name,
            "verified": speaker.verified,
            "profile_id": _get_profile_uuid(speaker, db),
        }

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                publish_notification(
                    user_id=current_user.id,
                    notification_type="speaker_updated",
                    data=notification_data,
                )
            )
        except RuntimeError:
            logger.debug(
                f"Skipped WebSocket notification for speaker {speaker.uuid} (no event loop)"
            )
    except Exception as e:
        logger.debug(f"WebSocket notification skipped for speaker update: {e}")


def _set_no_cache_headers(response: Response) -> None:
    """Set cache-busting headers on response."""
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"


def _apply_verification_on_display_name(speaker: Speaker, speaker_update: SpeakerUpdate) -> None:
    """Mark speaker as verified when display name is set."""
    if speaker_update.display_name is not None and speaker_update.display_name.strip():
        speaker.verified = True  # type: ignore[assignment]
        speaker.suggested_name = None  # type: ignore[assignment]
        speaker.suggestion_source = None  # type: ignore[assignment]
        speaker.confidence = None  # type: ignore[assignment]


def _propagate_speaker_rename_to_chunks(
    *,
    file_uuid: str | None,
    previous_chunk_name: str,
    new_display_name: str,
    profile_renames: list[tuple[str, str]],
) -> None:
    """Queue the chunk-plane rewrite for everything this request renamed.

    Two sources feed it: the speaker the request targeted, and — when the request
    carried ``profile_action="update_profile"`` — every other speaker the profile
    rename swept along, which may sit in other files entirely. Both go through
    ``dispatch_speaker_rename``, which coalesces per file.

    Best-effort by design: a rename that reached Postgres must not 500 because the
    broker was unreachable. The chunk plane stays stale until the next reindex,
    which is exactly the pre-#405 behaviour.
    """
    if not new_display_name:
        return
    try:
        from app.tasks.rename_propagation_task import dispatch_speaker_rename

        renames: list[tuple[str | None, str | None]] = list(profile_renames)
        if file_uuid and previous_chunk_name:
            renames.append((file_uuid, previous_chunk_name))
        dispatch_speaker_rename(renames, new_display_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not queue chunk-plane speaker rename propagation: {exc}")


@router.put("/{speaker_uuid}", response_model=SpeakerSchema)
def update_speaker(
    speaker_uuid: str,
    speaker_update: SpeakerUpdate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Update a speaker's information including display name and verification status.

    This endpoint returns immediately after saving to PostgreSQL. Heavy processing
    (profile embeddings, OpenSearch updates, cross-media matching) happens in a
    background Celery task. A WebSocket notification is sent when processing completes.
    """
    from app.tasks.speaker_tasks import process_speaker_update_background

    # Find and validate speaker
    speaker = get_speaker_by_uuid(db, speaker_uuid)
    if not current_user.is_admin and speaker.user_id != current_user.id:
        perm = PermissionService.get_file_permission(
            db, int(speaker.media_file_id), current_user.id
        )
        if not perm or perm == "viewer":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Requires editor permission"
            )

    speaker_id = speaker.id
    old_profile_id = int(speaker.profile_id) if speaker.profile_id else None
    was_auto_labeled = speaker.suggested_name is not None and not speaker.verified
    media_file_id = int(speaker.media_file_id)
    # The exact string the chunk plane was indexed with — display_name when the
    # speaker had one, else the diarizer's raw label. Captured here because the
    # overwrite below is the last moment it exists (issue #405).
    previous_chunk_name = str(speaker.display_name or speaker.name or "")
    speaker_file_uuid = _get_media_file_uuid(speaker, db)

    # Update speaker fields
    update_data = speaker_update.model_dump(exclude_unset=True)
    profile_action = update_data.pop("profile_action", None)

    for field, value in update_data.items():
        if field not in SPEAKER_UPDATABLE_FIELDS:
            logger.warning(f"Ignoring non-updatable field in speaker update: {field}")
            continue
        setattr(speaker, field, value)

    # Handle profile actions. Postgres writes only — they shape the immediate
    # response; the OpenSearch replay rides along with the background task below.
    renamed_profile_id, profile_chunk_renames = _handle_profile_action(
        profile_action, speaker_update, speaker, old_profile_id, current_user, db
    )

    # Handle verification when display name is set
    _apply_verification_on_display_name(speaker, speaker_update)

    # Commit to PostgreSQL immediately
    db.commit()
    db.refresh(speaker)

    # Determine what changed
    display_name_changed = speaker_update.display_name is not None
    new_profile_id = int(speaker.profile_id) if speaker.profile_id else None
    display_name = str(speaker.display_name) if speaker.display_name else ""

    # Queue background processing for heavy operations
    # This handles: profile embeddings, OpenSearch updates, retroactive matching, cache clearing
    if display_name_changed or old_profile_id != new_profile_id:
        process_speaker_update_background.delay(
            speaker_uuid=str(speaker.uuid),
            user_id=current_user.id,
            display_name=display_name,
            speaker_id=speaker_id,
            old_profile_id=old_profile_id,
            new_profile_id=new_profile_id,
            was_auto_labeled=was_auto_labeled,
            display_name_changed=display_name_changed,
            media_file_id=media_file_id,
            renamed_profile_id=renamed_profile_id,
        )
        logger.info(f"Queued background processing for speaker {speaker_uuid}")

    # Rewrite the speaker name the transcript chunks were indexed with. Without
    # this, chat's speaker scope (an exact `terms` match on the CURRENT name) and
    # the search facet dropdown keep working off the pre-rename snapshot — see
    # app/tasks/rename_propagation_task.py (issue #405).
    _propagate_speaker_rename_to_chunks(
        file_uuid=speaker_file_uuid,
        previous_chunk_name=previous_chunk_name,
        new_display_name=display_name,
        profile_renames=profile_chunk_renames,
    )

    # Invalidate caches so speaker lists and file data refresh
    try:
        from app.services.redis_cache_service import redis_cache

        redis_cache.invalidate_speakers(current_user.id)
        redis_cache.invalidate_user_files(current_user.id)
    except Exception as e:
        logger.debug(f"Cache invalidation failed (non-critical): {e}")

    # Add computed status for immediate response
    SpeakerStatusService.add_computed_status(speaker)
    _set_no_cache_headers(response)

    return speaker


@router.delete("/{speaker_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_speaker(
    speaker_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> None:
    """Delete one speaker row from a media file's diarization result.

    Consumed by the file-detail speaker panel. Authorization is broader than
    ownership: the owner or an admin always passes, and a non-owner needs
    ``editor`` on the underlying media file via ``PermissionService`` — a ``viewer``
    on a shared file gets 403, since deleting a speaker rewrites the transcript's
    attribution for everyone the file is shared with.

    This removes the per-file speaker, not the cross-media ``SpeakerProfile`` it may
    be assigned to; the profile and its other occurrences survive. Two follow-up
    steps are deliberately non-fatal and only logged if they fail — pruning the
    voiceprint from every OpenSearch speaker index, and invalidating the speaker and
    file caches — so a degraded search or cache backend cannot block the delete that
    Postgres has already committed. A stale voiceprint left behind can still surface
    in similarity results until it is cleaned up.
    """
    # Find the speaker by UUID
    speaker = get_speaker_by_uuid(db, speaker_uuid)

    # Verify ownership or shared editor permission
    if not current_user.is_admin and speaker.user_id != current_user.id:
        perm = PermissionService.get_file_permission(
            db, int(speaker.media_file_id), current_user.id
        )
        if not perm or perm == "viewer":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Requires editor permission"
            )

    # Capture UUID before DB delete
    uuid_to_clean = str(speaker.uuid)

    # Delete the speaker
    db.delete(speaker)
    db.commit()

    # Remove from all OpenSearch speaker indices (non-fatal)
    try:
        from app.services.opensearch_service import remove_speaker_embedding

        remove_speaker_embedding(uuid_to_clean)
    except Exception as e:
        logger.warning(f"Failed to remove speaker {uuid_to_clean} from OpenSearch: {e}")

    # Invalidate caches
    try:
        from app.services.redis_cache_service import redis_cache

        redis_cache.invalidate_speakers(current_user.id)
        redis_cache.invalidate_user_files(current_user.id)
    except Exception as e:
        logger.debug(f"Cache invalidation failed (non-critical): {e}")


# --- Helper functions for verify_speaker_identification ---


def _accept_speaker_profile_match(
    speaker: Speaker, speaker_id: int, profile_id: int, current_user: User, db: Session
) -> dict[str, Any]:
    """Handle acceptance of a speaker profile match."""
    # Verify profile exists and is accessible (own or shared)
    accessible_ids = PermissionService.get_accessible_profile_ids(db, current_user.id)
    profile = (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.id == profile_id,
            SpeakerProfile.id.in_(accessible_ids),
        )
        .first()
    )

    if not profile:
        raise HTTPException(status_code=404, detail="Speaker profile not found")

    # Assign speaker to profile - cast to int for assignment
    speaker.profile_id = profile_id  # type: ignore[assignment]
    speaker.verified = True  # type: ignore[assignment]
    db.commit()

    # Update the profile's consolidated embedding
    try:
        from app.services.profile_embedding_service import ProfileEmbeddingService

        success = ProfileEmbeddingService.add_speaker_to_profile_embedding(
            db, speaker_id, profile_id
        )
        if success:
            logger.info(f"Updated profile {profile_id} embedding after adding speaker {speaker_id}")
        else:
            logger.warning(
                f"Failed to update profile {profile_id} embedding for speaker {speaker_id}"
            )
    except Exception as e:
        logger.exception(f"Error updating profile embedding: {e}")

    return {
        "status": "accepted",
        "speaker_id": str(speaker.uuid),
        "profile_id": str(profile.uuid),
        "profile_name": profile.name,
        "message": f"Speaker assigned to profile '{profile.name}'",
    }


def _reject_speaker_suggestion(speaker: Speaker, speaker_id: int, db: Session) -> dict[str, Any]:
    """Handle rejection of a speaker identification suggestion."""
    old_profile_id = int(speaker.profile_id) if speaker.profile_id else None

    # Mark as verified but don't assign to profile - use setattr for proper type handling
    speaker.profile_id = None  # type: ignore[assignment]
    speaker.verified = True  # type: ignore[assignment]
    speaker.confidence = None  # type: ignore[assignment]
    db.commit()

    # Update the old profile's embedding if speaker was previously assigned
    if old_profile_id:
        try:
            from app.services.profile_embedding_service import ProfileEmbeddingService

            success = ProfileEmbeddingService.remove_speaker_from_profile_embedding(
                db, speaker_id, old_profile_id
            )
            if success:
                logger.info(
                    f"Updated profile {old_profile_id} embedding after removing speaker {speaker_id}"
                )
            else:
                logger.warning(
                    f"Failed to update profile {old_profile_id} embedding after removing speaker {speaker_id}"
                )
        except Exception as e:
            logger.exception(f"Error updating profile embedding after rejection: {e}")

    return {
        "status": "rejected",
        "speaker_id": str(speaker.uuid),
        "message": "Speaker identification suggestion rejected",
    }


def _create_new_speaker_profile(
    speaker: Speaker, speaker_id: int, profile_name: str, current_user: User, db: Session
) -> dict[str, Any]:
    """Handle creation of a new speaker profile."""
    # Check if profile with same name exists
    existing_profile = (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.user_id == current_user.id,
            SpeakerProfile.name == profile_name,
        )
        .first()
    )

    if existing_profile:
        raise HTTPException(status_code=400, detail="Profile with this name already exists")

    # Create new profile (org-stamped from the speaker's tenant — issue #262e)
    new_profile = SpeakerProfile(
        user_id=current_user.id,
        name=profile_name,
        uuid=str(uuid.uuid4()),
        organization_id=int(speaker.organization_id) if speaker.organization_id else None,
    )

    db.add(new_profile)
    db.flush()

    # Assign speaker to new profile - use setattr for proper type handling
    speaker.profile_id = new_profile.id  # type: ignore[assignment]
    speaker.verified = True  # type: ignore[assignment]
    db.commit()

    # Update the new profile's consolidated embedding
    try:
        from app.services.profile_embedding_service import ProfileEmbeddingService

        success = ProfileEmbeddingService.add_speaker_to_profile_embedding(
            db, speaker_id, new_profile.id
        )
        if success:
            logger.info(
                f"Updated new profile {new_profile.id} embedding after adding speaker {speaker_id}"
            )
        else:
            logger.warning(
                f"Failed to update new profile {new_profile.id} embedding for speaker {speaker_id}"
            )
    except Exception as e:
        logger.exception(f"Error updating new profile embedding: {e}")

    return {
        "status": "created_and_assigned",
        "speaker_id": str(speaker.uuid),
        "profile_id": str(new_profile.uuid),
        "profile_name": profile_name,
        "message": f"Created new profile '{profile_name}' and assigned speaker",
    }


def _resolve_profile_uuid_to_id(
    profile_uuid: str | None, current_user: User, db: Session
) -> int | None:
    """Convert profile UUID to internal ID if provided, validating access."""
    if not profile_uuid:
        return None

    from app.utils.uuid_helpers import get_speaker_profile_by_uuid

    profile = get_speaker_profile_by_uuid(db, profile_uuid)
    accessible_ids = PermissionService.get_accessible_profile_ids(db, current_user.id)
    if profile.id not in accessible_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    return profile.id


def _dispatch_verify_action(
    action: str,
    speaker: Speaker,
    speaker_id: int,
    profile_id: int | None,
    profile_name: str | None,
    current_user: User,
    db: Session,
) -> dict[str, Any]:
    """Dispatch to the appropriate verification action handler."""
    if action == "accept":
        if not profile_id:
            raise HTTPException(status_code=400, detail="profile_id required for accept action")
        return _accept_speaker_profile_match(speaker, speaker_id, profile_id, current_user, db)

    if action == "reject":
        return _reject_speaker_suggestion(speaker, speaker_id, db)

    if action == "create_profile":
        if not profile_name:
            raise HTTPException(
                status_code=400,
                detail="profile_name required for create_profile action",
            )
        return _create_new_speaker_profile(speaker, speaker_id, profile_name, current_user, db)

    raise HTTPException(
        status_code=400,
        detail="Invalid action. Must be 'accept', 'reject', or 'create_profile'",
    )


# --- Helper functions for merge_speakers ---


def _merge_speaker_embeddings(source_speaker_uuid: str, target: dict[str, Any]) -> None:
    """Merge and average speaker embeddings in OpenSearch.

    Takes the source speaker's UUID rather than the ORM row: this now runs in
    ``process_speaker_merge_background`` (issue #284 A2.6), by which time the source
    Speaker row has already been deleted from Postgres. Only its OpenSearch document
    is still needed, and that is removed later in the same task by
    ``_update_opensearch_speaker_merge`` — so the read here still precedes the delete.

    ``target`` is the surviving speaker as **plain data** (see
    ``speaker_merge_task._load_merge_plan``), not a ``Speaker``. Two OpenSearch reads
    and one write run here; a live ORM instance would lazy-load mid-flight and open a
    transaction underneath them, which is the leak this split exists to remove.
    """
    try:
        import numpy as np

        from app.services.opensearch_service import add_speaker_embedding
        from app.services.opensearch_service import get_speaker_embedding

        # Get embeddings for both speakers
        source_embedding = get_speaker_embedding(str(source_speaker_uuid))
        target_embedding = get_speaker_embedding(target["uuid"])

        if source_embedding and target_embedding:
            # Average the embeddings
            embeddings_array = np.array([source_embedding, target_embedding])
            averaged_embedding = np.mean(embeddings_array, axis=0).tolist()

            # Store the averaged embedding in OpenSearch
            add_speaker_embedding(
                speaker_uuid=target["uuid"],
                speaker_id=target["id"],
                user_id=target["user_id"],
                name=target["name"],
                embedding=averaged_embedding,
                profile_id=target["profile_id"],
                media_file_id=target["media_file_id"],
                display_name=target["display_name"],
                segment_count=2,  # Merged from 2 speakers
            )
            logger.info(f"Updated target speaker {target['id']} with averaged embedding")
        else:
            logger.warning("Could not retrieve embeddings for speaker merge")
    except Exception as e:
        logger.exception(f"Error averaging speaker embeddings during merge: {e}")


def _clear_speaker_video_cache(affected_media_files: set[int]) -> None:
    """Clear video cache for affected media files after speaker merge.

    **Takes no caller-owned ``Session``.** One short session per file (via
    ``_clear_video_cache_for_speaker``) rather than one transaction spanning the
    merge task, which previously stayed open across every object delete for both
    sides of the merge.
    """
    for media_file_id in sorted(affected_media_files):
        _clear_video_cache_for_speaker(media_file_id)


def _update_opensearch_speaker_merge(source_speaker_uuid: str, target_speaker_uuid: str) -> None:
    """Update OpenSearch index after speaker merge."""
    try:
        from app.services.opensearch_service import merge_speaker_embeddings

        merge_speaker_embeddings(source_speaker_uuid, target_speaker_uuid, [])
        logger.info(
            f"Merged speaker embeddings in OpenSearch: {source_speaker_uuid} -> {target_speaker_uuid}"
        )
    except Exception as e:
        logger.exception(f"Error merging speaker embeddings in OpenSearch: {e}")


def _refresh_analytics_after_merge(db: Session, affected_media_files: set[int]) -> None:
    """Recalculate analytics for affected media files after speaker merge."""
    try:
        from app.services.analytics_service import AnalyticsService

        for media_file_id in affected_media_files:
            if AnalyticsService.refresh_analytics(db, media_file_id):
                logger.info(
                    f"Refreshed analytics for media file {media_file_id} after speaker merge"
                )
            else:
                logger.warning(f"Failed to refresh analytics for media file {media_file_id}")
    except Exception as e:
        logger.exception(f"Error refreshing analytics after speaker merge: {e}")


def _update_profile_embeddings_after_merge(
    db: Session,
    source_profile_id: int | None,
    target_profile_id: int | None,
    source_speaker_id: int,
) -> None:
    """Update profile embeddings affected by speaker merge."""
    try:
        from app.services.profile_embedding_service import ProfileEmbeddingService

        # Update source profile embedding if it exists
        if (
            source_profile_id
            and source_profile_id != target_profile_id
            and ProfileEmbeddingService.remove_speaker_from_profile_embedding(
                db, source_speaker_id, source_profile_id
            )
        ):
            logger.info(f"Updated source profile {source_profile_id} embedding after speaker merge")

        # Update target profile embedding if it exists
        if target_profile_id and ProfileEmbeddingService.update_profile_embedding(
            db, target_profile_id
        ):
            logger.info(f"Updated target profile {target_profile_id} embedding after speaker merge")

    except Exception as e:
        logger.exception(f"Error updating profile embeddings after speaker merge: {e}")


# --- Helper functions for get_speaker_cross_media_occurrences ---


def _build_occurrence_dict(
    media_file: MediaFile,
    speaker_obj: Speaker,
    same_speaker: bool,
) -> dict[str, Any]:
    """Build occurrence dictionary for a speaker/media file pair."""
    return {
        "media_file_id": str(media_file.uuid),
        "filename": media_file.filename,
        "title": media_file.title or media_file.filename,
        "media_file_title": media_file.title or media_file.filename,
        "upload_time": media_file.upload_time.isoformat() if media_file.upload_time else "",
        "speaker_label": speaker_obj.name,
        "confidence": speaker_obj.confidence,
        "verified": speaker_obj.verified,
        "same_speaker": same_speaker,
    }


def _get_profile_based_occurrences(
    speaker: Speaker, current_user: User, db: Session
) -> list[dict[str, Any]]:
    """Get cross-media occurrences for a speaker with a profile."""
    query = (
        db.query(Speaker)
        .join(MediaFile)
        .filter(
            Speaker.profile_id == speaker.profile_id,
        )
    )
    if not current_user.is_admin:
        query = query.filter(Speaker.user_id == current_user.id)
    profile_speakers = query.all()

    result: list[dict[str, Any]] = []
    for profile_speaker in profile_speakers:
        if profile_speaker.media_file:
            result.append(
                _build_occurrence_dict(
                    profile_speaker.media_file,
                    profile_speaker,
                    same_speaker=(profile_speaker.id == speaker.id),
                )
            )
    return result


def _get_display_name_based_occurrences(
    speaker: Speaker, current_user: User, db: Session
) -> list[dict[str, Any]]:
    """Get cross-media occurrences for a speaker without a profile, by display name."""
    result: list[dict[str, Any]] = []

    # Add this speaker instance first
    if speaker.media_file:
        result.append(_build_occurrence_dict(speaker.media_file, speaker, same_speaker=True))

    # Skip if speaker doesn't have a valid display name
    if not speaker.display_name or speaker.display_name.startswith("SPEAKER_"):
        return result

    # Find other speakers with the same display name
    similar_q = (
        db.query(Speaker)
        .join(MediaFile)
        .filter(
            Speaker.display_name == speaker.display_name,
            Speaker.id != speaker.id,
        )
    )
    if not current_user.is_admin:
        similar_q = similar_q.filter(Speaker.user_id == current_user.id)
    similar_speakers = similar_q.all()

    for similar_speaker in similar_speakers:
        if similar_speaker.media_file:
            result.append(
                _build_occurrence_dict(
                    similar_speaker.media_file, similar_speaker, same_speaker=False
                )
            )

    return result
