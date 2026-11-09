"""Read and bulk-write queries behind the media collection endpoints.

Every function takes the request's ``Session`` and plain arguments. Commit points stay in the
routers: nothing here commits.
"""

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement
from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.engine import Row
from sqlalchemy.orm import Query
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.group import UserGroupMember
from app.models.media import MediaFile
from app.models.organization import OrganizationMembership
from app.models.prompt import SummaryPrompt
from app.models.sharing import CollectionShare
from app.models.user import User
from app.services.collection_visibility import visible_collection_ids
from app.services.file_availability import exclude_unavailable
from app.services.permission_service import PermissionService


def find_usable_prompt(db: Session, prompt_uuid: str, user_id: int) -> SummaryPrompt | None:
    """An active prompt the user owns or that is a system default, or None."""
    return (
        db.query(SummaryPrompt)
        .filter(
            SummaryPrompt.uuid == prompt_uuid,
            SummaryPrompt.is_active,
            or_(SummaryPrompt.is_system_default, SummaryPrompt.user_id == user_id),
        )
        .first()
    )


# --- collections -------------------------------------------------------------------------


def collection_name_taken_for_creator(
    db: Session, user_id: int, organization_id: int | None, name: str
) -> bool:
    """Whether the tenant already holds a collection called ``name`` visible to the creator."""
    return (
        db.query(Collection.id)
        .filter(
            PermissionService.collection_tenant_pred(user_id, organization_id),
            Collection.name == name,
        )
        .first()
        is not None
    )


def collection_name_taken_in_tenant_of(db: Session, collection: Collection, name: str) -> bool:
    """Whether another collection in ``collection``'s own tenant is already called ``name``."""
    if collection.organization_id is not None:
        same_tenant = Collection.organization_id == collection.organization_id
    else:
        same_tenant = and_(
            Collection.user_id == collection.user_id, Collection.organization_id.is_(None)
        )
    return (
        db.query(Collection.id)
        .filter(same_tenant, Collection.name == name, Collection.id != collection.id)
        .first()
        is not None
    )


def load_collection_detail(db: Session, collection_id: int) -> Collection | None:
    """A collection with its members, their files and its default prompt eagerly loaded."""
    return (
        db.query(Collection)
        .filter(Collection.id == collection_id)
        .options(
            joinedload(Collection.collection_members).joinedload(CollectionMember.media_file),
            joinedload(Collection.default_summary_prompt),
        )
        .first()
    )


def collection_member_file_ids(db: Session, collection_id: int) -> list[int]:
    """Ids of every file in the collection."""
    return [
        cm.media_file_id
        for cm in db.query(CollectionMember.media_file_id)
        .filter(CollectionMember.collection_id == collection_id)
        .all()
    ]


def collection_has_shares(db: Session, collection_id: int) -> bool:
    """Whether the collection has at least one share."""
    return (
        db.query(CollectionShare.id).filter(CollectionShare.collection_id == collection_id).first()
        is not None
    )


# --- listing -----------------------------------------------------------------------------


def visible_media_counts(
    db: Session, collection_ids: list[int], *, include_quarantined: bool
) -> dict[int, int]:
    """Per-collection member counts, hiding quarantined files unless ``include_quarantined``.

    Collections absent from the result have zero visible members.
    """
    if not collection_ids:
        return {}
    query: Query[Any] = (
        db.query(CollectionMember.collection_id, func.count(CollectionMember.id))
        .join(MediaFile, MediaFile.id == CollectionMember.media_file_id)
        .filter(CollectionMember.collection_id.in_(collection_ids))
    )
    query = exclude_unavailable(query, include_quarantined=include_quarantined)
    return {cid: cnt for cid, cnt in query.group_by(CollectionMember.collection_id).all()}


def ids_in_tenant_of_user(
    db: Session, collection_ids: list[int], user_id: int, organization_id: int | None
) -> set[int]:
    """The subset of ``collection_ids`` inside the user's own tenant (``collection_tenant_pred``)."""
    return {
        cid
        for (cid,) in db.query(Collection.id).filter(
            Collection.id.in_(collection_ids),
            PermissionService.collection_tenant_pred(user_id, organization_id),
        )
    }


def ids_matching(
    db: Session, collection_ids: list[int], tenant_pred: ColumnElement[bool]
) -> set[int]:
    """The subset of ``collection_ids`` satisfying the request's tenant predicate."""
    return {
        cid
        for (cid,) in db.query(Collection.id).filter(Collection.id.in_(collection_ids), tenant_pred)
    }


def page_matching_ids(
    db: Session,
    collection_ids: list[int],
    tenant_pred: ColumnElement[bool],
    *,
    skip: int,
    limit: int,
) -> list[int]:
    """One unordered page of ``collection_ids`` satisfying the request's tenant predicate."""
    return [
        row[0]
        for row in db.query(Collection.id)
        .filter(Collection.id.in_(collection_ids), tenant_pred)
        .offset(skip)
        .limit(limit)
        .all()
    ]


def page_own_collection_ids(
    db: Session,
    collection_ids: list[int],
    *,
    user_id: int,
    organization_id: int | None,
    tenant_pred: ColumnElement[bool],
    skip: int,
    limit: int,
) -> list[int]:
    """One page, ordered by id, of the user's own-tenant collections among ``collection_ids``."""
    return [
        row[0]
        for row in db.query(Collection.id)
        .filter(
            PermissionService.collection_tenant_pred(user_id, organization_id),
            tenant_pred,
            Collection.id.in_(collection_ids),
        )
        .order_by(Collection.id)
        .offset(skip)
        .limit(limit)
        .all()
    ]


def collections_with_owners(db: Session, collection_ids: list[int]) -> list[Collection]:
    """Collections by id with their owner eagerly loaded."""
    return (
        db.query(Collection)
        .options(joinedload(Collection.user))
        .filter(Collection.id.in_(collection_ids))
        .all()
    )


def collections_for_listing(
    db: Session, collection_ids: list[int], tenant_pred: ColumnElement[bool]
) -> list[Collection]:
    """Collections by id with owner and default prompt loaded, re-gated by the tenant predicate."""
    return (
        db.query(Collection)
        .options(
            joinedload(Collection.user),
            joinedload(Collection.default_summary_prompt),
        )
        .filter(Collection.id.in_(collection_ids), tenant_pred)
        .all()
    )


def share_counts(db: Session, collection_ids: list[int]) -> dict[int, int]:
    """``{collection_id: number_of_shares}`` for the collections that have any."""
    rows = (
        db.query(
            CollectionShare.collection_id,
            func.count(CollectionShare.id).label("share_count"),
        )
        .filter(CollectionShare.collection_id.in_(collection_ids))
        .group_by(CollectionShare.collection_id)
    ).all()
    return {row[0]: row[1] for row in rows}


def shares_reaching_user(
    db: Session, collection_ids: list[int], user_id: int
) -> list[CollectionShare]:
    """Shares of these collections that target the user directly or one of their groups."""
    user_group_ids = (
        db.query(UserGroupMember.group_id).filter(UserGroupMember.user_id == user_id).subquery()
    )
    return (
        db.query(CollectionShare)
        .options(joinedload(CollectionShare.shared_by))
        .filter(
            CollectionShare.collection_id.in_(collection_ids),
            or_(
                CollectionShare.target_user_id == user_id,
                CollectionShare.target_group_id.in_(db.query(user_group_ids.c.group_id)),
            ),
        )
        .all()
    )


def collections_holding_files(
    db: Session,
    file_ids: list[int],
    *,
    user_id: int,
    organization_id: int | None,
) -> list[Row[tuple[Collection, int]]]:
    """``(collection, distinct selected files in it)`` for the caller's visible collections.

    Ordered by that count descending, then name. Only the caller's own-tenant collections the
    caller may see are considered.
    """
    held = func.count(func.distinct(CollectionMember.media_file_id))
    return (
        db.query(Collection, held)
        .join(CollectionMember, CollectionMember.collection_id == Collection.id)
        .filter(
            CollectionMember.media_file_id.in_(file_ids),
            PermissionService.collection_tenant_pred(user_id, organization_id),
            Collection.id.in_(visible_collection_ids(db, user_id, organization_id)),
        )
        .group_by(Collection.id)
        .order_by(held.desc(), Collection.name)
        .all()
    )


# --- membership --------------------------------------------------------------------------


def files_in_scope(
    db: Session, file_uuids: list[UUID], file_pred: ColumnElement[bool]
) -> list[MediaFile]:
    """The files among ``file_uuids`` that satisfy ``file_pred``."""
    return db.query(MediaFile).filter(MediaFile.uuid.in_(file_uuids), file_pred).all()


def existing_member_ids(db: Session, collection_id: int, file_ids: list[int]) -> set[int]:
    """The subset of ``file_ids`` already in the collection."""
    rows = (
        db.query(CollectionMember.media_file_id)
        .filter(
            CollectionMember.collection_id == collection_id,
            CollectionMember.media_file_id.in_(file_ids),
        )
        .all()
    )
    return {member[0] for member in rows}


def resolve_file_ids(
    db: Session, file_uuids: list[UUID], *, owner_user_id: int | None
) -> list[Row[tuple[int, Any]]]:
    """``(id, uuid)`` for the given file uuids, restricted to one owner when given."""
    query = db.query(MediaFile.id, MediaFile.uuid).filter(MediaFile.uuid.in_(file_uuids))
    if owner_user_id is not None:
        query = query.filter(MediaFile.user_id == owner_user_id)
    return query.all()


def remove_members(db: Session, collection_id: int, file_ids: Iterable[int]) -> int:
    """Delete the collection's membership rows for ``file_ids``; returns how many went."""
    return (
        db.query(CollectionMember)
        .filter(
            CollectionMember.collection_id == collection_id,
            CollectionMember.media_file_id.in_(list(file_ids)),
        )
        .delete(synchronize_session=False)
    )


def collection_files_query(db: Session, collection_id: int, options: list[Any]) -> Query[MediaFile]:
    """Base ``MediaFile`` query over one collection's members, with loader ``options``."""
    return (
        db.query(MediaFile)
        .options(*options)
        .join(CollectionMember, CollectionMember.media_file_id == MediaFile.id)
        .filter(CollectionMember.collection_id == collection_id)
    )


# --- shares ------------------------------------------------------------------------------


def share_target_user_ids(
    db: Session, share: CollectionShare, organization_id: int | None
) -> list[int]:
    """User ids a share reaches: the target user, or every member of the target group.

    For an organization collection only members of that organization are returned.
    """
    if share.target_type == "user" and share.target_user_id:
        query = db.query(User.id).filter(User.id == share.target_user_id)
    elif share.target_type == "group" and share.target_group_id:
        query = db.query(UserGroupMember.user_id).filter(
            UserGroupMember.group_id == share.target_group_id
        )
    else:
        return []
    if organization_id is not None:
        user_col = User.id if share.target_type == "user" else UserGroupMember.user_id
        query = query.filter(
            _is_org_member(user_col, organization_id),
        )
    return [int(row[0]) for row in query.all()]


def _is_org_member(user_col: Any, organization_id: int) -> Any:
    from sqlalchemy import exists

    return exists().where(
        OrganizationMembership.user_id == user_col,
        OrganizationMembership.organization_id == organization_id,
    )


def list_shares(db: Session, collection_id: int, *, skip: int, limit: int) -> list[CollectionShare]:
    """A page of the collection's shares, newest first, with sharer and target loaded."""
    return (
        db.query(CollectionShare)
        .options(
            joinedload(CollectionShare.shared_by),
            joinedload(CollectionShare.target_user),
            joinedload(CollectionShare.target_group),
        )
        .filter(CollectionShare.collection_id == collection_id)
        .order_by(CollectionShare.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def load_share(db: Session, share_id: int) -> CollectionShare | None:
    """One share with sharer and target eagerly loaded."""
    return (
        db.query(CollectionShare)
        .options(
            joinedload(CollectionShare.shared_by),
            joinedload(CollectionShare.target_user),
            joinedload(CollectionShare.target_group),
        )
        .filter(CollectionShare.id == share_id)
        .first()
    )


def is_organization_member(db: Session, organization_id: int, user_id: int) -> bool:
    """Whether the user holds a membership in the organization."""
    return (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
        .first()
        is not None
    )


def share_to_user_exists(db: Session, collection_id: int, user_id: int) -> bool:
    """Whether the collection is already shared with this user."""
    return (
        db.query(CollectionShare)
        .filter(
            CollectionShare.collection_id == collection_id,
            CollectionShare.target_user_id == user_id,
        )
        .first()
        is not None
    )


def share_to_group_exists(db: Session, collection_id: int, group_id: int) -> bool:
    """Whether the collection is already shared with this group."""
    return (
        db.query(CollectionShare)
        .filter(
            CollectionShare.collection_id == collection_id,
            CollectionShare.target_group_id == group_id,
        )
        .first()
        is not None
    )


def group_member_user_ids(db: Session, group_id: int) -> set[int]:
    """User ids of every member of the group."""
    return {
        int(row[0])
        for row in db.query(UserGroupMember.user_id)
        .filter(UserGroupMember.group_id == group_id)
        .all()
    }


def organization_member_user_ids(db: Session, organization_id: int) -> set[int]:
    """User ids of every member of the organization."""
    return {
        int(row[0])
        for row in db.query(OrganizationMembership.user_id)
        .filter(OrganizationMembership.organization_id == organization_id)
        .all()
    }
