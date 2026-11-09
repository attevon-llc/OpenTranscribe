"""Speaker selections behind labeling, suggestion and gender endpoints.

Each selection is confined to a tenant (``file_ids_in_scope``); the ones that write also skip
files under a legal hold (``unheld_file_ids``). Functions take a ``Session`` and plain
arguments and never commit; the caller owns the transaction.
"""

from typing import Any

from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from app.core.tenancy import OrgScope
from app.models.speaker import Speaker
from app.services.hold_guard import unheld_file_ids
from app.services.permission_service import file_ids_in_scope

SUGGESTION_BAND_LOW = 0.5
SUGGESTION_BAND_HIGH = 0.75


def medium_confidence_suggestions(
    db: Session, user_id: int, organization_id: OrgScope, suggested_name: Any
) -> list[Speaker]:
    """Unverified speakers carrying a medium-confidence suggestion for ``suggested_name``."""
    return (
        db.query(Speaker)
        .filter(
            Speaker.user_id == user_id,
            Speaker.media_file_id.in_(file_ids_in_scope(organization_id)),
            Speaker.suggested_name == suggested_name,
            Speaker.confidence >= SUGGESTION_BAND_LOW,
            Speaker.confidence < SUGGESTION_BAND_HIGH,
            Speaker.verified.is_(False),
        )
        .all()
    )


def candidate_speaker_rows(
    db: Session, user_id: int, excluding_speaker_id: int, organization_id: OrgScope
) -> list[Row[Any]]:
    """Id, uuid, name, display name and verified flag of the user's other in-tenant speakers.

    Speakers on a held file are left out: they keep the identity they had when held (no
    auto-applied name, no new suggestion).
    """
    return (
        db.query(
            Speaker.id,
            Speaker.uuid,
            Speaker.name,
            Speaker.display_name,
            Speaker.verified,
        )
        .filter(
            Speaker.user_id == user_id,
            Speaker.id != excluding_speaker_id,
            Speaker.media_file_id.in_(file_ids_in_scope(organization_id)),
            Speaker.media_file_id.in_(unheld_file_ids()),
        )
        .all()
    )


def set_gender_on_profile_speakers(
    db: Session, profile_id: int, user_id: int, organization_id: OrgScope, gender: str
) -> int:
    """Confirm ``gender`` on the user's in-tenant speakers linked to the profile; returns the count.

    Speakers on a held file keep the gender they had when held.
    """
    return (
        db.query(Speaker)
        .filter(
            Speaker.profile_id == profile_id,
            Speaker.user_id == user_id,
            Speaker.media_file_id.in_(file_ids_in_scope(organization_id)),
            Speaker.media_file_id.in_(unheld_file_ids()),
        )
        .update(
            {"predicted_gender": gender, "gender_confirmed_by_user": True},
            synchronize_session="fetch",
        )
    )
