"""Speaker-to-profile assignment and profile suggestions (split from speaker_profiles)."""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.error_handlers import ErrorHandler
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.models.user import User
from app.services.opensearch_service import update_speaker_collections
from app.services.permission_service import PermissionService
from app.services.privacy.redact_view import PrivacyView
from app.services.request_context import RequestContext
from app.services.speaker_matching.thresholds import ConfidenceLevel
from app.services.speaker_matching_service import SpeakerMatchingService
from app.services.uuid_helpers import require_profile_in_scope
from app.services.uuid_helpers import require_speaker_access
from app.tasks.speaker_update_task import process_speaker_update_background

logger = logging.getLogger(__name__)

router = APIRouter()


def _queue_retroactive_matching_after_assignment(
    *,
    speaker_uuid: str,
    user_id: int,
    speaker_id: int,
    old_profile_id: int | None,
    new_profile_id: int,
    media_file_id: int,
) -> None:
    """Re-score the library once a speaker has been attached to a profile.

    A ``SpeakerProfile`` has no voiceprint until a speaker is attached, so this
    assignment is the moment the profile first becomes matchable. This endpoint
    queued nothing at all, so every other recording of that voice — including the
    other clusters of it in the same file — stayed unmatched with an empty Inbox.

    ``display_name_changed=False`` is load-bearing: it routes the task to the
    profile-change branch (``_should_rescore_after_profile_change``) rather than
    the labeling workflow, which would re-run profile auto-creation over an
    assignment the user just made explicitly.

    Fail-open: the assignment is already committed by the time this runs, so a
    broker error must not report the assignment as failed.
    """
    try:
        process_speaker_update_background.delay(
            speaker_uuid=speaker_uuid,
            user_id=user_id,
            display_name="",
            speaker_id=speaker_id,
            old_profile_id=old_profile_id,
            new_profile_id=new_profile_id,
            was_auto_labeled=False,
            display_name_changed=False,
            media_file_id=media_file_id,
        )
    except Exception:
        logger.exception(
            "Speaker %s was assigned to profile %s (COMMITTED) but retroactive "
            "matching could not be queued. Cross-recording suggestions for this "
            "profile will be missing until another speaker update runs.",
            speaker_id,
            new_profile_id,
        )


@router.post("/speakers/{speaker_uuid}/assign-profile", response_model=dict[str, Any])
def assign_speaker_to_profile(
    speaker_uuid: str,
    profile_uuid: str,
    confidence: float | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Assign a speaker instance to a profile (own or shared).

    Linking feeds the speaker's voiceprint into the profile's consolidated
    embedding, so it is a write on the speaker's file: editor or better, in the
    active tenant. The profile must be reachable in the same tenant and belong to
    the same tenant as the speaker's file.
    """
    try:
        speaker = require_speaker_access(
            db,
            speaker_uuid,
            current_user,
            organization_id=ctx.org_id,
            min_permission="editor",
            forbidden_detail="Not authorized to access this speaker",
            bypass=ctx.bypass,
        )
        speaker_id = speaker.id
        # Read before the assignment writes over it — the task's re-score gate
        # compares old against new to tell an attach from a no-op.
        prior_profile_id = int(speaker.profile_id) if speaker.profile_id else None
        media_file_id = int(speaker.media_file_id)

        profile = require_profile_in_scope(
            db, profile_uuid, current_user, organization_id=ctx.org_id, owner_only=False
        )
        if profile.organization_id != speaker.media_file.organization_id:
            raise HTTPException(status_code=404, detail="Speaker profile not found")
        profile_id = profile.id

        # assign_speaker_to_profile is a DB write (updates the speaker row, commits, then
        # flushes the rename tracker) — never construct SpeakerEmbeddingService here: it
        # imports pyannote and loads the embedding model, which the API/CI image doesn't
        # ship (→ 500 on every call) and which would load a full GPU model per request even
        # where it does, despite assign_speaker_to_profile never touching
        # self.embedding_service at all. Same pattern as get_speaker_occurrences below.
        matching_service = SpeakerMatchingService(db, None)

        # Assign speaker to profile
        updated_speaker = matching_service.assign_speaker_to_profile(
            int(speaker_id), int(profile_id), confidence, user_id=int(current_user.id)
        )

        # ─── POST-COMMIT REGION ───────────────────────────────────────────────
        # assign_speaker_to_profile commits (72d34861: the rename tracker must only
        # reach the chunk plane after the DB write is durable). From here on the
        # except block's `db.rollback()`-adjacent handling below CANNOT undo the
        # assignment, so nothing in this region may raise a 500 that tells the
        # client it did not happen. update_speaker_collections is itself fail-open
        # (it logs and returns on any OpenSearch error), so the only way to raise
        # here is a DB read for an expired ORM attribute — which is exactly the
        # case this guard covers (issue #620 item 8).
        try:
            speaker_uuid_str = str(speaker.uuid)
            profile_uuid_str = str(profile.uuid)
            response: dict[str, Any] = {
                "speaker_id": speaker_uuid_str,
                "profile_id": profile_uuid_str,
                "profile_name": profile.name,
                "confidence": confidence,
                "verified": bool(updated_speaker.verified),
                "status": "assigned",
            }
            # Update collections in OpenSearch
            # For now, we'll use a default collection (could be expanded)
            update_speaker_collections(speaker_uuid_str, int(profile_id), profile_uuid_str, [])
            _queue_retroactive_matching_after_assignment(
                speaker_uuid=speaker_uuid_str,
                user_id=int(current_user.id),
                speaker_id=int(speaker_id),
                old_profile_id=prior_profile_id,
                new_profile_id=int(profile_id),
                media_file_id=int(media_file_id),
            )
        except Exception:
            logger.exception(
                "Speaker %s was assigned to profile %s and the write is COMMITTED, but "
                "building the response / updating the OpenSearch speaker doc failed. "
                "Reporting success: the durable state is correct; re-run a speaker "
                "reindex if the search plane looks stale.",
                speaker_id,
                profile_id,
            )
            return {"status": "assigned", "profile_id": str(profile_uuid)}
        return response

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error assigning speaker to profile: {e}")
        db.rollback()
        raise ErrorHandler.internal_error() from e


def _get_embedding_suggestions(
    db: Session,
    speaker_id: int,
    current_user: User,
    threshold: float,
    accessible_profile_ids: set[int] | None = None,
    organization_id: int | None = None,
) -> list[dict[str, Any]]:
    """Get profile suggestions based on voice embeddings (``organization_id``: tenant gate)."""
    from app.core.constants import SPEAKER_CONFIDENCE_HIGH
    from app.services.opensearch_service import get_speaker_embedding
    from app.services.profile_embedding_service import ProfileEmbeddingService
    from app.services.speaker_matching_service import SpeakerMatchingService

    suggestions: list[dict[str, Any]] = []

    # Get speaker object to extract UUID
    from app.models.speaker import Speaker

    speaker = db.query(Speaker).filter(Speaker.id == speaker_id).first()
    if not speaker:
        return suggestions

    # Privacy policy (#1280): voiceprints off for the file's owner or organization means no
    # kNN at all (the LLM suggestions are transcript-derived and are added by the caller).
    if not PrivacyView(db).voiceprints(int(speaker.media_file_id)).enabled:
        return suggestions

    speaker_embedding = get_speaker_embedding(str(speaker.uuid))
    if speaker_embedding:
        profile_matches = ProfileEmbeddingService.calculate_profile_similarity(
            speaker_embedding,
            current_user.id,
            threshold=threshold,
            accessible_profile_ids=accessible_profile_ids,
            organization_id=organization_id,
        )

        for match in profile_matches:
            confidence = match["similarity"]
            matching_service = SpeakerMatchingService(db, None)  # type: ignore[arg-type]
            confidence_level = matching_service.get_confidence_level(float(confidence))
            auto_accept = confidence >= SPEAKER_CONFIDENCE_HIGH

            # Generate reason based on confidence
            if confidence >= 0.9:
                reason = f"Very strong voice match (based on {match['embedding_count']} recordings)"
            elif confidence >= 0.8:
                reason = f"Strong voice match (based on {match['embedding_count']} recordings)"
            elif confidence >= 0.7:
                reason = f"Good voice match (based on {match['embedding_count']} recordings)"
            else:
                reason = f"Possible voice match (based on {match['embedding_count']} recordings)"

            suggestions.append(
                {
                    "profile_id": match["profile_uuid"],  # Return UUID for external API
                    "profile_name": match["profile_name"],
                    "confidence": confidence,
                    "confidence_level": confidence_level,
                    "auto_accept": auto_accept,
                    "reason": reason,
                    "source": "voice_embedding",
                    "embedding_count": match["embedding_count"],
                    "last_update": match.get("last_update"),
                }
            )
    else:
        logger.warning(f"No embedding found for speaker {speaker_id}")

    return suggestions


def _get_llm_suggestions(
    db: Session,
    speaker: Speaker,
    current_user: User,
    accessible_profile_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Get profile suggestions based on LLM analysis."""
    from app.services.speaker_matching_service import SpeakerMatchingService

    suggestions: list[dict[str, Any]] = []
    if (
        speaker.suggested_name
        and speaker.confidence
        and speaker.suggestion_source == "llm_analysis"
    ):
        # Check if suggested_name matches any accessible profiles
        profile_query = db.query(SpeakerProfile).filter(
            SpeakerProfile.name.ilike(f"%{speaker.suggested_name}%"),
        )
        if accessible_profile_ids is not None:
            profile_query = profile_query.filter(SpeakerProfile.id.in_(accessible_profile_ids))
        else:
            profile_query = profile_query.filter(SpeakerProfile.user_id == current_user.id)
        suggested_profile = profile_query.first()

        matching_service = SpeakerMatchingService(db, None)  # type: ignore[arg-type]
        confidence_level = matching_service.get_confidence_level(float(speaker.confidence))

        if suggested_profile:
            # Add LLM suggestion for existing profile
            suggestions.append(
                {
                    "profile_id": str(suggested_profile.uuid),
                    "profile_name": str(suggested_profile.name),
                    "confidence": float(speaker.confidence),
                    "confidence_level": confidence_level,
                    "auto_accept": bool(speaker.confidence >= 0.8),
                    "reason": f"AI content analysis suggests this speaker is '{speaker.suggested_name}'",
                    "source": "llm_analysis",
                    "suggested_name": str(speaker.suggested_name),
                }
            )
        else:
            # Add LLM suggestion for new profile creation
            suggestions.append(
                {
                    "profile_id": None,
                    "profile_name": str(speaker.suggested_name),
                    "confidence": float(speaker.confidence),
                    "confidence_level": confidence_level,
                    "auto_accept": False,
                    "reason": f"AI content analysis suggests creating new profile for '{speaker.suggested_name}'",
                    "source": "llm_analysis",
                    "suggested_name": str(speaker.suggested_name),
                    "create_new": True,
                }
            )

    return suggestions


@router.get("/speakers/{speaker_uuid}/suggestions", response_model=list[dict[str, Any]])
def get_speaker_profile_suggestions(
    speaker_uuid: str,
    threshold: float = Query(ConfidenceLevel.MEDIUM, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Get profile suggestions for a speaker based on both embeddings and LLM analysis."""
    try:
        speaker = require_speaker_access(
            db,
            speaker_uuid,
            current_user,
            organization_id=ctx.org_id,
            forbidden_detail="Not authorized to access this speaker",
            bypass=ctx.bypass,
        )
        speaker_id = speaker.id

        # Check if speaker already has a profile
        if speaker.profile_id:
            return []

        # Get media file for audio processing
        media_file = db.query(MediaFile).filter(MediaFile.id == speaker.media_file_id).first()
        if not media_file:
            return []

        # Compute accessible profiles for cross-user matching
        # The request's tenant scope (#1027): an org request sees its org's profiles, a
        # personal one only org-less profiles — in SQL here and in the kNN below.
        # Matching candidates, not visibility: a shared profile whose owner switched
        # voiceprints off (or whose share consent is denied) is not matched against.
        accessible_ids = PermissionService.get_matchable_profile_ids(
            db, current_user.id, organization_id=ctx.org_id
        )

        # Get suggestions from different sources
        suggestions: list[dict[str, Any]] = []
        suggestions.extend(
            _get_embedding_suggestions(
                db,
                int(speaker_id),
                current_user,
                threshold,
                accessible_profile_ids=accessible_ids,
                organization_id=ctx.org_id,
            )
        )
        suggestions.extend(
            _get_llm_suggestions(db, speaker, current_user, accessible_profile_ids=accessible_ids)
        )
        # Never offer an identity the user unassigned this speaker from (#1233).
        from app.services.speaker_identity_rules import blocked_profile_names

        refused = blocked_profile_names(db, [int(speaker_id)])[int(speaker_id)]
        suggestions = [
            s for s in suggestions if str(s.get("profile_name") or "").casefold() not in refused
        ]

        # Sort suggestions by confidence (highest first) and source priority
        def sort_key(suggestion):
            source_priority = 0 if suggestion["source"] == "voice_embedding" else 1
            return (source_priority, -suggestion["confidence"])

        suggestions.sort(key=sort_key)
        return suggestions

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error getting speaker suggestions: {e}")
        raise ErrorHandler.internal_error() from e
