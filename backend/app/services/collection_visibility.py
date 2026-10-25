"""The one rule for which collections a caller may see, and which files that makes readable.

Every surface that can name a collection, filter by one, or reach a file through one asks
here (issues #1051, #1275; ``docs/design/v060_org_content_governance_plan.md`` section 3.1):
the collection lists, the file page's collection list, search cards and the search filter,
chat scope, auto-label and topic suggestions, and the upload targets.

``PermissionService.get_collection_permission`` / ``get_accessible_collection_ids`` delegate
to :func:`collection_permission_ranks`, so there is exactly one statement that decides a
caller's permission on a collection. :func:`org_collection_file_ids_stmt` is the file-read
route that statement gives: a member of a collection they may see can read the files in it
(the organization model of v422; before #1275 only the collection LIST honoured it).

Never join ``Collection`` to a file outside this module to show or filter by it: a raw join
names collections the caller cannot open.
"""

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement
from sqlalchemy import Select
from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import CollectionVisibility
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.core.tenancy import _Unscoped
from app.models.group import UserGroupMember
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.organization import OrganizationMembership
from app.models.sharing import CollectionShare
from app.services.permission_service import ORG_ADMIN_ROLE
from app.services.permission_service import PERMISSION_LEVELS
from app.services.permission_service import PERMISSION_NAMES
from app.services.permission_service import PermissionService
from app.services.permission_service import org_scope_pred

#: Search filter value for a collection the caller cannot see. No row has a negative id, so
#: the filter matches nothing: it neither widens to every file nor errors, which would let
#: the response tell an unseen collection's existence apart from an unknown one.
NO_VISIBLE_COLLECTION = -1


def _org_base_rank(user_id: int) -> Any:
    """SQL rank (0 none, 2 editor, 3 owner) a caller holds on a collection by being its tenant.

    Personal collection: its owner. Organization collection: a member of its org (the creator
    and org admins own it, every other member edits it, v422), except that a ``restricted``
    collection gives a plain member nothing (#1275). Explicit shares are NOT part of this rank;
    :func:`_share_rank` adds them.
    """
    role = (
        select(OrganizationMembership.role)
        .where(
            OrganizationMembership.organization_id == Collection.organization_id,
            OrganizationMembership.user_id == user_id,
        )
        .correlate(Collection)
        .scalar_subquery()
    )
    return case(
        (Collection.organization_id.is_(None), case((Collection.user_id == user_id, 3), else_=0)),
        (role.is_(None), 0),
        (role == ORG_ADMIN_ROLE, 3),
        (Collection.user_id == user_id, 3),
        # A restricted collection is not the organization's: a plain member holds nothing on
        # it by membership. Only a share (``_share_rank``) can give them access.
        (Collection.visibility == CollectionVisibility.RESTRICTED.value, 0),
        else_=2,
    )


def _share_rank(user_id: int) -> Any:
    """SQL rank (0 none, 1 viewer, 2 editor) of the caller's best share on a collection."""
    groups = select(UserGroupMember.group_id).where(UserGroupMember.user_id == user_id)
    best = (
        select(
            func.max(
                case(
                    (CollectionShare.permission == "editor", 2),
                    (CollectionShare.permission == "viewer", 1),
                    else_=0,
                )
            )
        )
        .where(
            CollectionShare.collection_id == Collection.id,
            or_(
                CollectionShare.target_user_id == user_id,
                CollectionShare.target_group_id.in_(groups),
            ),
        )
        .correlate(Collection)
        .scalar_subquery()
    )
    return func.coalesce(best, 0)


def collection_permission_ranks(db: Session, user_id: int) -> dict[int, str]:
    """Permission name by collection id, for every collection ``user_id`` may see (any tenant).

    The single statement behind ``PermissionService.get_collection_permission`` and
    ``get_accessible_collection_ids``. Callers apply their own tenant predicate.
    """
    rank = func.greatest(_org_base_rank(user_id), _share_rank(user_id))
    rows = db.execute(select(Collection.id, rank).where(rank > 0)).all()
    return {int(cid): PERMISSION_NAMES[int(r)] for cid, r in rows}


def collection_permission(db: Session, collection_id: int, user_id: int) -> str | None:
    """The caller's permission on one collection (``viewer`` | ``editor`` | ``owner``), or None."""
    rank = func.greatest(_org_base_rank(user_id), _share_rank(user_id))
    value = db.execute(select(rank).where(Collection.id == collection_id)).scalar()
    return PERMISSION_NAMES[int(value)] if value else None


def collection_is_hidden_from(db: Session, collection: Collection, user_id: int) -> bool:
    """True when ``collection`` is restricted and ``user_id`` holds no permission on it.

    The 404 rule for a collection addressed by uuid: an unrestricted collection is never
    "hidden" here (an out-of-tenant one is refused by the usual tenant gate), and a
    restricted one is hidden from everyone who is not its creator, an organization admin or
    a share target, including platform staff under a support-access grant.
    """
    if collection.visibility != CollectionVisibility.RESTRICTED.value:
        return False
    return collection_permission(db, collection.id, user_id) is None


def members_without_access(db: Session, collection: Collection) -> list[int]:
    """Ids of the collection's organization members who hold NO permission on it.

    Set-based (three reads however large the organization): members who are neither its
    creator, an organization admin, a direct share target nor in a share's group. For an
    unrestricted collection that is nobody, so it doubles as "who just lost sight of it"
    when a collection is restricted. Empty for a personal collection.
    """
    if (
        collection.organization_id is None
        or collection.visibility != CollectionVisibility.RESTRICTED.value
    ):
        return []
    rows = db.execute(
        select(OrganizationMembership.user_id, OrganizationMembership.role).where(
            OrganizationMembership.organization_id == collection.organization_id
        )
    ).all()
    shares = db.execute(
        select(CollectionShare.target_user_id, CollectionShare.target_group_id).where(
            CollectionShare.collection_id == collection.id
        )
    ).all()
    named = {int(u) for u, _g in shares if u is not None}
    group_ids = [int(g) for _u, g in shares if g is not None]
    if group_ids:
        named |= {
            int(uid)
            for uid in db.execute(
                select(UserGroupMember.user_id).where(UserGroupMember.group_id.in_(group_ids))
            ).scalars()
        }
    return [
        int(uid)
        for uid, role in rows
        if role != ORG_ADMIN_ROLE and uid != collection.user_id and int(uid) not in named
    ]


def org_collection_file_ids_stmt(user_id: int, organization_id: OrgScope) -> Select[Any]:
    """``SELECT media_file_id`` of files readable through an organization collection.

    A member of an organization collection they may see reads the organization's files in it
    (decision D-3 of the governance plan). ``organization_id`` is the usual default-deny tenant
    gate: an int keeps that org, ``None`` (personal) yields nothing, ``UNSCOPED`` spans every
    org the caller belongs to. Quarantine is the caller's concern, as for every file query.
    """
    stmt = (
        select(CollectionMember.media_file_id)
        .join(Collection, Collection.id == CollectionMember.collection_id)
        .join(
            MediaFile,
            and_(
                MediaFile.id == CollectionMember.media_file_id,
                MediaFile.organization_id == Collection.organization_id,
            ),
        )
        .where(Collection.organization_id.is_not(None), _org_base_rank(user_id) > 0)
    )
    gate = org_scope_pred(Collection.organization_id, organization_id)
    return stmt.where(gate) if gate is not None else stmt


def file_readable_via_org_collection(db: Session, file_id: int, user_id: int) -> bool:
    """True when ``file_id`` sits in an organization collection ``user_id`` may see."""
    stmt = org_collection_file_ids_stmt(user_id, UNSCOPED).where(
        CollectionMember.media_file_id == file_id
    )
    return db.execute(select(stmt.exists())).scalar() is True


def org_readable_collection_ids(db: Session, user_id: int, organization_id: int) -> list[int]:
    """Ids of ``organization_id``'s collections ``user_id`` reads files through (membership route).

    The search index arm: a chunk whose ``collection_ids`` names one of these is readable by the
    caller even when ``accessible_user_ids`` does not list them.
    """
    rows = db.execute(
        select(Collection.id).where(
            Collection.organization_id == organization_id, _org_base_rank(user_id) > 0
        )
    ).scalars()
    return [int(cid) for cid in rows]


def visible_collection_ids(db: Session, user_id: int, organization_id: OrgScope) -> set[int]:
    """Ids of the collections ``user_id`` may see in the tenant ``organization_id``.

    Visible means owned, a collection of one of the caller's organizations, or shared with
    the caller or one of their groups (:func:`collection_permission_ranks`).
    ``organization_id`` is the default-deny tenant gate of ``org_scope_pred``: an int keeps
    that organization's collections, ``None`` keeps personal ones, ``UNSCOPED`` applies none.
    """
    accessible = collection_permission_ranks(db, user_id)
    if not accessible:
        return set()
    stmt = select(Collection.id).where(Collection.id.in_(accessible))
    tenant_pred = org_scope_pred(Collection.organization_id, organization_id)
    if tenant_pred is not None:
        stmt = stmt.where(tenant_pred)
    return set(db.execute(stmt).scalars().all())


def visible_collections_by_uuid(
    db: Session, user_id: int, organization_id: OrgScope, collection_uuids: Iterable[str | UUID]
) -> list[Collection]:
    """The named collections the caller may see in the tenant; anything else is absent."""
    visible = visible_collection_ids(db, user_id, organization_id)
    if not visible:
        return []
    stmt = select(Collection).where(
        Collection.id.in_(visible), Collection.uuid.in_([str(c) for c in collection_uuids])
    )
    return list(db.execute(stmt.order_by(Collection.id)).scalars().all())


def usable_collections(
    db: Session,
    user_id: int,
    organization_id: int | None | ColumnElement[Any],
    *,
    min_permission: str = "editor",
    collection_uuids: Iterable[str | UUID] | None = None,
) -> list[Collection]:
    """The tenant's collections ``user_id`` holds at least ``min_permission`` on, by id.

    The one lookup for "put a file into a collection" and "match a suggested name against
    the collections this person can actually use": the tenant gate
    (``PermissionService.collection_tenant_pred``: an org's collections, or the caller's own
    personal ones) intersected with the caller's permission, in ONE statement. A collection
    the caller cannot see is simply absent, so a caller cannot tell it from one that does
    not exist. ``collection_uuids`` narrows to those collections. ``organization_id`` may be
    a scalar SQL expression (a file's own org column) so a batched caller keeps its
    statement count constant.
    """
    rank = func.greatest(_org_base_rank(user_id), _share_rank(user_id))
    if isinstance(organization_id, int) or organization_id is None:
        tenant = PermissionService.collection_tenant_pred(user_id, organization_id)
    else:
        tenant = or_(
            and_(
                organization_id.is_(None),
                PermissionService.collection_tenant_pred(user_id, None),
            ),
            Collection.organization_id == organization_id,
        )
    stmt = select(Collection).where(rank >= PERMISSION_LEVELS[min_permission], tenant)
    if collection_uuids is not None:
        stmt = stmt.where(Collection.uuid.in_([str(c) for c in collection_uuids]))
    return list(db.execute(stmt.order_by(Collection.id)).scalars().all())


def collections_on_files(
    db: Session,
    file_ids: Iterable[int],
    user_id: int,
    organization_id: OrgScope = UNSCOPED,
) -> dict[int, list[Collection]]:
    """The collections each file belongs to that the caller may see named on it, by file id.

    The membership rule is the file page's: the caller's own collections in the file's tenant
    (their personal collections for a personal file, the organization's collections for an
    organization file, v422), restricted to :func:`visible_collection_ids`. Batched for a
    whole result page: a constant number of queries however many files are passed (one more
    per distinct tenant when ``organization_id`` is ``UNSCOPED``). Collections are sorted by
    name; a file in no visible collection is absent from the result.
    """
    wanted = sorted({int(file_id) for file_id in file_ids})
    if not wanted:
        return {}

    files_by_tenant: dict[int | None, list[int]]
    if isinstance(organization_id, _Unscoped):
        files_by_tenant = {}
        tenant_rows = (
            db.query(MediaFile.id, MediaFile.organization_id).filter(MediaFile.id.in_(wanted)).all()
        )
        for file_id, file_org in tenant_rows:
            files_by_tenant.setdefault(file_org, []).append(file_id)
    else:
        files_by_tenant = {organization_id: wanted}

    visible = visible_collection_ids(db, user_id, organization_id)
    if not visible:
        return {}

    result: dict[int, list[Collection]] = {}
    for tenant, tenant_file_ids in files_by_tenant.items():
        rows = (
            db.query(CollectionMember.media_file_id, Collection)
            .join(Collection, Collection.id == CollectionMember.collection_id)
            .filter(
                CollectionMember.media_file_id.in_(tenant_file_ids),
                PermissionService.collection_tenant_pred(user_id, tenant),
            )
            .order_by(Collection.name, Collection.id)
            .all()
        )
        for file_id, collection in rows:
            if collection.id in visible:
                result.setdefault(file_id, []).append(collection)
    return result


def collection_member_file_uuids(db: Session, collection_id: int, limit: int) -> list[str] | None:
    """Uuids of the files in a collection, from Postgres, or ``None`` above ``limit``.

    The search collection filter matches on these rather than on the chunk documents'
    denormalized ``collection_ids``: that field is written only when a file is (re)indexed,
    so a file added to a collection afterwards (an AI auto-collection, a user's "add to
    collection") never matched its own collection's filter. ``collection_id`` is the
    already-resolved id from :func:`resolve_collection_filter`; the caller's access is
    enforced by the search's own ``accessible_user_ids`` term.
    """
    rows = (
        db.query(MediaFile.uuid)
        .join(CollectionMember, CollectionMember.media_file_id == MediaFile.id)
        .filter(CollectionMember.collection_id == collection_id)
        .limit(limit + 1)
        .all()
    )
    if len(rows) > limit:
        return None
    return [str(row[0]) for row in rows]


def resolve_collection_filter(
    db: Session,
    *,
    user_id: int,
    organization_id: OrgScope,
    collection_uuid: UUID | None = None,
    collection_id: int | None = None,
) -> int | None:
    """The collection id a search may filter by, or ``None`` when no filter was asked for.

    A collection the caller cannot see (or that does not exist) resolves to
    :data:`NO_VISIBLE_COLLECTION`, so the filter returns an empty page with 200 rather than
    a different answer for "exists but hidden" and "unknown".
    """
    if collection_uuid is None and collection_id is None:
        return None
    if collection_uuid is not None:
        resolved = db.query(Collection.id).filter(Collection.uuid == collection_uuid).scalar()
    else:
        resolved = collection_id
    if resolved is None or resolved not in visible_collection_ids(db, user_id, organization_id):
        return NO_VISIBLE_COLLECTION
    return int(resolved)
