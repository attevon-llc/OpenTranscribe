"""Collection membership routes: add, remove and list the files of a collection.

Tenant gate on the files added, the legal-hold guard on removal, the search access reindex and
the membership audit event. Handlers are plain ``def`` on purpose: see the
``media_collections`` module docstring (issue #284 A2.5).
"""

from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm import defer
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import selectinload

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.files.detail_response import set_file_urls
from app.api.endpoints.files.filtering import LIST_SPEAKER_COLUMNS
from app.api.endpoints.files.filtering import apply_all_filters
from app.api.endpoints.files.filtering import hidden_speaker_name_clause
from app.api.endpoints.media_collections_access import _hold_refusal
from app.auth.audit_events import AuditEventType
from app.db.base import get_db
from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.media import MediaFile
from app.models.sharing import CollectionShare
from app.models.user import User
from app.schemas.collection import CollectionMemberAdd
from app.schemas.collection import CollectionMemberRemove
from app.schemas.media import PaginatedMediaFileResponse
from app.services.content_audit import MAX_MEMBERSHIP_UUIDS
from app.services.content_audit import AuditedResource
from app.services.content_audit import record_content_event
from app.services.content_hold_queries import held_by_collection_hold
from app.services.formatting_service import FormattingService
from app.services.privacy.redact_view import PrivacyView
from app.services.request_context import RequestContext
from app.services.takedown_service import exclude_quarantined
from app.services.uuid_helpers import get_collection_by_uuid_with_permission
from app.services.uuid_helpers import get_collection_by_uuid_with_sharing
from app.services.uuid_helpers import validate_uuids
from app.tasks.search_file_field_tasks import update_file_access_index

router = APIRouter()


def _membership_affects_index(db: Session, collection: Collection) -> bool:
    """True when adding/removing files changes who can read them in search.

    An organization collection is a read route for its members (``search/access_filter``
    matches the chunk's ``collection_ids``), a shared one grants its sharees. A private
    personal collection changes neither.
    """
    if collection.organization_id is not None:
        return True
    return (
        db.query(CollectionShare.id).filter(CollectionShare.collection_id == collection.id).first()
        is not None
    )


def _record_membership_change(
    db: Session,
    request: Request,
    actor: User,
    collection: Collection,
    *,
    added: list[str],
    removed: list[str],
) -> None:
    """Audit files added to / removed from a collection (always recorded, issue #1277).

    ``added`` / ``removed`` are file uuids. The event carries the exact counts and at most
    ``MAX_MEMBERSHIP_UUIDS`` uuids; never a name. Written after the change commits.
    """
    if not added and not removed:
        return
    record_content_event(
        db,
        request,
        event=AuditEventType.COLLECTION_MEMBERSHIP_CHANGE,
        actor=actor,
        resource=AuditedResource.collection(collection),
        details={
            "added": len(added),
            "removed": len(removed),
            "file_uuids": (added + removed)[:MAX_MEMBERSHIP_UUIDS],
            "source": "manual",
        },
        dedup=False,
    )


@router.post("/{collection_uuid}/media", response_model=dict)
def add_media_to_collection(
    collection_uuid: str,
    media_data: CollectionMemberAdd,
    request: Request,
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

    # Joining a held collection holds the file (the CollectionMember insert covers it); mirror
    # that into the storage object lock, best effort and after the commit.
    covered = held_by_collection_hold(db, collection_id, added_file_ids)
    if covered:
        from app.services.minio_service import set_object_legal_hold

        for f in media_files:
            if f.id in covered and f.storage_path:
                set_object_legal_hold(str(f.storage_path), True)

    if added_file_ids and _membership_affects_index(db, collection):
        update_file_access_index.delay(added_file_ids)
    _record_membership_change(
        db,
        request,
        current_user,
        collection,
        added=[str(f.uuid) for f in media_files if f.id in set(added_file_ids)],
        removed=[],
    )

    return {
        "message": f"Added {added_count} media files to collection",
        "added": added_count,
        "already_existed": len(existing_ids),
    }


@router.delete("/{collection_uuid}/media", response_model=dict)
def remove_media_from_collection(
    collection_uuid: str,
    media_data: CollectionMemberRemove,
    request: Request,
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
    id_query = db.query(MediaFile.id, MediaFile.uuid).filter(MediaFile.uuid.in_(media_file_uuids))
    if not may_remove_any:
        id_query = id_query.filter(MediaFile.user_id == current_user.id)
    resolved = id_query.all()
    media_file_ids = [r[0] for r in resolved]

    # The members actually present, for the audit event (a uuid that was never in the
    # collection was not "removed").
    present_ids = {
        row[0]
        for row in db.query(CollectionMember.media_file_id).filter(
            CollectionMember.collection_id == collection_id,
            CollectionMember.media_file_id.in_(media_file_ids),
        )
    }

    # A file a hold on THIS collection covers cannot leave it (issue #1276): moving it out would
    # be the way to slip evidence from under a hold. The whole request is refused so nothing is
    # half removed; release the hold first.
    protected = held_by_collection_hold(db, collection_id, present_ids)
    if protected:
        raise _hold_refusal(
            f"refused: {len(protected)} of these files are under this collection's legal hold "
            "and cannot be removed from it. Release the hold first.",
            db=db,
            collection=collection,
            ctx=ctx,
            action="remove_from_collection",
        )

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

    if media_file_ids and _membership_affects_index(db, collection):
        update_file_access_index.delay(media_file_ids)
    _record_membership_change(
        db,
        request,
        current_user,
        collection,
        added=[],
        removed=[str(uuid) for fid, uuid in resolved if fid in present_ids],
    )

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
        selectinload(MediaFile.speakers).load_only(*LIST_SPEAKER_COLUMNS),  # type: ignore[arg-type]
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
        collection.organization_id is None
        and collection.user_id == current_user.id
        and not ctx.bypass.allows(
            org_id=collection.organization_id,
            owner_id=collection.user_id,
            need="read",
            resource_type="collection",
            resource_uuid=str(collection.uuid),
        )
    )
    if own_files_only:
        base_query = base_query.filter(MediaFile.user_id == current_user.id)

    # Abuse/DMCA: quarantined files are hidden from every read surface for
    # non-admins — the paginated collection-media list included (matches the
    # collection detail's is_hidden_for gate and the visible member counts).
    base_query = exclude_quarantined(base_query, include_quarantined=ctx.bypass.user_is_admin)

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

    # A speaker filter matches the names the page shows (#1280).
    filters["speaker_name_hidden"] = hidden_speaker_name_clause(db, base_query, speaker)

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

    # One privacy view for the page (see the main list endpoint, #1280).
    privacy = PrivacyView(db)

    # Format each file with URLs and formatted fields
    formatted_files = []
    for file in result:
        set_file_urls(file, ctx.bypass)
        formatted_file = FormattingService.format_media_file(file, file.speakers, privacy=privacy)
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
