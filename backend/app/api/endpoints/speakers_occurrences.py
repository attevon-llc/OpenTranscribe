"""Cross-media occurrences of a speaker."""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.error_handlers import ErrorHandler
from app.core.exceptions import DomainError
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.user import User
from app.services.permission_service import org_scope_pred
from app.services.platform_bypass import PlatformBypass
from app.services.privacy.redact_view import PrivacyView
from app.services.privacy.redact_view import hide_machine_identities
from app.services.request_context import RequestContext
from app.services.uuid_helpers import require_speaker_access

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{speaker_uuid}/cross-media", response_model=list[dict[str, Any]])
def get_speaker_cross_media_occurrences(
    speaker_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> list[dict[str, Any]]:
    """
    Get all media files of the active tenant where this speaker (or their profile) appears.
    """
    try:
        # Tenant, quarantine (hidden from its own owner too) and file-access gate.
        speaker = require_speaker_access(
            db, speaker_uuid, current_user, organization_id=ctx.org_id, bypass=ctx.bypass
        )

        # What this speaker's file lets the caller see of its name and link. A name or
        # link a voiceprint made is not an identity to match on while that file's
        # voiceprints are off (issue #1280); the matching below uses the redacted values.
        hide_machine_identities(PrivacyView(db), [speaker])
        if speaker.profile_id:
            result = _get_profile_based_occurrences(
                speaker, current_user, db, ctx.org_id, bypass=ctx.bypass
            )
        else:
            result = _get_display_name_based_occurrences(
                speaker, current_user, db, ctx.org_id, bypass=ctx.bypass
            )

        # Sort by confidence (highest first), with same_speaker files prioritized
        result.sort(key=lambda x: (x["same_speaker"], x.get("confidence") or 0.0), reverse=True)

        return result

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error getting cross-media occurrences: {e}")
        raise ErrorHandler.internal_error() from e


# --- Helper functions for get_speaker_cross_media_occurrences ---


def _build_occurrence_dict(
    media_file: MediaFile,
    speaker_obj: Speaker,
    same_speaker: bool,
    *,
    match_withheld: bool = False,
) -> dict[str, Any]:
    """Build occurrence dictionary for a speaker/media file pair.

    ``match_withheld``: a voiceprint made the speaker's identity and its file's voiceprints
    are off, so the match confidence is not shown either.
    """
    return {
        "media_file_id": str(media_file.uuid),
        "filename": media_file.filename,
        "title": media_file.title or media_file.filename,
        "media_file_title": media_file.title or media_file.filename,
        "upload_time": media_file.upload_time.isoformat() if media_file.upload_time else "",
        "speaker_label": speaker_obj.name,
        "confidence": None if match_withheld else speaker_obj.confidence,
        "verified": speaker_obj.verified,
        "same_speaker": same_speaker,
    }


def _get_profile_based_occurrences(
    speaker: Speaker,
    current_user: User,
    db: Session,
    organization_id: OrgScope = UNSCOPED,
    *,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> list[dict[str, Any]]:
    """Get cross-media occurrences for a speaker with a profile."""
    query = (
        db.query(Speaker)
        .join(MediaFile)
        .filter(
            Speaker.profile_id == speaker.profile_id,
        )
    )
    if not bypass.sees_all_in_scope:
        query = query.filter(Speaker.user_id == current_user.id)
    if not bypass.user_is_admin:
        query = query.filter(MediaFile.is_quarantined.is_(False))
    tenant_pred = org_scope_pred(MediaFile.organization_id, organization_id)
    if tenant_pred is not None:
        query = query.filter(tenant_pred)
    # Each candidate is judged by its OWN file's switch: a link a voiceprint made in a file
    # whose voiceprints are off is not a link (its profile_id reads None after this), so the
    # speaker connects nothing. ``speaker`` arrives already redacted.
    profile_speakers = query.all()
    hide_machine_identities(PrivacyView(db), profile_speakers)

    result: list[dict[str, Any]] = []
    for profile_speaker in profile_speakers:
        if profile_speaker.profile_id != speaker.profile_id:
            continue
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
    speaker: Speaker,
    current_user: User,
    db: Session,
    organization_id: OrgScope = UNSCOPED,
    *,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> list[dict[str, Any]]:
    """Get cross-media occurrences for a speaker without a profile, by display name."""
    result: list[dict[str, Any]] = []

    # Add this speaker instance first. ``speaker`` arrives redacted; when its identity was
    # a voiceprint's, the match confidence goes with it.
    if speaker.media_file:
        result.append(
            _build_occurrence_dict(
                speaker.media_file,
                speaker,
                same_speaker=True,
                match_withheld=bool(hide_machine_identities(PrivacyView(db), [speaker])),
            )
        )

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
    if not bypass.sees_all_in_scope:
        similar_q = similar_q.filter(Speaker.user_id == current_user.id)
    if not bypass.user_is_admin:
        similar_q = similar_q.filter(MediaFile.is_quarantined.is_(False))
    tenant_pred = org_scope_pred(MediaFile.organization_id, organization_id)
    if tenant_pred is not None:
        similar_q = similar_q.filter(tenant_pred)
    similar_speakers = similar_q.all()
    hide_machine_identities(PrivacyView(db), similar_speakers)

    for similar_speaker in similar_speakers:
        if similar_speaker.display_name != speaker.display_name:
            continue  # a voiceprint made this name and its file's voiceprints are off
        if similar_speaker.media_file:
            result.append(
                _build_occurrence_dict(
                    similar_speaker.media_file, similar_speaker, same_speaker=False
                )
            )

    return result
