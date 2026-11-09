"""Row lookups the speaker endpoints share: one speaker, profile, file or match at a time.

Functions take a ``Session`` and plain arguments and never commit; the caller owns the
transaction. Lookups that are tenant-scoped take the file's ``organization_id`` (``None`` =
personal) and apply ``org_stamp_is``, so a profile of another tenant is never found.
"""

from collections.abc import Collection
from typing import Any

from sqlalchemy import ColumnExpressionArgument
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember
from app.models.speaker import SpeakerMatch
from app.models.speaker import SpeakerProfile
from app.utils.db_helpers import org_stamp_is


def speaker_by_id(db: Session, speaker_id: int) -> Speaker | None:
    return db.query(Speaker).filter(Speaker.id == speaker_id).first()


def speakers_by_id(db: Session, speaker_ids: Collection[int]) -> dict[int, Speaker]:
    """The speakers with these ids, keyed by id."""
    rows = db.query(Speaker).filter(Speaker.id.in_(list(speaker_ids))).all()
    return {int(row.id): row for row in rows}


def speaker_file_id(db: Session, speaker_id: int) -> int | None:
    """The id of the file a speaker belongs to."""
    value = db.query(Speaker.media_file_id).filter(Speaker.id == speaker_id).scalar()
    return None if value is None else int(value)


def speaker_labels_for_profile(db: Session, profile_id: int) -> list[tuple[Any, Any]]:
    """``(uuid, display_name)`` of every speaker linked to the profile."""
    rows = (
        db.query(Speaker.uuid, Speaker.display_name).filter(Speaker.profile_id == profile_id).all()
    )
    return [(row[0], row[1]) for row in rows]


def file_by_id(db: Session, media_file_id: int) -> MediaFile | None:
    return db.query(MediaFile).filter(MediaFile.id == media_file_id).first()


def file_organization_id(db: Session, media_file_id: int) -> int | None:
    """The tenant of a file: its ``organization_id`` (``None`` = personal)."""
    value = db.query(MediaFile.organization_id).filter(MediaFile.id == media_file_id).scalar()
    return None if value is None else int(value)


def file_filename(db: Session, media_file_id: int) -> str | None:
    row = db.query(MediaFile.filename).filter(MediaFile.id == media_file_id).first()
    return None if row is None else str(row[0])


def file_uuid(db: Session, media_file_id: int) -> str | None:
    row = db.query(MediaFile.uuid).filter(MediaFile.id == media_file_id).first()
    return None if row is None else str(row[0])


def profile_by_id(db: Session, profile_id: int) -> SpeakerProfile | None:
    return db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()


def profile_uuid(db: Session, profile_id: int) -> str | None:
    profile = profile_by_id(db, profile_id)
    return str(profile.uuid) if profile else None


def owned_profile_in_scope(
    db: Session, profile_id: int, user_id: int, scope: ColumnExpressionArgument[bool]
) -> SpeakerProfile | None:
    """The user's own profile ``profile_id`` when it also satisfies the tenant ``scope``."""
    return (
        db.query(SpeakerProfile)
        .filter(SpeakerProfile.id == profile_id, SpeakerProfile.user_id == user_id, scope)
        .first()
    )


def profile_by_exact_name(
    db: Session,
    user_id: int,
    name: str,
    organization_id: int | None,
    *,
    excluding_profile_id: int | None = None,
) -> SpeakerProfile | None:
    """The user's profile with exactly this name in the tenant (names are unique per tenant).

    ``excluding_profile_id`` leaves one profile out, so a rename does not collide with itself.
    """
    query = db.query(SpeakerProfile).filter(
        SpeakerProfile.user_id == user_id,
        SpeakerProfile.name == name,
        org_stamp_is(SpeakerProfile.organization_id, organization_id),
    )
    if excluding_profile_id is not None:
        query = query.filter(SpeakerProfile.id != excluding_profile_id)
    return query.first()


def profile_by_name_pattern(
    db: Session, user_id: int, pattern: str, organization_id: int | None
) -> SpeakerProfile | None:
    """The user's profile whose name matches ``pattern`` (SQL ``ILIKE``) in the tenant."""
    return (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.user_id == user_id,
            SpeakerProfile.name.ilike(pattern),
            org_stamp_is(SpeakerProfile.organization_id, organization_id),
        )
        .first()
    )


def profile_id_by_lowercased_name(
    db: Session, user_id: int, lowered_name: str, organization_id: int | None
) -> int | None:
    """Id of the user's profile whose lowercased name equals ``lowered_name`` in the tenant."""
    row = (
        db.query(SpeakerProfile.id)
        .filter(
            SpeakerProfile.user_id == user_id,
            func.lower(SpeakerProfile.name) == lowered_name,
            org_stamp_is(SpeakerProfile.organization_id, organization_id),
        )
        .first()
    )
    return None if row is None else int(row[0])


def clusters_promoted_to_holding(db: Session, profile_id: int, speaker_id: int) -> list[Any]:
    """The clusters promoted to ``profile_id`` that count this speaker as a member."""
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


def speaker_match_for_pair(
    db: Session, smaller_speaker_id: int, larger_speaker_id: int
) -> SpeakerMatch | None:
    """The stored match for a speaker pair (ids ordered smaller first)."""
    return (
        db.query(SpeakerMatch)
        .filter(
            SpeakerMatch.speaker1_id == smaller_speaker_id,
            SpeakerMatch.speaker2_id == larger_speaker_id,
        )
        .first()
    )


def profile_named_like(
    db: Session,
    name: str,
    *,
    accessible_profile_ids: Collection[int] | None,
    user_id: int,
) -> SpeakerProfile | None:
    """A profile whose name contains ``name`` (case-insensitive), among those the caller may use.

    ``accessible_profile_ids`` limits the search to those profiles; ``None`` falls back to the
    caller's own profiles.
    """
    query = db.query(SpeakerProfile).filter(SpeakerProfile.name.ilike(f"%{name}%"))
    if accessible_profile_ids is not None:
        query = query.filter(SpeakerProfile.id.in_(accessible_profile_ids))
    else:
        query = query.filter(SpeakerProfile.user_id == user_id)
    return query.first()


def reassign_transcript_segments(db: Session, from_speaker_id: int, to_speaker_id: int) -> None:
    """Point every transcript segment of ``from_speaker_id`` at ``to_speaker_id``; no commit."""
    db.query(TranscriptSegment).filter(TranscriptSegment.speaker_id == from_speaker_id).update(
        {"speaker_id": to_speaker_id}
    )
