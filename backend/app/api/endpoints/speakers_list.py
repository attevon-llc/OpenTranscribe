"""Speaker list endpoint (``GET /speakers``) and its response-shaping helpers.

The handler is registered on the package router in ``speakers.py``: FastAPI refuses to
include a sub-router carrying an empty path with an empty prefix."""

import logging
from typing import Any

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.speakers_list_filters import SPEAKER_FILTER_DEFAULT_LIMIT
from app.api.endpoints.speakers_list_filters import SPEAKER_FILTER_MAX_LIMIT
from app.api.endpoints.speakers_list_filters import _filter_speakers_query
from app.api.endpoints.speakers_list_filters import _get_first_segment_timestamps_for_speakers
from app.api.endpoints.speakers_list_filters import _get_segment_counts_for_speakers
from app.api.endpoints.speakers_list_filters import _get_unique_speakers_for_filter
from app.api.endpoints.speakers_list_filters import _resolve_file_uuid_to_id
from app.api.endpoints.speakers_list_filters import _resolve_profile_filter_id
from app.api.endpoints.speakers_list_filters import _sort_speakers
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.user import User
from app.schemas.media import split_attribute_confidence
from app.services.permission_service import file_ids_in_scope
from app.services.privacy.redact_view import PrivacyView
from app.services.privacy.redact_view import redact_speaker_rows
from app.services.request_context import RequestContext
from app.services.speaker_provenance_view import attach_speaker_provenance
from app.services.speaker_provenance_view import provenance_for_dict
from app.services.speaker_status_service import SpeakerStatusService

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

# Speaker suggestion constants
SPEAKER_SUGGESTION_MIN_CONFIDENCE = 0.5
SPEAKER_SUGGESTION_MAX_COUNT = 5


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

    # Unknown provenance stays unknown: v439 stamped every legacy row that could prove a
    # voiceprint source, and claiming one here would badge a possible LLM guess as a voice
    # match that the canonical label itself refuses to trust.
    return None


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

    # Pre-compute placeholder text.
    #
    # ⚠️ A suggestion is ALWAYS labelled as one, however confident. The
    # high-confidence branch used to emit the bare ``suggested_name`` — and the
    # editor's `translatePlaceholder` passes an unrecognised placeholder through
    # verbatim — so a >=0.75 voice match sat in the name field looking exactly
    # like a value a human had confirmed, with only a border colour to say
    # otherwise. Speaker-ID suggestions are never auto-applied; presenting one
    # as an established name is the same claim by other means. Confidence is
    # still reported, through ``is_high_confidence``/``is_medium_confidence``.
    # (It also swallowed the metadata provenance: a confident `metadata_hint`
    # took the bare branch and never said where the name came from.)
    if suggested_name and suggestion_source == "metadata_hint":
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
    # The same split the Speaker schema applies, so this list and the detail surfaces agree.
    attribute_fields = split_attribute_confidence(speaker.attribute_confidence)

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
        "attribute_confidence": attribute_fields["attribute_confidence"],
        "attributes_predicted_at": speaker.attributes_predicted_at.isoformat()
        if speaker.attributes_predicted_at
        else None,
        # Cross-reference alignment data from metadata hints
        "gender_alignment": attribute_fields["gender_alignment"],
        "gender_alignment_hint": attribute_fields["gender_alignment_hint"],
        "metadata_hints": attribute_fields["metadata_hints"],
        # Pre-computed display flags
        **display_flags,
        # Read-only provenance, attached after the redaction
        **provenance_for_dict(speaker),
    }

    # Add profile information if speaker is assigned to a profile
    if speaker.profile_id and speaker.profile:
        speaker_dict["profile"] = {
            "uuid": str(speaker.profile.uuid) if speaker.profile.uuid else None,
            "name": speaker.profile.name,
            "description": speaker.profile.description,
        }

    return speaker_dict


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


def list_speakers(
    verified_only: bool = False,
    file_uuid: str | None = None,
    for_filter: bool = False,
    q: str | None = Query(None, description="for_filter only: substring search on display name"),
    limit: int = Query(
        SPEAKER_FILTER_DEFAULT_LIMIT,
        ge=1,
        le=SPEAKER_FILTER_MAX_LIMIT,
        description="for_filter only: page size",
    ),
    profile_id: str | None = Query(None, description="for_filter only: profile UUID"),
    is_profile: bool | None = Query(
        None, description="for_filter only: restrict to profile-linked (true) or unlinked (false)"
    ),
    include_unnamed: bool = Query(
        False,
        description=(
            "for_filter only: also offer speakers nobody has named, under their diarization "
            "label and flagged is_unnamed. They are per-file labels, not people — never "
            "render a flagged entry as a person."
        ),
    ),
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
        q, limit, profile_id, is_profile, include_unnamed: ``for_filter=True`` only —
            server-side type-to-search (substring, case-insensitive), page size, profile
            scoping, and whether to also offer unlabeled diarization labels (#743).
            Ignored otherwise.

    Returns:
        List[dict]: Speaker objects with embedded suggestion data and profile information.
    """
    try:
        from sqlalchemy.orm import joinedload

        # Fast path: filter mode only needs aggregated display names
        # Skip loading all speaker objects, profiles, and media files
        if for_filter:
            resolved_profile_id = _resolve_profile_filter_id(
                db, current_user, profile_id, organization_id=ctx.org_id, bypass=ctx.bypass
            )
            if profile_id and resolved_profile_id is None:
                # A profile UUID was given and did not resolve (not found, or
                # not this caller's) — an empty result, not a 404: this is a
                # filter parameter, not a resource fetch.
                return []
            return _get_unique_speakers_for_filter(
                db,
                current_user,
                organization_id=ctx.org_id,
                q=q,
                limit=limit,
                profile_id=resolved_profile_id,
                is_profile=is_profile,
                include_unnamed=include_unnamed,
                bypass=ctx.bypass,
            )

        # Convert file_uuid to file_id if provided (tenant-gated via ctx.org_id)
        file_id = _resolve_file_uuid_to_id(
            file_uuid, current_user, db, organization_id=ctx.org_id, bypass=ctx.bypass
        )

        query = db.query(Speaker).options(
            joinedload(Speaker.profile), joinedload(Speaker.media_file)
        )
        # A caller who sees all in scope gets every speaker of the tenant; when file_id is
        # provided, permission was
        # already checked by _resolve_file_uuid_to_id (which applies the
        # takedown gate via get_file_by_uuid_with_permission); otherwise scope
        # to owner AND drop speakers whose file is quarantined (A2's class —
        # the general listing has no per-file permission check to catch it,
        # so a caller's OWN quarantined file's speakers would otherwise leak
        # here even though the file itself 404s for them).
        if ctx.bypass.sees_all_in_scope:
            query = query.filter(Speaker.media_file_id.in_(file_ids_in_scope(ctx.org_id)))
        elif file_id is not None:
            pass  # Viewing specific file — permission already checked
        else:
            from sqlalchemy import select

            query = query.filter(
                Speaker.user_id == current_user.id,
                Speaker.media_file_id.in_(file_ids_in_scope(ctx.org_id)),
            )
            quarantined_ids = select(MediaFile.id).where(MediaFile.is_quarantined.is_(True))
            query = query.filter(~Speaker.media_file_id.in_(quarantined_ids))
        query = _filter_speakers_query(query, verified_only, file_id)
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
        # Privacy policy (#1280): a file whose owner or organization has voiceprints off gets
        # no kNN suggestions, its stored unverified voice suggestions are not shown, and an
        # inferred gender is withheld wherever the gender switch is off. Existing data is
        # hidden, never deleted (re-enabling the layer brings it back).
        privacy = PrivacyView(db)
        voiceprints_off = privacy.voiceprints_off_file_ids(int(s.media_file_id) for s in speakers)
        redact_speaker_rows(privacy, speakers)
        attach_speaker_provenance(db, speakers, ctx, view=privacy)
        batch_suggestions = SmartSpeakerSuggestionService.consolidate_suggestions_batch(
            speakers=speakers,
            user_id=current_user.id,
            file_org_map=file_org_map,
            voiceprints_off_file_ids=voiceprints_off,
            confidence_threshold=SPEAKER_SUGGESTION_MIN_CONFIDENCE,
            max_suggestions=SPEAKER_SUGGESTION_MAX_COUNT,
        )
        # A speaker is never offered an identity the user unassigned it from (#1233).
        batch_suggestions = SmartSpeakerSuggestionService.without_refused(db, batch_suggestions)

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

    except (HTTPException, DomainError):
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
