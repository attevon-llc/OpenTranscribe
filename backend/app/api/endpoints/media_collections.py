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

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy import and_
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints import media_collections_listing
from app.api.endpoints import media_collections_membership
from app.api.endpoints import media_collections_shares
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.media_collections_access import _can_change_visibility
from app.api.endpoints.media_collections_access import _collection_held
from app.api.endpoints.media_collections_access import _get_prompt_info
from app.api.endpoints.media_collections_access import _hold_refusal
from app.api.endpoints.media_collections_access import _resolve_prompt_uuid
from app.api.endpoints.media_collections_access import _tenant_scope
from app.api.endpoints.media_collections_listing import list_collections
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.core.constants import NOTIFICATION_TYPE_COLLECTION_ACCESS_CHANGED
from app.db.base import get_db
from app.middleware.audit import get_request_context
from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.user import User
from app.schemas.collection import Collection as CollectionSchema
from app.schemas.collection import CollectionCreate
from app.schemas.collection import CollectionUpdate
from app.schemas.collection import CollectionVisibilityUpdate
from app.schemas.collection import CollectionWithCount
from app.schemas.media import CollectionResponse
from app.services.collection_visibility import members_without_access
from app.services.content_hold_queries import collection_has_active_hold
from app.services.permission_service import PermissionService
from app.services.request_context import RequestContext
from app.services.uuid_helpers import get_collection_by_uuid_with_permission
from app.services.uuid_helpers import get_collection_by_uuid_with_sharing
from app.services.uuid_helpers import require_resource_owner
from app.tasks.search_file_field_tasks import update_file_access_index
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)

router = APIRouter()


# Registration order is the original route order, which FastAPI matches in: the literal
# listing paths (/shared-with-me, /for-files) and the empty path ahead of /{collection_uuid},
# then this module's routes, then the membership and sharing sub-routes. Do not reorder.
router.include_router(media_collections_listing.router)
# Registered here rather than on a sub-router: an empty path cannot be included with an
# empty prefix.
router.get("", response_model=list[CollectionWithCount])(list_collections)


@router.post(
    "", response_model=CollectionSchema, dependencies=[Depends(refuse_under_support_grant)]
)
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
    if collection.visibility != "organization" and ctx.org_id is None:
        raise HTTPException(
            status_code=422,
            detail="Only an organization collection can be restricted",
        )

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
    result.can_change_visibility = _can_change_visibility(db_collection, "owner")
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

    media_files = [
        member.media_file
        for member in collection.collection_members
        if not is_hidden_for(member.media_file, is_admin=ctx.bypass.user_is_admin)
    ]

    # Build response with prompt info
    result = CollectionResponse.model_validate(collection)
    # ORM MediaFile list coerced to schema via response_model serialization
    result.media_files = media_files  # type: ignore[assignment]
    prompt_uuid, prompt_name = _get_prompt_info(collection)
    result.default_prompt_id = prompt_uuid
    result.default_prompt_name = prompt_name
    result.can_change_visibility = _can_change_visibility(
        collection, PermissionService.get_collection_permission(db, collection.id, current_user.id)
    )
    result.hold = _collection_held(db, collection, ctx)

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
    result.can_change_visibility = _can_change_visibility(collection, permission)
    result.hold = _collection_held(db, collection, ctx)
    return result


@router.patch(
    "/{collection_uuid}/visibility",
    response_model=CollectionSchema,
    dependencies=[Depends(refuse_under_support_grant)],
)
def set_collection_visibility(
    collection_uuid: str,
    body: CollectionVisibilityUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Restrict an organization collection to its named people, or open it to the organization.

    ``restricted``: only the creator, the organization's admins and the people it is shared
    with (``/shares``) see it or the files in it through it. ``organization``: every member.
    Authority is the collection's owners (its creator or an organization admin); a person who
    cannot see a restricted collection gets 404, exactly as for a missing one, and a platform
    support grant never reaches it. A personal collection has no organization to hide it
    from: 422. This is the ONLY path that changes visibility (``PUT`` does not accept it).

    Effects: the audit event ``collection.visibility.change`` (always recorded, the
    collection's name is never in it), a ``collection_access_changed`` WebSocket event
    carrying only the collection uuid to the members who just lost sight of it so their UI
    drops it, and a retrieval-cache bump so a cached chat answer cannot keep serving
    excerpts to somebody who no longer may read them. No reindex is needed: search reads the
    caller's visibility from Postgres per request.
    """
    collection = get_collection_by_uuid_with_permission(
        db, collection_uuid, current_user.id, organization_id=ctx.org_id
    )
    if collection.organization_id is None:
        raise HTTPException(
            status_code=422, detail="Only an organization collection has a visibility"
        )
    permission = PermissionService.get_collection_permission(db, collection.id, current_user.id)
    if permission != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the collection's creator or an organization admin can change who sees it",
        )

    previous = collection.visibility
    if body.visibility != previous:
        collection.visibility = body.visibility
        db.commit()
        db.refresh(collection)

        from app.services.chat.retrieval_cache import bump_corpus_version

        lost_sight = members_without_access(db, collection)
        for member_id in lost_sight:
            send_ws_event(
                member_id,
                NOTIFICATION_TYPE_COLLECTION_ACCESS_CHANGED,
                {"collection_uuid": str(collection.uuid)},
            )
        bump_corpus_version()

        req_ctx = get_request_context(request)
        audit_logger.log(
            event_type=AuditEventType.COLLECTION_VISIBILITY_CHANGE,
            outcome=AuditOutcome.SUCCESS,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=req_ctx["source_ip"],
            user_agent=req_ctx["user_agent"],
            organization_id=collection.organization_id,
            resource_type="collection",
            resource_uuid=str(collection.uuid),
            tenant_scope=_tenant_scope(collection),
            details={
                "from": previous,
                "to": body.visibility,
                "members_who_lost_access": len(lost_sight),
            },
        )

    result = CollectionSchema.model_validate(collection)
    prompt_uuid, prompt_name = _get_prompt_info(collection)
    result.default_prompt_id = prompt_uuid
    result.default_prompt_name = prompt_name
    result.can_change_visibility = _can_change_visibility(collection, permission)
    result.hold = _collection_held(db, collection, ctx)
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

    # A collection under a hold is evidence in its own right (issue #1276): it cannot be deleted
    # until the hold is released, even when it is empty.
    if collection_has_active_hold(db, int(collection.id)):
        raise _hold_refusal(
            "refused: this collection is under an active legal hold and cannot be deleted. "
            "Release the hold before deleting.",
            db=db,
            collection=collection,
            ctx=ctx,
            action="delete_collection",
        )

    # Capture the members before the cascade removes them; reindex after the commit so the
    # rewrite reads the collection as gone.
    file_ids = [
        cm.media_file_id
        for cm in db.query(CollectionMember.media_file_id)
        .filter(CollectionMember.collection_id == collection.id)
        .all()
    ]

    db.delete(collection)
    db.commit()
    if file_ids:
        update_file_access_index.delay(file_ids)

    return {"message": "Collection deleted successfully"}


router.include_router(media_collections_membership.router)
router.include_router(media_collections_shares.router)
