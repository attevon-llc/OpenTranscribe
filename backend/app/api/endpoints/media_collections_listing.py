"""Collection listing routes: ``GET ""``, ``/shared-with-me`` and ``/for-files``.

``list_collections`` (the empty path) is registered on the parent router, which an
empty-prefix sub-router cannot hold. Handlers are plain ``def`` on purpose: see the
``media_collections`` module docstring (issue #284 A2.5).
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from sqlalchemy import ColumnElement
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.media_collections_access import UNKNOWN_TIMESTAMP
from app.api.endpoints.media_collections_access import _can_change_visibility
from app.api.endpoints.media_collections_access import _get_prompt_info
from app.api.endpoints.media_collections_access import _tenant_pred
from app.core.exceptions import ResourceNotFoundError
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.sharing import CollectionShare
from app.models.user import User
from app.schemas.collection import CollectionOnSelection
from app.schemas.collection import CollectionWithCount
from app.schemas.content_hold import HoldSummary
from app.schemas.sharing import SharedCollectionInfo
from app.schemas.user import UserBrief
from app.services.collection_queries import collections_for_listing
from app.services.collection_queries import collections_holding_files
from app.services.collection_queries import collections_with_owners
from app.services.collection_queries import ids_in_tenant_of_user
from app.services.collection_queries import ids_matching
from app.services.collection_queries import page_matching_ids
from app.services.collection_queries import page_own_collection_ids
from app.services.collection_queries import share_counts
from app.services.collection_queries import shares_reaching_user
from app.services.collection_queries import visible_media_counts
from app.services.content_hold_queries import collection_hold_summaries
from app.services.file_availability import is_unavailable_for
from app.services.file_availability import unavailable_list_exempt
from app.services.permission_service import PermissionService
from app.services.request_context import RequestContext
from app.services.uuid_helpers import get_by_uuid

router = APIRouter()


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
    own = ids_in_tenant_of_user(db, list(candidate), user_id, ctx.org_id)
    in_tenant = ids_matching(db, list(candidate), _tenant_pred(ctx))
    return [(cid, perm) for cid, perm in candidate.items() if cid in in_tenant and cid not in own]


@router.get("/shared-with-me", response_model=list[SharedCollectionInfo])
def list_shared_collections(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
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

    collections = collections_with_owners(db, collection_ids)

    # Filter out owned collections
    collections = [c for c in collections if c.user_id != current_user.id]
    if not collections:
        return []
    filtered_ids = [c.id for c in collections]

    # Batch: media counts per collection (quarantined files hidden for non-admins)
    media_counts = visible_media_counts(
        db, filtered_ids, include_quarantined=unavailable_list_exempt(ctx.bypass)
    )

    # Batch: share records for shared_by info
    shares = shares_reaching_user(db, filtered_ids, current_user.id)
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
) -> Any:
    """Which collections a set of selected files already belongs to.

    The mirror of ``GET /tags/for-files``, so the gallery's two organizing
    modals answer the same question the same way — `file_count` against
    `selection_size` distinguishes a collection holding every selected file from
    one holding a few.

    Scoped to the caller's own collections in the request's tenant — in an
    organization, the org's collections they may see (v422); in the personal
    workspace, their personal ones. Membership of anyone else's collection, or of a
    restricted one they cannot see, is not theirs to read off a selection.

    Registered before ``/{collection_uuid}`` so the literal path is not
    swallowed.
    """

    file_ids = []
    for file_uuid in file_uuids:
        media_file = get_by_uuid(db, MediaFile, file_uuid, error_message="File not found")
        # An unavailable file answers like a missing one: its membership is not readable.
        if is_unavailable_for(ctx.bypass, media_file):
            raise ResourceNotFoundError("File not found")
        file_ids.append(media_file.id)
    selection_size = len(set(file_ids))
    rows = collections_holding_files(
        db, file_ids, user_id=current_user.id, organization_id=ctx.org_id
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


def _ownership_page(
    db: Session,
    ownership: str,
    user_id: int,
    ctx: RequestContext,
    org_pred: ColumnElement[bool],
    *,
    include_quarantined: bool,
    skip: int,
    limit: int,
) -> tuple[list[int], dict[int, int], dict[int, str], dict[int, UserBrief | None]]:
    """One page of ``list_collections``: (ids, member counts, my permission, shared_by).

    Every id set is the caller's accessible collections, narrowed by ``org_pred`` (the
    request's tenant gate, ``_tenant_pred``).
    """
    if ownership == "mine":
        accessible_perms = dict(PermissionService.get_accessible_collection_ids(db, user_id))
        collection_ids = page_own_collection_ids(
            db,
            list(accessible_perms),
            user_id=user_id,
            organization_id=ctx.org_id,
            tenant_pred=org_pred,
            skip=skip,
            limit=limit,
        )
        counts_dict = visible_media_counts(
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
            collection_ids = page_matching_ids(db, collection_ids, org_pred, skip=skip, limit=limit)
            counts_dict = visible_media_counts(
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
            collection_ids = page_matching_ids(db, all_ids, org_pred, skip=skip, limit=limit)
            counts_dict = visible_media_counts(
                db, collection_ids, include_quarantined=include_quarantined
            )

            # Get shared_by info for non-owned collections
            non_owned = [cid for cid in collection_ids if perm_dict.get(cid) != "owner"]
            if non_owned:
                _populate_shared_by(db, non_owned, user_id, shared_by_dict)
        else:
            collection_ids = []
            counts_dict = {}

    return collection_ids, counts_dict, perm_dict, shared_by_dict


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
) -> list[CollectionWithCount]:
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
    include_quarantined = unavailable_list_exempt(ctx.bypass)

    collection_ids, counts_dict, perm_dict, shared_by_dict = _ownership_page(
        db,
        ownership,
        user_id,
        ctx,
        org_pred,
        include_quarantined=include_quarantined,
        skip=skip,
        limit=limit,
    )

    if not collection_ids:
        return []

    # Fetch full collection objects with user and prompt relationships
    # (org_pred re-applied as defense-in-depth against un-gated id sources)
    collections_objs = collections_for_listing(db, collection_ids, org_pred)

    held_by_collection = collection_hold_summaries(
        db, collection_ids, reveal_uuids=ctx.is_org_admin
    )

    # Bulk-fetch share counts for all collections
    share_counts_dict = share_counts(db, collection_ids)

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
        collection_with_count.can_change_visibility = _can_change_visibility(collection, my_perm)
        collection_with_count.hold = held_by_collection.get(collection.id, HoldSummary())
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
    shares = shares_reaching_user(db, collection_ids, user_id)
    for share in shares:
        if share.collection_id not in shared_by_dict and share.shared_by:
            shared_by_dict[share.collection_id] = UserBrief(
                uuid=share.shared_by.uuid,
                full_name=share.shared_by.full_name,
                email=share.shared_by.email,
            )
