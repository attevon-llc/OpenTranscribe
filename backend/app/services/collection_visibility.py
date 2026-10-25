"""Which collections a caller may see, for the surfaces that name a collection on a file.

The file page's collection list, the search result card and the search collection filter all
ask the same question, so it is answered here once, on top of ``PermissionService``. Issue
#1275 (restricted organization collections, ``docs/design/v060_org_content_governance_plan.md``
section 3.1) replaces the body of :func:`visible_collection_ids` with its visibility
predicate; every function below inherits the change without being edited.

Never join ``Collection`` to a file outside this module to show or filter by it: a raw join
names collections the caller cannot open, which is exactly what restricted collections must
hide.
"""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.core.tenancy import _Unscoped
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.services.permission_service import PermissionService
from app.services.permission_service import org_scope_pred

#: Search filter value for a collection the caller cannot see. No row has a negative id, so
#: the filter matches nothing: it neither widens to every file nor errors, which would let
#: the response tell an unseen collection's existence apart from an unknown one.
NO_VISIBLE_COLLECTION = -1


def visible_collection_ids(db: Session, user_id: int, organization_id: OrgScope) -> set[int]:
    """Ids of the collections ``user_id`` may see in the tenant ``organization_id``.

    Accessible means owned, a collection of one of the caller's organizations, or shared with
    the caller or one of their groups (``PermissionService.get_accessible_collection_ids``).
    ``organization_id`` is the default-deny tenant gate of ``org_scope_pred``: an int keeps
    that organization's collections, ``None`` keeps personal ones, ``UNSCOPED`` applies none.
    """
    accessible = {
        cid for cid, _perm in PermissionService.get_accessible_collection_ids(db, user_id)
    }
    if not accessible:
        return set()
    stmt = select(Collection.id).where(Collection.id.in_(accessible))
    tenant_pred = org_scope_pred(Collection.organization_id, organization_id)
    if tenant_pred is not None:
        stmt = stmt.where(tenant_pred)
    return set(db.execute(stmt).scalars().all())


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
