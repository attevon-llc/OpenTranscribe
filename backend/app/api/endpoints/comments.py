"""API endpoints for media file comments with sharing-aware permissions."""

from typing import Any
from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.hold_refusal import refuse_if_frozen
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.db.base import get_db
from app.models.media import Comment
from app.models.media import MediaFile
from app.models.user import User
from app.schemas.media import Comment as CommentSchema
from app.schemas.media import CommentCreate
from app.schemas.media import CommentCreateStandalone
from app.schemas.media import CommentUpdate
from app.services import audit_outbox
from app.services.comment_queries import add_comment
from app.services.comment_queries import file_org_and_owner
from app.services.comment_queries import file_organization_id
from app.services.comment_queries import file_owner_id
from app.services.comment_queries import get_comment_file
from app.services.comment_queries import list_file_comments
from app.services.comment_queries import load_comment
from app.services.content_hold_service import record_hold_annotation
from app.services.permission_service import PermissionService
from app.services.platform_bypass import PlatformBypass
from app.services.request_context import RequestContext
from app.services.uuid_helpers import get_comment_by_uuid
from app.services.uuid_helpers import get_file_by_uuid_with_permission
from app.services.uuid_helpers import require_resource_owner

router = APIRouter()


def _check_file_access(
    db: Session,
    file_uuid: str,
    current_user: User,
    organization_id: OrgScope = UNSCOPED,
    *,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> MediaFile:
    """Get a media file after verifying the user has at least viewer permission.

    Uses PermissionService to check ownership, direct shares, and group shares.
    ``organization_id`` tenant-gates the lookup (default-deny across scopes).
    """
    media_file = get_file_by_uuid_with_permission(
        db,
        file_uuid,
        current_user.id,
        bypass=bypass,
        organization_id=organization_id,
    )
    return media_file


@router.get("/files/{file_uuid}/comments", response_model=list[CommentSchema])
def get_comments_for_file_nested(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Get all comments for a specific media file (nested route).

    Requires viewer+ permission on the file (via ownership or sharing).
    """
    media_file = _check_file_access(
        db, file_uuid, current_user, organization_id=ctx.org_id, bypass=ctx.bypass
    )
    file_id = media_file.id

    # Get comments for this file with user relationship loaded
    return list_file_comments(db, file_id, order_by_timestamp=False)


def _annotate_if_held(
    db: Session,
    request: Request,
    current_user: User,
    media_file: MediaFile,
    comment: Comment,
    ctx: RequestContext,
) -> None:
    """A new comment on a held file is allowed and recorded (``content.hold.annotation``)."""
    record_hold_annotation(
        db, request, current_user, media_file, str(comment.uuid), bypass=ctx.bypass
    )


def _refuse_if_comment_frozen(
    db: Session, comment: Comment, current_user: User, *, action: str
) -> None:
    """An existing comment on a held file is part of its preserved record (legal-hold freeze)."""
    refuse_if_frozen(db, int(comment.media_file_id), actor=current_user, action=action)


@router.post("/files/{file_uuid}/comments", response_model=CommentSchema)
def create_comment_for_file_nested(
    file_uuid: str,
    comment: CommentCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Create a comment for a specific media file (nested route).

    Requires viewer+ permission on the file (commenting is collaborative).
    """
    media_file = _check_file_access(
        db, file_uuid, current_user, organization_id=ctx.org_id, bypass=ctx.bypass
    )
    file_id = media_file.id

    # Create comment with file_id from URL
    db_comment = add_comment(
        db,
        media_file_id=file_id,
        user_id=current_user.id,
        text=comment.text,
        timestamp=comment.timestamp,
    )
    _annotate_if_held(db, request, current_user, media_file, db_comment, ctx)
    db.commit()
    db.refresh(db_comment)
    audit_outbox.deliver_staged(db)

    # Reload with relationships for UUID mapping
    db_comment_reloaded = load_comment(db, db_comment.id)

    return CommentSchema.model_validate(db_comment_reloaded)


@router.get("", response_model=list[CommentSchema])
def get_comments_for_file(
    media_file_id: str | None = None,
    media_file_uuid: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """List all comments for a media file using query parameter.

    Accepts the file's public UUID as ``media_file_id`` (what the frontend
    fallback sends) or the legacy ``media_file_uuid`` name. Requires viewer+
    permission on the file.
    """
    file_ref = media_file_id or media_file_uuid
    if not file_ref:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Query parameter 'media_file_id' is required",
        )
    media_file = _check_file_access(
        db, file_ref, current_user, organization_id=ctx.org_id, bypass=ctx.bypass
    )
    media_file_pk = media_file.id

    # Get comments for this file (eager-load relationships to avoid N+1)
    return list_file_comments(db, media_file_pk, order_by_timestamp=True)


@router.post("", response_model=CommentSchema)
def create_comment_standalone(
    comment: CommentCreateStandalone,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Add a comment to a media file referenced by UUID in the request body.

    Fallback route used by the frontend when the nested
    ``POST /files/{file_uuid}/comments`` route is unavailable. Requires
    viewer+ permission on the file.
    """
    media_file = _check_file_access(
        db,
        str(comment.media_file_id),
        current_user,
        organization_id=ctx.org_id,
        bypass=ctx.bypass,
    )
    file_id = media_file.id

    # Create new comment
    db_comment = add_comment(
        db,
        media_file_id=file_id,
        user_id=current_user.id,  # Always use the authenticated user's ID
        text=comment.text,
        timestamp=comment.timestamp,
    )
    _annotate_if_held(db, request, current_user, media_file, db_comment, ctx)
    db.commit()
    db.refresh(db_comment)
    audit_outbox.deliver_staged(db)

    # Reload with relationships for UUID mapping
    db_comment_reloaded = load_comment(db, db_comment.id)

    return CommentSchema.model_validate(db_comment_reloaded)


def _assert_comment_file_in_scope(
    db: Session,
    comment: Comment,
    current_user: User,
    ctx: RequestContext,
    *,
    forbidden_detail: str,
    need: Literal["read", "write"] = "read",
) -> None:
    """Refuse a comment whose file is outside the caller's tenant scope.

    Authorship is NOT a tenant. Every mutation below also applies its own
    ownership rule, but ownership alone let a caller edit and delete a comment on
    a file the read path refuses to show them: they authored it while acting in
    another organization, and ``comment.user_id == current_user.id`` stays true
    forever. This gate runs first so the read and write paths agree on what is
    reachable, and the ownership rules only ever narrow that further.

    The platform role bypasses through ``ctx.bypass``, decided on the comment's
    file and its tenant (single-tenant mode only), matching ``_check_file_access``.

    ``PermissionService.get_file_permission`` has no notion of quarantine at
    all (ownership/sharing only), unlike ``_check_file_access`` (used by the
    file-scoped GET/POST routes above), which resolves through
    ``get_file_by_uuid_with_permission`` and so already 404s a quarantined
    file. Without this check, reaching a comment directly by its own UUID
    (``GET``/``PUT``/``DELETE /{comment_uuid}``) bypassed that gate entirely —
    A2's leak class, and it reached even the file's own owner: a comment on
    your own quarantined file stayed fully readable/editable/deletable here
    while the file itself 404s everywhere else.
    """
    from app.services.file_availability import is_unavailable

    media_file = get_comment_file(db, comment.media_file_id)
    if media_file is not None and ctx.bypass.allows(
        org_id=media_file.organization_id,
        owner_id=media_file.user_id,
        need=need,
        resource_type="comment",
        resource_uuid=str(comment.uuid),
    ):
        return
    permission = PermissionService.get_file_permission(
        db, int(comment.media_file_id), current_user.id, organization_id=ctx.org_id
    )
    if permission is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=forbidden_detail,
        )
    if media_file is not None and is_unavailable(media_file):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")


@router.get("/{comment_uuid}", response_model=CommentSchema)
def get_comment(
    comment_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Get a single comment by UUID.

    Requires viewer+ permission on the comment's file.
    """
    comment = get_comment_by_uuid(db, comment_uuid)

    # Verify the user has access to the comment's file via PermissionService
    # (tenant-gated via ctx.org_id).
    _assert_comment_file_in_scope(
        db,
        comment,
        current_user,
        ctx,
        forbidden_detail="You do not have permission to view this comment",
    )

    return comment


@router.put("/{comment_uuid}", response_model=CommentSchema)
def update_comment(
    comment_uuid: str,
    comment_update: CommentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Update a comment. Only the comment author can edit, within their tenant."""
    comment = get_comment_by_uuid(db, comment_uuid)

    _assert_comment_file_in_scope(
        db,
        comment,
        current_user,
        ctx,
        forbidden_detail="You do not have permission to edit this comment",
        need="write",
    )

    comment_file_org = file_organization_id(db, comment.media_file_id)
    require_resource_owner(
        comment,
        current_user,
        forbidden_detail="You do not have permission to edit this comment",
        bypass=ctx.bypass,
        org_id=comment_file_org,
        resource_type="comment",
    )
    _refuse_if_comment_frozen(db, comment, current_user, action="comment_edit")

    # Update fields
    for field, value in comment_update.model_dump(exclude_unset=True).items():
        setattr(comment, field, value)

    db.commit()

    # Reload with relationships for UUID mapping
    db.refresh(comment)
    comment_reloaded = load_comment(db, comment.id)

    # Use model_validate to handle UUID conversion automatically
    return CommentSchema.model_validate(comment_reloaded)


@router.delete("/{comment_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_comment(
    comment_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> None:
    """Delete a comment. Admin, comment author, or file owner — within their tenant.

    The scope gate runs before all three branches on purpose: author and file
    owner are both properties that survive the caller moving organizations, so
    either one alone would re-open the asymmetry this gate closes.
    """
    comment = get_comment_by_uuid(db, comment_uuid)

    _assert_comment_file_in_scope(
        db,
        comment,
        current_user,
        ctx,
        forbidden_detail="You do not have permission to delete this comment",
        need="write",
    )
    _refuse_if_comment_frozen(db, comment, current_user, action="comment_delete")

    # Allow deletion by the platform role (single-tenant mode), on the comment's file
    file_row = file_org_and_owner(db, comment.media_file_id)
    if file_row is not None and ctx.bypass.allows(
        org_id=file_row[0],
        owner_id=file_row[1],
        need="write",
        resource_type="comment",
        resource_uuid=str(comment.uuid),
    ):
        db.delete(comment)
        db.commit()
        return None

    # Allow deletion by comment author
    if comment.user_id == current_user.id:
        db.delete(comment)
        db.commit()
        return None

    # Allow deletion by file owner (project only user_id, not full ORM object)
    if file_owner_id(db, comment.media_file_id) == current_user.id:
        db.delete(comment)
        db.commit()
        return None

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to delete this comment",
    )
