"""Speaker verify / confirm-gender routes and the accept/reject suggestion helpers."""

import logging
import uuid
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.speakers_profile_workflow import _get_media_file_uuid
from app.api.endpoints.speakers_profile_workflow import _handle_profile_embedding_updates
from app.api.error_handlers import ErrorHandler
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.user import User
from app.services.request_context import RequestContext
from app.services.speaker_cleanup import SpeakerCleanupIncompleteError
from app.services.speaker_cleanup import capture_speaker_impact
from app.services.speaker_cleanup import detach_from_cluster
from app.services.speaker_cleanup import lift_profile_blacklist
from app.services.speaker_cleanup import raise_if_incomplete
from app.services.speaker_cleanup import reconcile_speaker_impact
from app.services.uuid_helpers import require_profile_in_scope
from app.services.uuid_helpers import require_speaker_access
from app.utils.db_helpers import org_stamp_is
from app.utils.speaker_identity_detail import ORIGIN_ACCEPTED_SUGGESTION
from app.utils.speaker_identity_detail import ORIGIN_PROFILE_CREATED
from app.utils.speaker_identity_source import after_unlink
from app.utils.speaker_identity_source import clear_suggestion
from app.utils.speaker_identity_source import record_human_identity
from app.utils.speaker_labels import canonical_speaker_label_for_row

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/{speaker_uuid}/verify", response_model=dict[str, Any])
def verify_speaker_identification(
    speaker_uuid: str,
    action: str,  # 'accept', 'reject', 'create_profile'
    profile_uuid: str | None = None,
    profile_name: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> dict[str, Any]:
    """
    Verify or reject speaker identification suggestions.

    Every action rewrites the speaker (and may feed its voiceprint into a
    profile), so it needs editor or better on the speaker's file, in the active
    tenant; a profile must be of the speaker's tenant.

    Actions:
    - 'accept': Accept suggested profile match
    - 'reject': Reject suggestion and keep unassigned
    - 'create_profile': Create new profile and assign speaker
    """
    try:
        # Tenant, quarantine (issue #908, finding C) and editor gate.
        speaker = require_speaker_access(
            db,
            speaker_uuid,
            current_user,
            organization_id=ctx.org_id,
            min_permission="editor",
            bypass=ctx.bypass,
        )

        profile_id = _resolve_profile_uuid_to_id(profile_uuid, speaker, current_user, db, ctx)

        # Captured before dispatch commits: `expire_on_commit=True` means a post-commit
        # attribute read re-queries rather than reusing the value already in hand.
        speaker_uuid_str = str(speaker.uuid)
        media_file_uuid = _get_media_file_uuid(speaker, db)

        result = _dispatch_verify_action(
            action, speaker, speaker.id, profile_id, profile_name, current_user, db
        )

        from app.utils.websocket_notify import send_ws_event
        from app.utils.websocket_notify import send_ws_event_for_file

        _verify_notification_data = {
            "media_file_id": media_file_uuid,
            "speaker_uuid": speaker_uuid_str,
            "reason": f"speaker_verification_{action}",
        }
        if media_file_uuid is not None:
            send_ws_event_for_file(
                current_user.id,
                "speaker_updated",
                _verify_notification_data,
                file_uuid=media_file_uuid,
            )
        else:
            send_ws_event(current_user.id, "speaker_updated", _verify_notification_data)

        return result

    except (HTTPException, SpeakerCleanupIncompleteError, DomainError):
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
    ctx: RequestContext = Depends(get_current_context),
) -> dict[str, Any]:
    """Confirm or set the predicted gender for a speaker."""
    if gender not in ("male", "female"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Gender must be 'male' or 'female'",
        )

    # Tenant, quarantine (issue #908, finding C) and editor gate.
    speaker = require_speaker_access(
        db,
        speaker_uuid,
        current_user,
        organization_id=ctx.org_id,
        min_permission="editor",
        forbidden_detail="Requires editor permission",
        bypass=ctx.bypass,
    )

    speaker.predicted_gender = gender  # type: ignore[assignment]
    speaker.gender_confirmed_by_user = True  # type: ignore[assignment]

    # Captured before commit: the session default is `expire_on_commit=True`, so a
    # post-commit read of a relationship/column re-queries instead of reusing the
    # already-loaded value.
    speaker_uuid_str = str(speaker.uuid)
    media_file_uuid = _get_media_file_uuid(speaker, db)

    db.commit()

    from app.utils.websocket_notify import send_ws_event
    from app.utils.websocket_notify import send_ws_event_for_file

    _gender_notification_data = {
        "media_file_id": media_file_uuid,
        "speaker_uuid": speaker_uuid_str,
        "reason": "gender_confirmed",
    }
    if media_file_uuid is not None:
        send_ws_event_for_file(
            current_user.id,
            "speaker_updated",
            _gender_notification_data,
            file_uuid=media_file_uuid,
        )
    else:
        send_ws_event(current_user.id, "speaker_updated", _gender_notification_data)

    return {
        "speaker_uuid": speaker_uuid_str,
        "predicted_gender": gender,
        "gender_confirmed_by_user": True,
    }


# --- Helper functions for verify_speaker_identification ---


def _accept_speaker_profile_match(
    speaker: Speaker, speaker_id: int, profile_id: int, current_user: User, db: Session
) -> dict[str, Any]:
    """Handle acceptance of a speaker profile match.

    ``profile_id`` was resolved by ``_resolve_profile_uuid_to_id``, which already
    checked the caller may use it in this tenant and that it is the speaker's tenant.
    """
    profile = db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()

    if not profile:
        raise HTTPException(status_code=404, detail="Speaker profile not found")

    old_profile_id = int(speaker.profile_id) if speaker.profile_id else None
    # Accepting this profile for this one speaker overrides an earlier unassignment (#1233).
    lift_profile_blacklist(db, int(speaker_id), int(profile_id))
    # Assign speaker to profile - cast to int for assignment
    speaker.profile_id = profile_id  # type: ignore[assignment]
    speaker.verified = True  # type: ignore[assignment]
    record_human_identity(speaker, user_id=current_user.id, origin=ORIGIN_ACCEPTED_SUGGESTION)
    db.commit()

    # The new profile gains the voice; a profile the speaker leaves loses it (#1214).
    raise_if_incomplete(
        _handle_profile_embedding_updates(db, speaker_id, old_profile_id, profile_id, False)
    )

    return {
        "status": "accepted",
        "speaker_id": str(speaker.uuid),
        "profile_id": str(profile.uuid),
        "profile_name": profile.name,
        "message": f"Speaker assigned to profile '{profile.name}'",
    }


def _clusters_promoted_to_holding(db: Session, profile_id: int, speaker_id: int) -> list[Any]:
    """The clusters promoted to ``profile_id`` that count this speaker as a member."""
    from sqlalchemy import or_

    from app.models.media import SpeakerCluster
    from app.models.media import SpeakerClusterMember

    member_of = (
        db.query(SpeakerClusterMember.cluster_id)
        .filter(SpeakerClusterMember.speaker_id == speaker_id)
        .scalar_subquery()
    )
    cluster_id = db.query(Speaker.cluster_id).filter(Speaker.id == speaker_id).scalar_subquery()
    clusters: list[Any] = (
        db.query(SpeakerCluster)
        .filter(
            SpeakerCluster.promoted_to_profile_id == profile_id,
            or_(SpeakerCluster.id.in_(member_of), SpeakerCluster.id == cluster_id),
        )
        .all()
    )
    return clusters


def _rejected_profile_ids(db: Session, speaker: Speaker, linked_profile_id: int | None) -> set[int]:
    """The profiles a rejection refuses: the one linked, and the one the suggestion named.

    The suggestion's profile is looked up by name among the speaker owner's profiles in
    the speaker file's tenant, the same scope the name-based paths assign in.
    """
    from sqlalchemy import func

    rejected = {linked_profile_id} if linked_profile_id is not None else set()
    if speaker.suggested_name and str(speaker.suggested_name).strip():
        org_id = (
            db.query(MediaFile.organization_id)
            .filter(MediaFile.id == speaker.media_file_id)
            .scalar()
        )
        named = (
            db.query(SpeakerProfile.id)
            .filter(
                SpeakerProfile.user_id == speaker.user_id,
                func.lower(SpeakerProfile.name) == str(speaker.suggested_name).strip().lower(),
                org_stamp_is(SpeakerProfile.organization_id, org_id),
            )
            .first()
        )
        if named is not None:
            rejected.add(int(named[0]))
    return rejected


def _reject_speaker_suggestion(speaker: Speaker, speaker_id: int, db: Session) -> dict[str, Any]:
    """Handle rejection of a speaker identification suggestion.

    Clears ``suggested_name`` alongside ``confidence`` (issue #605) — nulling
    ``confidence`` alone left ``suggested_name`` populated, so any reader that
    checks ``suggested_name is not None`` without also checking ``confidence``
    (this module's own ``was_auto_labeled = speaker.suggested_name is not
    None and not speaker.verified``) kept surfacing a rejected suggestion as
    if it were still live. Clearing both also means the canonical label can
    genuinely move (a rejected suggestion falls back to the raw diarizer
    name), so the rejection is propagated to the chunk plane exactly like
    every other write that moves the canonical label — a suggestion the user
    rejected must not keep appearing in search facets or chat's speaker
    scope.

    ``suggestion_source`` is deliberately NOT cleared here. Every display
    reader gates on ``suggested_name``/``confidence`` too (both nulled
    above), so leaving it set never resurrects a "live" suggestion — but
    ``task_detection_service.identify_incomplete_post_transcription_files``
    reads ``suggestion_source == "llm_analysis"`` on its own as the sole
    signal for "has LLM speaker ID already run on this file". Nulling it here
    used to make a fully-rejected file look never-identified, so identify-
    speakers got re-offered (and re-dispatched) and regenerated the exact
    suggestions the user had just rejected, gated only by the ~30 minute
    ``recently_attempted`` cooldown rather than actually prevented (audit
    follow-up to issue #603).
    """
    from app.services.speaker_cleanup import REASON_REJECTED
    from app.services.speaker_cleanup import record_unassignment
    from app.services.speaker_cleanup import unlink_from_profile
    from app.services.speaker_rename_tracker import SpeakerRenameTracker

    old_profile_id = int(speaker.profile_id) if speaker.profile_id else None
    before = canonical_speaker_label_for_row(speaker)
    # Read before the write: afterwards nothing says which clusters averaged this voice.
    impact = capture_speaker_impact(db, [speaker_id], removed=False, reindex=False)
    impact.relabel_speaker_ids.add(int(speaker_id))
    # The renames below are dispatched as ONE before/after pair at the end, so the
    # shared helpers' own tracker is never flushed.
    superseded = SpeakerRenameTracker()

    # Rejecting an identification says "not this person" — an unassignment (#1233).
    # Rejecting a LINK leaves that profile's clusters and the profile by the one
    # unassign implementation (a name from the profile goes, a typed name stays, the
    # cannot-link and the profile blacklist are recorded). The profile its suggestion
    # named is recorded too, so no automatic path (retroactive matching above all)
    # applies either identity again.
    if old_profile_id is not None:
        for cluster in _clusters_promoted_to_holding(db, old_profile_id, int(speaker_id)):
            impact |= detach_from_cluster(
                db, cluster, [speaker], blacklist=True, tracker=superseded
            )
        unlink_from_profile(db, [speaker], old_profile_id, superseded)
    for profile_id in sorted(_rejected_profile_ids(db, speaker, old_profile_id)):
        record_unassignment(
            db, [speaker_id], against_speaker_ids=[], profile_id=profile_id, reason=REASON_REJECTED
        )

    # Mark as verified but don't assign to profile - use setattr for proper type handling
    speaker.profile_id = None  # type: ignore[assignment]
    after_unlink(speaker)  # idempotent after unlink_from_profile; covers a speaker with no link
    speaker.verified = True  # type: ignore[assignment]
    clear_suggestion(speaker)
    after = canonical_speaker_label_for_row(speaker)
    db.commit()

    if before != after:
        try:
            from app.tasks.rename_propagation_task import dispatch_speaker_rename

            file_uuid = _get_media_file_uuid(speaker, db)
            dispatch_speaker_rename([(file_uuid, before)], after, speaker_id=speaker_id)
        except Exception as exc:  # noqa: BLE001 — a rejection must not fail on dispatch
            logger.warning(
                f"Could not queue chunk-plane label sync for rejected speaker {speaker_id}: {exc}"
            )

    # The profile and clusters the speaker leaves stop averaging its voice (#1214).
    if old_profile_id is not None:
        impact.profile_ids.add(old_profile_id)
    raise_if_incomplete(reconcile_speaker_impact(db, impact))

    return {
        "status": "rejected",
        "speaker_id": str(speaker.uuid),
        "message": "Speaker identification suggestion rejected",
    }


def _create_new_speaker_profile(
    speaker: Speaker, speaker_id: int, profile_name: str, current_user: User, db: Session
) -> dict[str, Any]:
    """Handle creation of a new speaker profile."""
    # Names are unique per user per tenant (v430); the profile joins the file's tenant.
    existing_profile = (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.user_id == current_user.id,
            SpeakerProfile.name == profile_name,
            org_stamp_is(SpeakerProfile.organization_id, speaker.media_file.organization_id),
        )
        .first()
    )

    if existing_profile:
        raise HTTPException(status_code=400, detail="Profile with this name already exists")

    # Create new profile, stamped with the tenant of the speaker's file (issue #262e)
    new_profile = SpeakerProfile(
        user_id=current_user.id,
        name=profile_name,
        uuid=str(uuid.uuid4()),
        organization_id=speaker.media_file.organization_id,
    )

    db.add(new_profile)
    db.flush()

    old_profile_id = int(speaker.profile_id) if speaker.profile_id else None
    # Assign speaker to new profile - use setattr for proper type handling
    speaker.profile_id = new_profile.id  # type: ignore[assignment]
    speaker.verified = True  # type: ignore[assignment]
    record_human_identity(speaker, user_id=current_user.id, origin=ORIGIN_PROFILE_CREATED)
    db.commit()

    # The new profile gains the voice; a profile the speaker leaves loses it (#1214).
    raise_if_incomplete(
        _handle_profile_embedding_updates(
            db, speaker_id, old_profile_id, int(new_profile.id), False
        )
    )

    return {
        "status": "created_and_assigned",
        "speaker_id": str(speaker.uuid),
        "profile_id": str(new_profile.uuid),
        "profile_name": profile_name,
        "message": f"Created new profile '{profile_name}' and assigned speaker",
    }


def _resolve_profile_uuid_to_id(
    profile_uuid: str | None,
    speaker: Speaker,
    current_user: User,
    db: Session,
    ctx: RequestContext,
) -> int | None:
    """Convert profile UUID to internal ID if provided, validating access.

    The profile must be usable by the caller in the active tenant AND belong to
    the tenant of the speaker's file, so a voiceprint never joins another tenant's
    profile.
    """
    if not profile_uuid:
        return None

    profile = require_profile_in_scope(
        db, profile_uuid, current_user, organization_id=ctx.org_id, owner_only=False
    )
    if profile.organization_id != speaker.media_file.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Speaker profile not found"
        )
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
