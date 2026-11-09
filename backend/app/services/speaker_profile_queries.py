"""Speaker-profile list, count and membership queries behind the profile endpoints.

The endpoints decide who may see which profiles (``org_scope_pred``, the accessible-id lists)
and pass the result in as a SQL criterion. Functions take a ``Session`` and plain arguments and
never commit.
"""

from collections.abc import Collection

from sqlalchemy import ColumnExpressionArgument
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerCollection
from app.models.speaker import SpeakerCollectionMember
from app.models.speaker import SpeakerProfile
from app.models.user import User
from app.services.file_availability import available_pred
from app.services.privacy.redact_view import PrivacyView
from app.services.privacy.redact_view import machine_identity_clauses
from app.services.request_context import RequestContext
from app.services.tenant_scope import context_scope_criterion
from app.utils.db_helpers import org_stamp_is


def users_with_ids(db: Session, user_ids: Collection[int]) -> list[User]:
    return db.query(User).filter(User.id.in_(user_ids)).all()


def list_profiles(
    db: Session,
    criterion: ColumnExpressionArgument[bool] | None,
    *,
    collection_id: int | None = None,
) -> list[SpeakerProfile]:
    """Profiles satisfying ``criterion`` (``None`` = all), optionally only a collection's members."""
    query = db.query(SpeakerProfile)
    if criterion is not None:
        query = query.filter(criterion)
    if collection_id is not None:
        query = query.join(SpeakerCollectionMember).filter(
            SpeakerCollectionMember.collection_id == collection_id
        )
    return query.all()


def profile_ids_owned_by(db: Session, user_id: int) -> set[int]:
    rows = db.query(SpeakerProfile.id).filter(SpeakerProfile.user_id == user_id).all()
    return {row[0] for row in rows}


def profile_media_instance_counts(
    db: Session, profile_ids: list[int], *, include_quarantined: bool = False
) -> dict[int, tuple[int, int]]:
    """Per-profile ``(media_count, instance_count)`` over available files only.

    Hides voiceprint links the policy hides, and every speaker of an unavailable (trashed or
    quarantined) file: the profile would otherwise report appearances no read surface shows.
    ``include_quarantined`` is the list exemption (``unavailable_list_exempt``).
    """
    # A link a voiceprint made is not counted while its file's voiceprints are off
    # (#1280): the profile would otherwise report appearances the speaker pages hide.
    # Decided per speaker by its FILE's scope; a person's own link is always counted.
    _name_hidden, link_hidden = machine_identity_clauses(
        PrivacyView(db),
        select(Speaker.media_file_id).where(Speaker.profile_id.in_(profile_ids)),
    )
    count_rows = (
        db.query(
            Speaker.profile_id,
            func.count(func.distinct(Speaker.media_file_id)).label("media_count"),
            func.count(Speaker.id).label("instance_count"),
        )
        .join(MediaFile, MediaFile.id == Speaker.media_file_id)
        .filter(
            Speaker.profile_id.in_(profile_ids),
            ~link_hidden,
            available_pred(include_quarantined=include_quarantined),
        )
        .group_by(Speaker.profile_id)
        .all()
    )
    return {row.profile_id: (row.media_count, row.instance_count) for row in count_rows}


def speakers_linked_to(db: Session, profile_id: int) -> list[Speaker]:
    return db.query(Speaker).filter(Speaker.profile_id == profile_id).all()


def clusters_promoted_to(db: Session, profile_id: int) -> list[SpeakerCluster]:
    return (
        db.query(SpeakerCluster).filter(SpeakerCluster.promoted_to_profile_id == profile_id).all()
    )


def user_collections(db: Session, user_id: int, ctx: RequestContext) -> list[SpeakerCollection]:
    """The user's own speaker collections within the request's tenant."""
    return (
        db.query(SpeakerCollection)
        .filter(context_scope_criterion(SpeakerCollection, ctx))
        .filter(SpeakerCollection.user_id == user_id)
        .all()
    )


def collection_member_counts(db: Session, collection_ids: Collection[int]) -> dict[int, int]:
    """Profiles per collection in one query; collections with no member are absent."""
    if not collection_ids:
        return {}
    rows = (
        db.query(SpeakerCollectionMember.collection_id, func.count().label("cnt"))
        .filter(SpeakerCollectionMember.collection_id.in_(collection_ids))
        .group_by(SpeakerCollectionMember.collection_id)
        .all()
    )
    return {row.collection_id: row.cnt for row in rows}


def collection_by_exact_name(
    db: Session, user_id: int, name: str, organization_id: int | None
) -> SpeakerCollection | None:
    """The user's collection with exactly this name in the tenant."""
    return (
        db.query(SpeakerCollection)
        .filter(
            SpeakerCollection.user_id == user_id,
            SpeakerCollection.name == name,
            org_stamp_is(SpeakerCollection.organization_id, organization_id),
        )
        .first()
    )
