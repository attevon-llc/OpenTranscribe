"""API endpoints for media collections with sharing support.

Every route handler here is declared ``def``, not ``async def`` (issue #284 A2.5).
They do nothing but blocking work — synchronous SQLAlchemy queries plus a Redis
``PUBLISH`` for share notifications — and none of them ``await`` anything. Declared
``async def``, that work ran directly on the event loop, so one slow collection
query stalled every other request the process was serving. FastAPI dispatches a
plain ``def`` handler to Starlette's threadpool instead, which is where blocking
I/O belongs. Do not "modernise" these back to ``async def`` unless the body
genuinely gains an ``await``.
"""

import logging
from datetime import UTC
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy import and_
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Query as OrmQuery  # fastapi.Query is already imported
from sqlalchemy.orm import Session
from sqlalchemy.orm import defer
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import selectinload

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.files.crud import set_file_urls
from app.api.endpoints.files.filtering import apply_all_filters
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.core.constants import NOTIFICATION_TYPE_COLLECTION_SHARE_REVOKED
from app.core.constants import NOTIFICATION_TYPE_COLLECTION_SHARE_UPDATED
from app.core.constants import NOTIFICATION_TYPE_COLLECTION_SHARED
from app.db.base import get_db
from app.middleware.audit import get_request_context
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.organization import OrganizationMembership
from app.models.prompt import SummaryPrompt
from app.models.sharing import CollectionShare
from app.models.user import User
from app.schemas.media import Collection as CollectionSchema
from app.schemas.media import CollectionCreate
from app.schemas.media import CollectionMemberAdd
from app.schemas.media import CollectionMemberRemove
from app.schemas.media import CollectionOnSelection
from app.schemas.media import CollectionResponse
from app.schemas.media import CollectionUpdate
from app.schemas.media import CollectionWithCount
from app.schemas.media import PaginatedMediaFileResponse
from app.schemas.sharing import Share
from app.schemas.sharing import ShareCreate
from app.schemas.sharing import SharedCollectionInfo
from app.schemas.sharing import ShareUpdate
from app.schemas.user import UserBrief
from app.services.formatting_service import FormattingService
from app.services.permission_service import PermissionService
from app.services.takedown_service import exclude_quarantined
from app.tasks.search_indexing_task import update_file_access_index
from app.utils.uuid_helpers import get_by_uuid
from app.utils.uuid_helpers import get_collection_by_uuid_with_permission
from app.utils.uuid_helpers import get_collection_by_uuid_with_sharing
from app.utils.uuid_helpers import require_resource_owner
from app.utils.uuid_helpers import validate_uuids
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)

router = APIRouter()

#: Substituted for a NULL ``created_at`` on a share or a collection.
#:
#: ``collection_share.created_at`` and ``collection.created_at`` are both ``nullable`` in
#: Postgres — a ``server_default`` only fills a column an INSERT omits, so a raw-SQL insert
#: naming it, a backfill or an explicit ``UPDATE ... SET created_at = NULL`` leaves NULL.
#: ``Share.created_at`` and ``SharedCollectionInfo.shared_at`` are required ``datetime``
#: fields, so a NULL must become some timestamp or Pydantic fails the whole response.
#: The epoch, not ``datetime.now(UTC)``: "now" would claim the share was just created and
#: would sort a timestamp-less share to the top of the shared-with-me list.
UNKNOWN_TIMESTAMP = datetime(1970, 1, 1, tzinfo=UTC)


def _visible_media_counts(
    db: Session, collection_ids: list[int], *, include_quarantined: bool
) -> dict[int, int]:
    """Per-collection member counts, hiding quarantined files from non-admins.

    Abuse/DMCA (issue #262g): a taken-down file is invisible on every read
    surface for normal users — the member COUNT must agree with the member
    LIST or the mismatch leaks the takedown. Admins keep the true count.
    Collections absent from the result have zero visible members (callers use
    ``.get(cid, 0)``).
    """
    if not collection_ids:
        return {}
    query: OrmQuery = (
        db.query(CollectionMember.collection_id, func.count(CollectionMember.id))
        .join(MediaFile, MediaFile.id == CollectionMember.media_file_id)
        .filter(CollectionMember.collection_id.in_(collection_ids))
    )
    query = exclude_quarantined(query, include_quarantined=include_quarantined)
    return {cid: cnt for cid, cnt in query.group_by(CollectionMember.collection_id).all()}


def _get_share_target_user_ids(
    db: Session, share: CollectionShare, collection: Collection
) -> list[int]:
    """Return the user IDs affected by a share.

    For user-targeted shares this is a single-element list.
    For group-targeted shares this is all group members.

    For an organization collection, only members of that organization are
    returned: a group can outlive (or predate) a member's time in the
    organization, and such a user must not be told about the org's collection.
    """
    if share.target_type == "user" and share.target_user_id:
        query = db.query(User.id).filter(User.id == share.target_user_id)
    elif share.target_type == "group" and share.target_group_id:
        query = db.query(UserGroupMember.user_id).filter(
            UserGroupMember.group_id == share.target_group_id
        )
    else:
        return []
    if collection.organization_id is not None:
        user_col = User.id if share.target_type == "user" else UserGroupMember.user_id
        query = query.filter(
            exists().where(
                OrganizationMembership.user_id == user_col,
                OrganizationMembership.organization_id == collection.organization_id,
            )
        )
    return [int(row[0]) for row in query.all()]


def _notify_share_event(
    db: Session,
    share: CollectionShare,
    collection: Collection,
    notification_type: str,
    extra_data: dict | None = None,
) -> None:
    """Send a WebSocket notification for a sharing event to all affected users."""
    target_ids = _get_share_target_user_ids(db, share, collection)
    data: dict = {
        "collection_uuid": str(collection.uuid),
        "collection_name": collection.name,
        "share_uuid": str(share.uuid),
        "permission": share.permission,
        "message": _share_message(notification_type, collection.name),
    }
    if extra_data:
        data.update(extra_data)

    for uid in target_ids:
        send_ws_event(uid, notification_type, data)


def _share_message(notification_type: str, collection_name: str) -> str:
    """Build a human-readable message for a share notification."""
    if notification_type == NOTIFICATION_TYPE_COLLECTION_SHARED:
        return f"Collection '{collection_name}' has been shared with you"
    if notification_type == NOTIFICATION_TYPE_COLLECTION_SHARE_REVOKED:
        return f"Your access to collection '{collection_name}' has been revoked"
    if notification_type == NOTIFICATION_TYPE_COLLECTION_SHARE_UPDATED:
        return f"Your permissions on collection '{collection_name}' have been updated"
    return "Collection sharing update"


def _resolve_prompt_uuid(db: Session, prompt_uuid: str | None, user_id: int) -> int | None:
    """Resolve a prompt UUID to its internal ID, validating access."""
    if prompt_uuid is None:
        return None

    prompt = (
        db.query(SummaryPrompt)
        .filter(
            SummaryPrompt.uuid == prompt_uuid,
            SummaryPrompt.is_active,
            or_(SummaryPrompt.is_system_default, SummaryPrompt.user_id == user_id),
        )
        .first()
    )

    if not prompt:
        raise HTTPException(
            status_code=404,
            detail="Summary prompt not found or not accessible",
        )

    return prompt.id


def _get_prompt_info(collection: Collection) -> tuple:
    """Get prompt UUID and name from a collection's default_summary_prompt relationship."""
    prompt = collection.default_summary_prompt
    if prompt:
        return (prompt.uuid, prompt.name)
    return (None, None)


def _build_share_response(db: Session, share: CollectionShare) -> Share:
    """Build a Share response from a CollectionShare record."""
    shared_by_brief = UserBrief(
        uuid=share.shared_by.uuid,
        full_name=share.shared_by.full_name,
        email=share.shared_by.email,
    )

    if share.target_type == "user" and share.target_user:
        target_uuid = share.target_user.uuid
        target_name = share.target_user.full_name or share.target_user.email
        target_email = share.target_user.email
        member_count = None
    elif share.target_type == "group" and share.target_group:
        target_uuid = share.target_group.uuid
        target_name = share.target_group.name
        target_email = None
        member_count = (
            db.query(UserGroupMember)
            .filter(UserGroupMember.group_id == share.target_group.id)
            .count()
        )
    else:
        raise HTTPException(status_code=500, detail="Invalid share target")

    return Share(
        uuid=share.uuid,
        target_type=share.target_type,
        target_uuid=target_uuid,
        target_name=target_name,
        target_email=target_email,
        member_count=member_count,
        permission=share.permission,
        shared_by=shared_by_brief,
        created_at=share.created_at or UNKNOWN_TIMESTAMP,
    )


# ============================================================================
# Static routes FIRST (before parameterized routes)
# ============================================================================


def _tenant_pred(ctx: RequestContext):
    """Collections that live in the request's tenant (org context: that org; else personal).

    Community edition: ctx.org_id is None and rows are org-less, so this is a no-op.
    """
    if ctx.org_id is not None:
        return Collection.organization_id == ctx.org_id
    return Collection.organization_id.is_(None)


def _explicitly_shared_entries(
    db: Session, user_id: int, ctx: RequestContext
) -> list[tuple[int, str]]:
    """(collection id, permission) for collections shared with the caller in this tenant.

    Excludes the tenant's own collections — the caller's personal ones, or in an
    organization every collection of the org (v422: those are the org's, reached
    by membership, and are listed as the caller's own).
    """
    accessible = PermissionService.get_accessible_collection_ids(db, user_id)
    candidate = {cid: perm for cid, perm in accessible if perm != "owner"}
    if not candidate:
        return []
    own = {
        cid
        for (cid,) in db.query(Collection.id).filter(
            Collection.id.in_(list(candidate)),
            PermissionService.collection_tenant_pred(user_id, ctx.org_id),
        )
    }
    in_tenant = {
        cid
        for (cid,) in db.query(Collection.id).filter(
            Collection.id.in_(list(candidate)), _tenant_pred(ctx)
        )
    }
    return [(cid, perm) for cid, perm in candidate.items() if cid in in_tenant and cid not in own]


@router.get("/shared-with-me", response_model=list[SharedCollectionInfo])
def list_shared_collections(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """List collections shared with the current user (not owned by them).

    Tenant-gated like ``GET /collections``: only collections of the active tenant,
    and in an organization not the org's own collections (every member already
    has those).
    """
    shared_ids = _explicitly_shared_entries(db, current_user.id, ctx)

    if not shared_ids:
        return []

    collection_ids = [cid for cid, _ in shared_ids]
    perm_map = {cid: perm for cid, perm in shared_ids}

    collections = (
        db.query(Collection)
        .options(joinedload(Collection.user))
        .filter(Collection.id.in_(collection_ids))
        .all()
    )

    # Filter out owned collections
    collections = [c for c in collections if c.user_id != current_user.id]
    if not collections:
        return []
    filtered_ids = [c.id for c in collections]

    # Batch: media counts per collection (quarantined files hidden for non-admins)
    media_counts = _visible_media_counts(
        db, filtered_ids, include_quarantined=bool(current_user.is_admin)
    )

    # Batch: share records for shared_by info
    user_group_ids = (
        db.query(UserGroupMember.group_id)
        .filter(UserGroupMember.user_id == current_user.id)
        .subquery()
    )
    shares = (
        db.query(CollectionShare)
        .options(joinedload(CollectionShare.shared_by))
        .filter(
            CollectionShare.collection_id.in_(filtered_ids),
            or_(
                CollectionShare.target_user_id == current_user.id,
                CollectionShare.target_group_id.in_(db.query(user_group_ids.c.group_id)),
            ),
        )
        .all()
    )
    # First matching share per collection
    share_map: dict[int, CollectionShare] = {}
    for share in shares:
        if share.collection_id not in share_map:
            share_map[share.collection_id] = share

    results = []
    for coll in collections:
        coll_share = share_map.get(coll.id)
        # Both source columns are nullable, so this falls through to the sentinel rather
        # than asserting on a premise a server_default does not establish.
        shared_at = (coll_share.created_at if coll_share else coll.created_at) or UNKNOWN_TIMESTAMP
        sharer = coll_share.shared_by if coll_share and coll_share.shared_by else coll.user
        if sharer is None:
            # An unattributed org collection with no share row has nobody to credit
            # (unreachable today: the tenant gate above keeps org collections out).
            continue
        shared_by_brief = UserBrief(
            uuid=sharer.uuid,
            full_name=sharer.full_name,
            email=sharer.email,
        )

        results.append(
            SharedCollectionInfo(
                uuid=coll.uuid,
                name=coll.name,
                description=coll.description,
                media_count=media_counts.get(coll.id, 0),
                my_permission=perm_map.get(coll.id, "viewer"),
                shared_by=shared_by_brief,
                shared_at=shared_at,
            )
        )

    return results[skip : skip + limit]


@router.get("/for-files", response_model=list[CollectionOnSelection])
def list_collections_on_files(
    file_uuids: list[UUID] = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Which collections a set of selected files already belongs to.

    The mirror of ``GET /tags/for-files``, so the gallery's two organizing
    modals answer the same question the same way — `file_count` against
    `selection_size` distinguishes a collection holding every selected file from
    one holding a few.

    Scoped to the caller's own collections in the request's tenant — in an
    organization, the org's collections (v422); in the personal workspace, their
    personal ones. Membership of anyone else's collection is not theirs to read
    off a selection.

    Registered before ``/{collection_uuid}`` so the literal path is not
    swallowed.
    """
    from app.models.media import CollectionMember
    from app.models.media import MediaFile

    file_ids = [
        get_by_uuid(db, MediaFile, file_uuid, error_message="File not found").id
        for file_uuid in file_uuids
    ]
    selection_size = len(set(file_ids))
    rows = (
        db.query(Collection, func.count(func.distinct(CollectionMember.media_file_id)))
        .join(CollectionMember, CollectionMember.collection_id == Collection.id)
        .filter(
            CollectionMember.media_file_id.in_(file_ids),
            PermissionService.collection_tenant_pred(current_user.id, ctx.org_id),
        )
        .group_by(Collection.id)
        .order_by(func.count(func.distinct(CollectionMember.media_file_id)).desc(), Collection.name)
        .all()
    )
    return [
        CollectionOnSelection(
            uuid=collection.uuid,
            name=collection.name,
            file_count=int(count),
            selection_size=selection_size,
        )
        for collection, count in rows
    ]


@router.get("", response_model=list[CollectionWithCount])
def list_collections(
    ownership: str = Query(
        "mine",
        pattern="^(mine|shared|all)$",
        description="Filter: 'mine' (owned), 'shared' (shared with me), 'all' (both)",
    ),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Get collections for the current user with media count.

    Use ownership param to filter:
    - 'mine': the tenant's own collections (default) — the caller's personal
      collections in the personal workspace; in an organization, every
      collection of the org (they are shared by the org, v422), with
      ``my_permission`` ``owner`` for the creator/org admins and ``editor`` for
      other members
    - 'shared': Only collections explicitly shared with current user
    - 'all': Both
    """
    user_id = current_user.id

    # Tenant gate (mirrors the gallery list): org context sees only same-org
    # collections; personal scope sees only org-less collections. Community
    # edition: ctx.org_id is None and rows are org-less, so this is a no-op.
    org_pred = _tenant_pred(ctx)

    # Member counts hide quarantined files for non-admins (issue #262g).
    include_quarantined = bool(current_user.is_admin)

    if ownership == "mine":
        accessible_perms = dict(PermissionService.get_accessible_collection_ids(db, user_id))
        collection_ids = [
            row[0]
            for row in db.query(Collection.id)
            .filter(PermissionService.collection_tenant_pred(user_id, ctx.org_id), org_pred)
            .order_by(Collection.id)
            .offset(skip)
            .limit(limit)
            .all()
            if row[0] in accessible_perms
        ]
        counts_dict = _visible_media_counts(
            db, collection_ids, include_quarantined=include_quarantined
        )
        perm_dict: dict[int, str] = {cid: accessible_perms[cid] for cid in collection_ids}
        shared_by_dict: dict[int, UserBrief | None] = {}

    elif ownership == "shared":
        # Only explicitly shared collections of this tenant (not the tenant's own)
        shared_entries = _explicitly_shared_entries(db, user_id, ctx)

        collection_ids = [cid for cid, _ in shared_entries]
        perm_dict = {cid: perm for cid, perm in shared_entries}
        counts_dict = {}
        shared_by_dict = {}

        if collection_ids:
            collection_ids = [
                row[0]
                for row in db.query(Collection.id)
                .filter(Collection.id.in_(collection_ids), org_pred)
                .offset(skip)
                .limit(limit)
                .all()
            ]
            counts_dict = _visible_media_counts(
                db, collection_ids, include_quarantined=include_quarantined
            )

            # Get shared_by info for each shared collection
            _populate_shared_by(db, collection_ids, user_id, shared_by_dict)

    else:
        # All collections: owned + shared
        accessible = PermissionService.get_accessible_collection_ids(db, user_id)
        perm_dict = {cid: perm for cid, perm in accessible}
        all_ids = list(perm_dict.keys())
        shared_by_dict = {}

        if all_ids:
            collection_ids = [
                row[0]
                for row in db.query(Collection.id)
                .filter(Collection.id.in_(all_ids), org_pred)
                .offset(skip)
                .limit(limit)
                .all()
            ]
            counts_dict = _visible_media_counts(
                db, collection_ids, include_quarantined=include_quarantined
            )

            # Get shared_by info for non-owned collections
            non_owned = [cid for cid in collection_ids if perm_dict.get(cid) != "owner"]
            if non_owned:
                _populate_shared_by(db, non_owned, user_id, shared_by_dict)
        else:
            collection_ids = []
            counts_dict = {}

    if not collection_ids:
        return []

    # Fetch full collection objects with user and prompt relationships
    # (org_pred re-applied as defense-in-depth against un-gated id sources)
    collections_objs = (
        db.query(Collection)
        .options(
            joinedload(Collection.user),
            joinedload(Collection.default_summary_prompt),
        )
        .filter(Collection.id.in_(collection_ids), org_pred)
        .all()
    )

    # Bulk-fetch share counts for all collections
    share_counts_query = (
        db.query(
            CollectionShare.collection_id,
            func.count(CollectionShare.id).label("share_count"),
        )
        .filter(CollectionShare.collection_id.in_(collection_ids))
        .group_by(CollectionShare.collection_id)
    ).all()
    share_counts_dict = {row[0]: row[1] for row in share_counts_query}

    collections = []
    for collection in collections_objs:
        collection_with_count = CollectionWithCount.model_validate(collection)
        collection_with_count.media_count = counts_dict.get(collection.id, 0)
        prompt_uuid, prompt_name = _get_prompt_info(collection)
        collection_with_count.default_prompt_id = prompt_uuid
        collection_with_count.default_prompt_name = prompt_name

        # Set sharing metadata
        my_perm = perm_dict.get(collection.id, "owner")
        collection_with_count.my_permission = my_perm
        collection_with_count.is_shared = collection.user_id != current_user.id
        collection_with_count.share_count = share_counts_dict.get(collection.id, 0)
        if collection.id in shared_by_dict:
            collection_with_count.shared_by = shared_by_dict[collection.id]

        collections.append(collection_with_count)

    return collections


def _populate_shared_by(
    db: Session,
    collection_ids: list[int],
    user_id: int,
    shared_by_dict: dict,
) -> None:
    """Populate shared_by UserBrief for a list of shared collection IDs."""
    user_group_ids_sq = (
        db.query(UserGroupMember.group_id).filter(UserGroupMember.user_id == user_id).subquery()
    )
    shares = (
        db.query(CollectionShare)
        .options(joinedload(CollectionShare.shared_by))
        .filter(
            CollectionShare.collection_id.in_(collection_ids),
            or_(
                CollectionShare.target_user_id == user_id,
                CollectionShare.target_group_id.in_(db.query(user_group_ids_sq.c.group_id)),
            ),
        )
        .all()
    )
    for share in shares:
        if share.collection_id not in shared_by_dict and share.shared_by:
            shared_by_dict[share.collection_id] = UserBrief(
                uuid=share.shared_by.uuid,
                full_name=share.shared_by.full_name,
                email=share.shared_by.email,
            )


@router.post("", response_model=CollectionSchema)
def create_collection(
    collection: CollectionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Create a collection in the request's tenant.

    Consumed by the gallery's "New collection" dialog and by scripts organising an
    imported library. Any active user; the collection is stamped with
    ``current_user.id`` (its creator) and the request's tenant (``ctx.org_id``,
    issue #1051) — in an organization it is the org's collection, shared by every
    member; in the personal workspace it is the caller's. There is no way to
    create one for someone else.

    Names are unique **per tenant**, not globally — a duplicate among the caller's
    personal collections, or among the organization's, is 400, while another
    tenant may hold the same name. ``default_prompt_id`` arrives as a
    prompt *uuid* and is resolved to the internal id by ``_resolve_prompt_uuid``,
    which accepts only an active prompt the caller owns or a system default and 404s
    otherwise; the response carries the uuid back, never the internal id. A new
    collection starts with no members and no shares.
    """
    # Check if a collection with the same name exists in this tenant
    existing = (
        db.query(Collection.id)
        .filter(
            PermissionService.collection_tenant_pred(current_user.id, ctx.org_id),
            Collection.name == collection.name,
        )
        .first()
    )

    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Collection with name '{collection.name}' already exists",
        )

    # Resolve prompt UUID to internal ID if provided
    create_data = collection.dict(exclude={"default_prompt_id"})
    prompt_internal_id = None
    if collection.default_prompt_id:
        prompt_internal_id = _resolve_prompt_uuid(
            db, str(collection.default_prompt_id), current_user.id
        )

    db_collection = Collection(
        **create_data,
        user_id=current_user.id,
        organization_id=ctx.org_id,
        default_summary_prompt_id=prompt_internal_id,
    )
    db.add(db_collection)
    db.commit()
    db.refresh(db_collection)

    # Eagerly load prompt relationship for response
    if db_collection.default_summary_prompt_id:
        db.refresh(db_collection, ["default_summary_prompt"])

    result = CollectionSchema.model_validate(db_collection)
    prompt_uuid, prompt_name = _get_prompt_info(db_collection)
    result.default_prompt_id = prompt_uuid
    result.default_prompt_name = prompt_name
    return result


# ============================================================================
# Parameterized routes (/{collection_uuid}/...)
# ============================================================================


@router.get("/{collection_uuid}", response_model=CollectionResponse)
def get_collection(
    collection_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Get a specific collection with its media files.

    Uses PermissionService: any user with viewer+ permission can access.
    Tenant-gated via ctx.org_id (issue #262d) — out-of-scope collections 403.
    """
    collection = get_collection_by_uuid_with_permission(
        db, collection_uuid, current_user.id, organization_id=ctx.org_id
    )

    # Reload with joined data
    reloaded_collection = (
        db.query(Collection)
        .filter(Collection.id == collection.id)
        .options(
            joinedload(Collection.collection_members).joinedload(CollectionMember.media_file),
            joinedload(Collection.default_summary_prompt),
        )
        .first()
    )

    if reloaded_collection is None:
        raise HTTPException(status_code=404, detail="Collection not found")

    collection = reloaded_collection

    # Extract media files from collection members. Abuse/DMCA: quarantined
    # files are hidden from every read surface for non-admins — collection
    # detail included (matches the gallery, search, and per-file 404 gate).
    from app.services.takedown_service import is_hidden_for

    is_admin = bool(getattr(current_user, "is_admin", False))
    media_files = [
        member.media_file
        for member in collection.collection_members
        if not is_hidden_for(member.media_file, is_admin=is_admin)
    ]

    # Build response with prompt info
    result = CollectionResponse.model_validate(collection)
    # ORM MediaFile list coerced to schema via response_model serialization
    result.media_files = media_files  # type: ignore[assignment]
    prompt_uuid, prompt_name = _get_prompt_info(collection)
    result.default_prompt_id = prompt_uuid
    result.default_prompt_name = prompt_name

    return result


@router.put("/{collection_uuid}", response_model=CollectionSchema)
def update_collection(
    collection_uuid: str,
    collection_update: CollectionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Update a collection. Requires editor+ permission. Tenant-gated (#262d)."""
    collection, permission = get_collection_by_uuid_with_sharing(
        db, collection_uuid, current_user.id, min_permission="editor", organization_id=ctx.org_id
    )
    collection_id = collection.id

    # Check if new name conflicts with another collection of the same tenant
    if collection_update.name and collection_update.name != collection.name:
        if collection.organization_id is not None:
            same_tenant = Collection.organization_id == collection.organization_id
        else:
            same_tenant = and_(
                Collection.user_id == collection.user_id, Collection.organization_id.is_(None)
            )
        existing = (
            db.query(Collection.id)
            .filter(
                same_tenant,
                Collection.name == collection_update.name,
                Collection.id != collection_id,
            )
            .first()
        )

        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Collection with name '{collection_update.name}' already exists",
            )

    # Update fields
    update_data = collection_update.dict(exclude_unset=True)

    # Handle prompt UUID resolution separately
    if "default_prompt_id" in update_data:
        prompt_uuid = update_data.pop("default_prompt_id")
        if prompt_uuid is None:
            # Explicitly clearing the prompt
            collection.default_summary_prompt_id = None  # type: ignore[assignment]
        else:
            prompt_internal_id = _resolve_prompt_uuid(db, str(prompt_uuid), current_user.id)
            collection.default_summary_prompt_id = prompt_internal_id  # type: ignore[assignment]

    for field, value in update_data.items():
        setattr(collection, field, value)

    db.commit()
    db.refresh(collection)

    # Eagerly load prompt relationship for response
    if collection.default_summary_prompt_id:
        db.refresh(collection, ["default_summary_prompt"])

    result = CollectionSchema.model_validate(collection)
    prompt_uuid_val, prompt_name = _get_prompt_info(collection)
    result.default_prompt_id = prompt_uuid_val
    result.default_prompt_name = prompt_name
    return result


@router.delete("/{collection_uuid}")
def delete_collection(
    collection_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Delete a collection. Tenant-gated (#262d).

    Personal collection: only its owner. Organization collection (v422): its
    creator or an org admin — other members are editors.
    """
    collection, permission = get_collection_by_uuid_with_sharing(
        db, collection_uuid, current_user.id, min_permission="owner", organization_id=ctx.org_id
    )

    # Only the original owner can delete a personal collection (a stranger is
    # already rejected by the min_permission="owner" sharing helper above, since a
    # share never grants "owner"). An org collection's owners are resolved by that
    # helper from the org membership.
    if collection.organization_id is None:
        require_resource_owner(
            collection, current_user, forbidden_detail="Only the collection owner can delete it"
        )

    # Reindex files BEFORE deletion (cascade will remove shares + members)
    file_ids = [
        cm.media_file_id
        for cm in db.query(CollectionMember.media_file_id)
        .filter(CollectionMember.collection_id == collection.id)
        .all()
    ]
    if file_ids:
        update_file_access_index.delay(file_ids)

    db.delete(collection)
    db.commit()

    return {"message": "Collection deleted successfully"}


@router.post("/{collection_uuid}/media", response_model=dict)
def add_media_to_collection(
    collection_uuid: str,
    media_data: CollectionMemberAdd,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Add media files to a collection. Requires editor+ permission. Tenant-gated (#262d)."""
    collection, permission = get_collection_by_uuid_with_sharing(
        db, collection_uuid, current_user.id, min_permission="editor", organization_id=ctx.org_id
    )
    collection_id = collection.id

    # Bulk resolve UUIDs to IDs in a single query (avoids N+1)
    media_file_uuids = validate_uuids([str(uuid) for uuid in media_data.media_file_ids])

    # Files must be in the collection's tenant — without the org predicate a
    # member could pull another scope's file into this collection, exposing it via
    # the collection detail (cross-scope leak). In the personal workspace they must
    # also be the caller's own; in an organization every org file is already
    # visible to every member (scope_to_context), so any of them may go into the
    # org's collection (v422). Community invariance: ctx.org_id is None and rows
    # are org-less, so this is the caller's-own-files rule.
    if ctx.org_id is not None:
        file_pred = MediaFile.organization_id == ctx.org_id
    else:
        file_pred = and_(MediaFile.user_id == current_user.id, MediaFile.organization_id.is_(None))

    media_files = db.query(MediaFile).filter(MediaFile.uuid.in_(media_file_uuids), file_pred).all()

    if len(media_files) != len(media_file_uuids):
        # Determine which UUIDs are missing or unauthorized
        found_uuids = {str(f.uuid) for f in media_files}
        missing = [u for u in media_file_uuids if u not in found_uuids]
        raise HTTPException(
            status_code=404,
            detail=f"Media files not found or not authorized: {missing}",
        )

    media_file_ids = [f.id for f in media_files]

    # Get existing members to avoid duplicates
    existing_members = (
        db.query(CollectionMember.media_file_id)
        .filter(
            CollectionMember.collection_id == collection_id,
            CollectionMember.media_file_id.in_(media_file_ids),
        )
        .all()
    )

    existing_ids = {member[0] for member in existing_members}
    new_ids = set(media_file_ids) - existing_ids

    # Add new members
    added_count = 0
    added_file_ids = []
    for media_file_id in new_ids:
        member = CollectionMember(collection_id=collection_id, media_file_id=media_file_id)
        db.add(member)
        added_file_ids.append(media_file_id)
        added_count += 1

    db.commit()

    # If collection has shares, reindex newly added files
    if added_file_ids:
        share_count = (
            db.query(CollectionShare).filter(CollectionShare.collection_id == collection_id).count()
        )
        if share_count > 0:
            update_file_access_index.delay(added_file_ids)

    return {
        "message": f"Added {added_count} media files to collection",
        "added": added_count,
        "already_existed": len(existing_ids),
    }


@router.delete("/{collection_uuid}/media", response_model=dict)
def remove_media_from_collection(
    collection_uuid: str,
    media_data: CollectionMemberRemove,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Remove media files from a collection. Requires editor+ permission. Tenant-gated (#262d)."""
    collection, permission = get_collection_by_uuid_with_sharing(
        db, collection_uuid, current_user.id, min_permission="editor", organization_id=ctx.org_id
    )
    collection_id = collection.id

    # Bulk resolve UUIDs to IDs in a single query (avoids N+1)
    media_file_uuids = validate_uuids([str(uuid) for uuid in media_data.media_file_ids])

    # Collection owner can remove any file; shared editors can only remove their own.
    # Every member of an org collection may remove any of its (org) files — the
    # collection is the org's (v422).
    may_remove_any = permission == "owner" or collection.organization_id is not None
    id_query = db.query(MediaFile.id).filter(MediaFile.uuid.in_(media_file_uuids))
    if not may_remove_any:
        id_query = id_query.filter(MediaFile.user_id == current_user.id)
    media_file_ids = [r[0] for r in id_query.all()]

    # Remove members
    removed_count = (
        db.query(CollectionMember)
        .filter(
            CollectionMember.collection_id == collection_id,
            CollectionMember.media_file_id.in_(media_file_ids),
        )
        .delete(synchronize_session=False)
    )

    db.commit()

    # If collection has shares, reindex removed files
    if media_file_ids:
        share_count = (
            db.query(CollectionShare).filter(CollectionShare.collection_id == collection_id).count()
        )
        if share_count > 0:
            update_file_access_index.delay(media_file_ids)

    return {
        "message": f"Removed {removed_count} media files from collection",
        "removed": removed_count,
    }


@router.get("/{collection_uuid}/media", response_model=PaginatedMediaFileResponse)
def get_collection_media(
    collection_uuid: str,
    # Pagination parameters
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    # Filters
    search: str | None = None,
    tag: list[str] | None = Query(None),
    speaker: list[str] | None = Query(None),
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    min_duration: float | None = None,
    max_duration: float | None = None,
    min_file_size: int | None = None,
    max_file_size: int | None = None,
    file_type: list[str] | None = Query(None),
    status: list[str] | None = Query(None),
    transcript_search: str | None = None,
    # Sort parameters
    sort_by: str = Query(
        "upload_time",
        description="Field to sort by: upload_time, completed_at, filename, duration, file_size",
    ),
    sort_order: str = Query("desc", description="Sort order: asc or desc"),
    # Dependencies
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Get media files in a collection with filtering, sorting, and pagination."""
    # Verify collection exists and user has access (tenant-gated, #262d)
    collection = get_collection_by_uuid_with_permission(
        db, collection_uuid, current_user.id, organization_id=ctx.org_id
    )
    collection_id = collection.id

    # Eager-loading strategy matching the main list endpoint
    list_options = [
        joinedload(MediaFile.user),
        selectinload(MediaFile.speakers).load_only(
            Speaker.uuid,  # type: ignore[arg-type]
            Speaker.name,  # type: ignore[arg-type]
            Speaker.display_name,  # type: ignore[arg-type]
        ),
        defer(MediaFile.metadata_raw),  # type: ignore[arg-type]
        defer(MediaFile.waveform_data),  # type: ignore[arg-type]
    ]

    # Build base query scoped to this collection
    base_query = (
        db.query(MediaFile)
        .options(*list_options)
        .join(CollectionMember, CollectionMember.media_file_id == MediaFile.id)
        .filter(CollectionMember.collection_id == collection_id)
    )

    # The owner of a PERSONAL collection sees only their own files in it; for
    # shared collections, and for an org's collections (v422: all org files are
    # visible to every member), show all files in the collection.
    own_files_only = (
        not current_user.is_admin
        and collection.organization_id is None
        and collection.user_id == current_user.id
    )
    if own_files_only:
        base_query = base_query.filter(MediaFile.user_id == current_user.id)

    # Abuse/DMCA: quarantined files are hidden from every read surface for
    # non-admins — the paginated collection-media list included (matches the
    # collection detail's is_hidden_for gate and the visible member counts).
    base_query = exclude_quarantined(base_query, include_quarantined=bool(current_user.is_admin))

    # Prepare filters dictionary
    filters = {
        "search": search,
        "tag": tag,
        "speaker": speaker,
        "from_date": from_date,
        "to_date": to_date,
        "min_duration": min_duration,
        "max_duration": max_duration,
        "min_file_size": min_file_size,
        "max_file_size": max_file_size,
        "file_type": file_type,
        "status": status,
        "transcript_search": transcript_search,
        "user_id": current_user.id if own_files_only else None,
        # The collection was resolved in this scope, and a file joins a collection only
        # from the same scope, so gating the transcript index by it drops nothing.
        "organization_id": ctx.org_id,
    }

    # Apply all filters
    filtered_query = apply_all_filters(base_query, filters)

    # Sorting field mapping
    sort_field_mapping = {
        "upload_time": MediaFile.upload_time,
        "completed_at": MediaFile.completed_at,
        "filename": MediaFile.filename,
        "duration": MediaFile.duration,
        "file_size": MediaFile.file_size,
    }
    sort_field = sort_field_mapping.get(sort_by, MediaFile.upload_time)

    # Get total count before sorting/pagination
    total_count = (filtered_query.with_entities(func.count(MediaFile.id)).scalar()) or 0

    # Apply sort order
    if sort_order.lower() == "asc":
        filtered_query = filtered_query.order_by(sort_field.asc())  # type: ignore[attr-defined]
    else:
        filtered_query = filtered_query.order_by(sort_field.desc())  # type: ignore[attr-defined]

    # Apply pagination
    offset = (page - 1) * page_size
    result = filtered_query.offset(offset).limit(page_size).all()

    # Format each file with URLs and formatted fields
    formatted_files = []
    for file in result:
        set_file_urls(file)
        formatted_file = FormattingService.format_media_file(file, file.speakers)
        formatted_files.append(formatted_file)

    # Calculate pagination metadata
    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 0
    has_more = page < total_pages

    return PaginatedMediaFileResponse(
        items=formatted_files,
        total=total_count,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        has_more=has_more,
    )


# ============================================================================
# Collection sharing endpoints
# ============================================================================


def _require_collection_owner(db: Session, collection: Collection, user_id: int) -> None:
    """Require that the user owns the collection.

    Only an owner may manage shares: the owner of a personal collection, or the
    creator / an org admin of an organization collection (v422). Users who
    received "editor" permission via a share — or as an ordinary org member —
    cannot re-share.
    """
    if PermissionService.get_collection_permission(db, collection.id, user_id) != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the collection owner can manage sharing",
        )


@router.get("/{collection_uuid}/shares", response_model=list[Share])
def list_collection_shares(
    collection_uuid: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """List all shares on a collection. Requires direct collection ownership. Tenant-gated (#262d)."""
    collection = get_collection_by_uuid_with_permission(
        db, collection_uuid, current_user.id, organization_id=ctx.org_id
    )
    _require_collection_owner(db, collection, current_user.id)

    shares = (
        db.query(CollectionShare)
        .options(
            joinedload(CollectionShare.shared_by),
            joinedload(CollectionShare.target_user),
            joinedload(CollectionShare.target_group),
        )
        .filter(CollectionShare.collection_id == collection.id)
        .order_by(CollectionShare.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    return [_build_share_response(db, share) for share in shares]


@router.post(
    "/{collection_uuid}/shares",
    response_model=Share,
    status_code=status.HTTP_201_CREATED,
)
def create_collection_share(
    collection_uuid: str,
    share_in: ShareCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Share a collection with a user or group. Requires direct collection ownership. Tenant-gated (#262d)."""
    collection = get_collection_by_uuid_with_permission(
        db, collection_uuid, current_user.id, organization_id=ctx.org_id
    )
    _require_collection_owner(db, collection, current_user.id)

    target_user_id = None
    target_group_id = None

    if share_in.target_type == "user":
        target_user = get_by_uuid(db, User, str(share_in.target_uuid), "User not found")

        # Cannot share with yourself
        if target_user.id == current_user.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot share a collection with yourself",
            )

        # Tenant gate: an org-scoped collection may only be shared with active
        # members of that organization — cross-org shares would otherwise leak
        # org content to outsiders. Personal collections (org NULL) keep the
        # existing behavior (shareable with any user).
        if collection.organization_id is not None:
            from app.models.organization import OrganizationMembership

            membership = (
                db.query(OrganizationMembership)
                .filter(
                    OrganizationMembership.organization_id == collection.organization_id,
                    OrganizationMembership.user_id == target_user.id,
                )
                .first()
            )
            if membership is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "Cannot share an organization collection with a user who is "
                        "not a member of that organization"
                    ),
                )

        # Check for existing share
        existing = (
            db.query(CollectionShare)
            .filter(
                CollectionShare.collection_id == collection.id,
                CollectionShare.target_user_id == target_user.id,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Collection is already shared with this user",
            )

        target_user_id = target_user.id

    elif share_in.target_type == "group":
        target_group = get_by_uuid(db, UserGroup, str(share_in.target_uuid), "Group not found")

        # Verify the sharer is a member of the target group
        is_member = (
            db.query(UserGroupMember)
            .filter(
                UserGroupMember.group_id == target_group.id,
                UserGroupMember.user_id == current_user.id,
            )
            .first()
        )
        if not is_member:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You must be a member of the group to share with it",
            )

        # Tenant gate (issue #262d): groups can span organizations, so a
        # group-targeted share of an org-stamped collection requires EVERY
        # group member to hold membership in that org — otherwise the share
        # grants the non-member(s) a view onto the tenant's content.
        if collection.organization_id is not None:
            from app.models.organization import OrganizationMembership

            group_member_ids = {
                int(row[0])
                for row in db.query(UserGroupMember.user_id)
                .filter(UserGroupMember.group_id == target_group.id)
                .all()
            }
            org_member_ids = {
                int(row[0])
                for row in db.query(OrganizationMembership.user_id)
                .filter(OrganizationMembership.organization_id == collection.organization_id)
                .all()
            }
            outside = group_member_ids - org_member_ids
            if outside:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "Cannot share an organization collection with this group: "
                        f"{len(outside)} group member(s) are not members of that "
                        "organization"
                    ),
                )

        # Check for existing share
        existing = (
            db.query(CollectionShare)
            .filter(
                CollectionShare.collection_id == collection.id,
                CollectionShare.target_group_id == target_group.id,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Collection is already shared with this group",
            )

        target_group_id = target_group.id

    share = CollectionShare(
        collection_id=collection.id,
        shared_by_id=current_user.id,
        target_type=share_in.target_type,
        target_user_id=target_user_id,
        target_group_id=target_group_id,
        permission=share_in.permission,
    )
    db.add(share)
    db.commit()
    db.refresh(share)

    # Reload with relationships
    reloaded_share = (
        db.query(CollectionShare)
        .options(
            joinedload(CollectionShare.shared_by),
            joinedload(CollectionShare.target_user),
            joinedload(CollectionShare.target_group),
        )
        .filter(CollectionShare.id == share.id)
        .first()
    )
    assert reloaded_share is not None  # just committed above
    share = reloaded_share

    # Reindex OpenSearch accessible_user_ids for files in this collection
    file_ids = [
        cm.media_file_id
        for cm in db.query(CollectionMember.media_file_id)
        .filter(CollectionMember.collection_id == collection.id)
        .all()
    ]
    if file_ids:
        update_file_access_index.delay(file_ids)

    # Notify affected user(s) about the new share
    _notify_share_event(db, share, collection, NOTIFICATION_TYPE_COLLECTION_SHARED)

    req_ctx = get_request_context(request)
    audit_logger.log(
        event_type=AuditEventType.RESOURCE_SHARE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=req_ctx["source_ip"],
        user_agent=req_ctx["user_agent"],
        organization_id=ctx.org_id,
        target_user_id=target_user_id,
        details={
            "resource_type": "collection",
            "resource_uuid": str(collection.uuid),
            "resource_name": collection.name,
            "target_group_id": target_group_id,
            "permission": share_in.permission,
        },
    )

    return _build_share_response(db, share)


@router.put("/{collection_uuid}/shares/{share_uuid}", response_model=Share)
def update_collection_share(
    collection_uuid: str,
    share_uuid: str,
    share_update: ShareUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Update a share's permission level. Requires direct collection ownership. Tenant-gated (#262d)."""
    collection = get_collection_by_uuid_with_permission(
        db, collection_uuid, current_user.id, organization_id=ctx.org_id
    )
    _require_collection_owner(db, collection, current_user.id)

    share = get_by_uuid(db, CollectionShare, share_uuid, "Share not found")

    # Verify share belongs to this collection
    if share.collection_id != collection.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Share not found on this collection",
        )

    previous_permission = share.permission
    share.permission = share_update.permission
    db.commit()
    db.refresh(share)

    # Reload with relationships
    reloaded_share = (
        db.query(CollectionShare)
        .options(
            joinedload(CollectionShare.shared_by),
            joinedload(CollectionShare.target_user),
            joinedload(CollectionShare.target_group),
        )
        .filter(CollectionShare.id == share.id)
        .first()
    )
    assert reloaded_share is not None  # just committed above
    share = reloaded_share

    # Reindex files since permission level changed
    file_ids = [
        cm.media_file_id
        for cm in db.query(CollectionMember.media_file_id)
        .filter(CollectionMember.collection_id == collection.id)
        .all()
    ]
    if file_ids:
        update_file_access_index.delay(file_ids)

    # Notify affected user(s) about the permission change
    _notify_share_event(db, share, collection, NOTIFICATION_TYPE_COLLECTION_SHARE_UPDATED)

    req_ctx = get_request_context(request)
    audit_logger.log(
        event_type=AuditEventType.RESOURCE_SHARE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=req_ctx["source_ip"],
        user_agent=req_ctx["user_agent"],
        organization_id=ctx.org_id,
        target_user_id=share.target_user_id,
        details={
            "resource_type": "collection",
            "resource_uuid": str(collection.uuid),
            "resource_name": collection.name,
            "target_group_id": share.target_group_id,
            "action": "permission_update",
            "previous_permission": previous_permission,
            "permission": share.permission,
        },
    )

    return _build_share_response(db, share)


@router.delete(
    "/{collection_uuid}/shares/{share_uuid}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_collection_share(
    collection_uuid: str,
    share_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Revoke a share on a collection. Requires direct collection ownership. Tenant-gated (#262d)."""
    collection = get_collection_by_uuid_with_permission(
        db, collection_uuid, current_user.id, organization_id=ctx.org_id
    )
    _require_collection_owner(db, collection, current_user.id)

    share = get_by_uuid(db, CollectionShare, share_uuid, "Share not found")

    # Verify share belongs to this collection
    if share.collection_id != collection.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Share not found on this collection",
        )

    # Capture notification/audit data before deletion -- gone from the row after.
    target_user_ids = _get_share_target_user_ids(db, share, collection)
    revoked_target_user_id = share.target_user_id
    revoked_target_group_id = share.target_group_id
    revoked_permission = share.permission
    revoke_data: dict = {
        "collection_uuid": str(collection.uuid),
        "collection_name": collection.name,
        "share_uuid": share_uuid,
        "permission": share.permission,
        "message": _share_message(NOTIFICATION_TYPE_COLLECTION_SHARE_REVOKED, collection.name),
    }

    db.delete(share)
    db.commit()

    # Reindex OpenSearch accessible_user_ids for files in this collection
    file_ids = [
        cm.media_file_id
        for cm in db.query(CollectionMember.media_file_id)
        .filter(CollectionMember.collection_id == collection.id)
        .all()
    ]
    if file_ids:
        update_file_access_index.delay(file_ids)

    req_ctx = get_request_context(request)
    audit_logger.log(
        event_type=AuditEventType.RESOURCE_UNSHARE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=req_ctx["source_ip"],
        user_agent=req_ctx["user_agent"],
        organization_id=ctx.org_id,
        target_user_id=revoked_target_user_id,
        details={
            "resource_type": "collection",
            "resource_uuid": str(collection.uuid),
            "resource_name": collection.name,
            "target_group_id": revoked_target_group_id,
            "permission": revoked_permission,
        },
    )

    # Notify affected user(s) about the revocation
    for uid in target_user_ids:
        send_ws_event(uid, NOTIFICATION_TYPE_COLLECTION_SHARE_REVOKED, revoke_data)

    return None
