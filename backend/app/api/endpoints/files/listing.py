"""Gallery listing routes: ``GET ""`` (the file list), ``/metadata-filters`` and ``/owners``.

``list_media_files`` (the empty path) is registered on the parent router, which an
empty-prefix sub-router cannot hold.
"""

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request
from fastapi import Response
from sqlalchemy.orm import Query as OrmQuery  # fastapi.Query is already imported
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.auth.rate_limit import get_directory_rate_limit
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.auth.utils import mask_email_for_display
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.user import User
from app.schemas.media import PaginatedMediaFileResponse
from app.schemas.user import UserSearchResult
from app.services.delete_permissions import can_delete_file
from app.services.formatting_service import FormattingService
from app.services.privacy.redact_view import PrivacyView
from app.services.request_context import RequestContext

from .detail_response import set_file_urls
from .filtering import LIST_SPEAKER_COLUMNS
from .filtering import apply_all_filters
from .filtering import get_accessible_owners
from .filtering import get_metadata_filters
from .filtering import hidden_speaker_name_clause
from .filtering import resolve_owner_user_ids

router = APIRouter()
logger = logging.getLogger(__name__)


def _list_load_options() -> list:
    """Eager-load options of the gallery list query (``list_media_files``)."""
    from sqlalchemy.orm import defer
    from sqlalchemy.orm import joinedload
    from sqlalchemy.orm import selectinload

    # Eager-loading strategy for list view:
    # - joinedload for user (many-to-one — no row inflation)
    # - selectinload for speakers (one-to-many — avoids Cartesian product)
    #   with load_only for the two fields the speaker summary needs
    # - defer heavy JSON columns not required by the list Pydantic schema
    list_options = [
        joinedload(MediaFile.user),
        selectinload(MediaFile.speakers).load_only(*LIST_SPEAKER_COLUMNS),  # type: ignore[arg-type]
        defer(MediaFile.metadata_raw),  # type: ignore[arg-type]
        defer(MediaFile.waveform_data),  # type: ignore[arg-type]
    ]
    return list_options


def _sort_and_page(
    filtered_query: OrmQuery, sort_by: str, sort_order: str, page: int, page_size: int
) -> tuple[int, list[MediaFile]]:
    """Total count of the filtered gallery query, then one sorted page of its rows."""
    from sqlalchemy import func as sa_func

    # Apply sorting
    # Note: MediaFile has upload_time, completed_at, filename, duration, file_size
    sort_field_mapping = {
        "upload_time": MediaFile.upload_time,
        "completed_at": MediaFile.completed_at,
        "filename": MediaFile.filename,
        "duration": MediaFile.duration,
        "file_size": MediaFile.file_size,
    }

    # Get the sort field (default to upload_time if invalid)
    sort_field = sort_field_mapping.get(sort_by, MediaFile.upload_time)

    # Get total count BEFORE sorting/pagination.
    # Use with_entities + func.count to avoid the subquery wrapper that
    # .count() generates — produces a flat SELECT count(media_file.id) …
    # instead of SELECT count(*) FROM (SELECT … ORDER BY …).
    total_count = (filtered_query.with_entities(sa_func.count(MediaFile.id)).scalar()) or 0

    # Apply sort order (only for the data query, not the count)
    if sort_order.lower() == "asc":
        filtered_query = filtered_query.order_by(sort_field.asc())  # type: ignore[attr-defined]
    else:
        filtered_query = filtered_query.order_by(sort_field.desc())  # type: ignore[attr-defined]

    # Apply pagination
    offset = (page - 1) * page_size
    paginated_query = filtered_query.offset(offset).limit(page_size)
    result = paginated_query.all()
    return total_count, result


def _format_list_page(db: Session, result: list[MediaFile], ctx: RequestContext) -> list:
    """Format one gallery page: presigned URLs, speaker chips and the delete capability."""
    current_user = ctx.user
    org_scope = ctx.org_id
    # One privacy view for the page: the speaker chips ask the voiceprint policy per FILE
    # scope, and a page of files spans a handful of scopes, not one per speaker (#1280).
    privacy = PrivacyView(db)

    # Format each file with URLs and formatted fields
    formatted_files = []
    for file in result:
        set_file_urls(file, ctx.bypass)

        # Use the FormattingService method which handles formatting correctly
        # Pass speakers for speaker_summary in list view
        formatted_file = FormattingService.format_media_file(file, file.speakers, privacy=privacy)
        formatted_file.can_delete = can_delete_file(
            current_user,
            file,
            organization_id=org_scope,
            is_org_admin=ctx.is_org_admin,
            bypass=ctx.bypass,
        )
        formatted_files.append(formatted_file)
    return formatted_files


def list_media_files(
    # Pagination parameters
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    # Ownership filter
    ownership: str = Query(
        "mine",
        pattern="^(mine|shared|all)$",
        description="Filter: 'mine' (owned), 'shared' (via shared collections), 'all' (both)",
    ),
    # Specific-owner filter (issue #966), repeatable. Orthogonal to `ownership`:
    # applied INSIDE whichever ownership-scoped file set that param already
    # selected (see `resolve_owner_user_ids` / `apply_owner_filter`).
    owner: list[UUID] | None = Query(
        None, description="Filter to specific owner UUID(s); repeatable"
    ),
    # Existing filters
    search: str | None = None,
    tag: list[str] | None = Query(None),
    speaker: list[str] | None = Query(None),
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    min_duration: float | None = None,
    max_duration: float | None = None,
    min_file_size: int | None = None,  # In MB
    max_file_size: int | None = None,  # In MB
    file_type: list[str] | None = Query(None),  # ['audio', 'video']
    status: list[str] | None = Query(
        None
    ),  # ['pending', 'processing', 'completed', 'error', 'cancelling', 'cancelled', 'orphaned']
    transcript_search: str | None = None,  # Search in transcript content
    # Sort parameters (after filters to avoid parameter shifting)
    sort_by: str = Query(
        "upload_time",
        description="Field to sort by: upload_time, completed_at, filename, duration, file_size",
    ),
    sort_order: str = Query("desc", description="Sort order: asc or desc"),
    # Dependencies
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
):
    """List media files for the current user with optional filters and pagination.

    Use ownership param to control scope:
    - 'mine': Only files owned by current user (default, preserves existing behavior)
    - 'shared': Only files accessible via shared collections
    - 'all': Both owned and shared files
    """
    current_user = ctx.user
    from app.services.permission_service import PermissionService

    list_options = _list_load_options()

    user_id = current_user.id
    org_scope = ctx.org_id  # int -> org scope, None -> personal scope (org-less rows)

    # Tenant gate for the admin "see all" and the owned-file branches. In org
    # context only same-org rows; in personal scope only org-less rows. Community
    # edition: org_scope is None and rows are org-less, so this is a no-op.
    if org_scope is not None:
        org_pred = MediaFile.organization_id == org_scope
    else:
        org_pred = MediaFile.organization_id.is_(None)

    from app.services.takedown_service import exclude_quarantined

    # Admin users can see all files regardless of ownership param (still org-gated).
    # Admins also see quarantined (taken-down) files so they can review them; for
    # every other caller the abuse/DMCA exclusion hides taken-down files.
    if ctx.bypass.sees_all_in_scope:
        base_query = db.query(MediaFile).options(*list_options).filter(org_pred)
        effective_user_id = None
    elif ownership == "mine":
        # Default: only owned files (within tenant scope)
        base_query = (
            db.query(MediaFile)
            .options(*list_options)
            .filter(MediaFile.user_id == user_id, org_pred)
        )
        effective_user_id = user_id
    elif ownership == "shared":
        # Only files from shared collections (not owned by user)
        accessible_subquery = PermissionService.get_accessible_file_ids_subquery(
            db, user_id, organization_id=org_scope
        )
        base_query = (
            db.query(MediaFile)
            .options(*list_options)
            .filter(
                MediaFile.id.in_(db.query(accessible_subquery.c.id)),
                MediaFile.user_id != user_id,  # Exclude owned files
            )
        )
        effective_user_id = None  # Don't filter by user_id in apply_all_filters
    else:
        # All: owned + shared
        accessible_subquery = PermissionService.get_accessible_file_ids_subquery(
            db, user_id, organization_id=org_scope
        )
        base_query = (
            db.query(MediaFile)
            .options(*list_options)
            .filter(MediaFile.id.in_(db.query(accessible_subquery.c.id)))
        )
        effective_user_id = None

    # Abuse/DMCA: hide taken-down files from the gallery for non-admins.
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
        "user_id": effective_user_id,
        "owner_user_ids": resolve_owner_user_ids(db, owner),
        "organization_id": org_scope,
    }

    # A speaker filter matches the names the page shows, so a name withheld by the privacy
    # policy (#1280) is not matchable.
    filters["speaker_name_hidden"] = hidden_speaker_name_clause(db, base_query, speaker)

    # Apply all filters
    filtered_query = apply_all_filters(base_query, filters)

    total_count, result = _sort_and_page(filtered_query, sort_by, sort_order, page, page_size)

    formatted_files = _format_list_page(db, result, ctx)

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


@router.get("/metadata-filters", response_model=dict)
def get_metadata_filters_endpoint(
    ownership: str = Query("all", pattern="^(mine|shared|all)$"),
    owner: list[UUID] | None = Query(
        None, description="Filter to specific owner UUID(s); repeatable (issue #966)"
    ),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
):
    """Get available metadata filters like formats, codecs, etc."""
    return get_metadata_filters(
        db,
        ctx.user.id,
        ownership=ownership,
        organization_id=ctx.org_id,
        include_quarantined=ctx.bypass.user_is_admin,
        owner_user_ids=resolve_owner_user_ids(db, owner),
    )


@router.get("/owners", response_model=list[UserSearchResult])
@limiter.limit(get_directory_rate_limit(), key_func=user_or_ip_key)
def list_file_owners(
    request: Request,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
):
    """List the owners of files visible to the caller, for the gallery's
    ownership filter (issue #966).

    **Closed enumeration, not a directory search.** This deliberately takes
    no free-text query parameter — the caller receives the (capped) list of
    owners of files they can already see and narrows it client-side, the same
    shape ``SearchableMultiSelect`` already uses for tags and collections. A
    free-text parameter would turn an authorized list into an
    account-probing oracle, which is exactly what this route must not be.

    **Scope is `get_accessible_owners`**, which joins ``User`` against the
    IDENTICAL predicate ``GET /files`` uses to decide what this caller can
    see (``PermissionService.get_accessible_file_ids_subquery``, or the same
    org-tenant predicate for an admin) — never the tenant-wide scope
    ``GET /users/search`` uses for the sharing picker. A user who shares
    nothing with anyone else, and with whom nothing is shared, sees only
    themselves here.

    **Rate-limited** the same way as ``GET /users/search``
    (``RATE_LIMIT_DIRECTORY_PER_MINUTE``), as a volume/noise bound — the
    authorization boundary is the join predicate above, not this limit.

    **Payload-minimized**: reuses ``UserSearchResult`` — the exact wire shape
    the sharing picker already exposes (``uuid``, ``full_name``,
    ``masked_email``) — so this route exposes no more per-owner information
    than the app already shows elsewhere.
    """
    owners = get_accessible_owners(db, ctx.user.id, organization_id=ctx.org_id, bypass=ctx.bypass)
    return [
        UserSearchResult(
            uuid=u.uuid, full_name=u.full_name, masked_email=mask_email_for_display(u.email)
        )
        for u in owners
    ]
