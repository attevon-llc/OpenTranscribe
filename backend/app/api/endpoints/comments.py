"""API endpoints for media file comments with sharing-aware permissions."""

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.db.base import get_db
from app.models.document import Document
from app.models.document import DocumentChunk
from app.models.media import Comment
from app.models.media import MediaFile
from app.models.user import User
from app.schemas.document import DocumentCommentCreate
from app.schemas.media import Comment as CommentSchema
from app.schemas.media import CommentCreate
from app.schemas.media import CommentCreateStandalone
from app.schemas.media import CommentUpdate
from app.services.permission_service import PERMISSION_LEVELS
from app.services.permission_service import PermissionService
from app.utils.uuid_helpers import get_by_uuid
from app.utils.uuid_helpers import get_comment_by_uuid
from app.utils.uuid_helpers import get_file_by_uuid_with_permission
from app.utils.uuid_helpers import require_resource_owner

router = APIRouter()


def _check_document_access(
    db: Session,
    document_uuid: str,
    current_user: User,
    organization_id: OrgScope = UNSCOPED,
    min_permission: str = "viewer",
) -> Document:
    """Get a document after verifying the user has at least *min_permission*.

    v400 (#362 lane C3-remainder/C5) — the document counterpart of
    ``_check_file_access``, using ``PermissionService.get_document_permission``
    (owner + direct/group ``DocumentShare`` grants) rather than the collection-sharing
    rule that method reads for media files. **404, not 403**, on a document the caller
    cannot reach — matching ``documents.py:_get_owned_document``'s convention rather
    than this module's own 403-for-media convention: a document that exists but is out
    of reach must not be distinguishable from one that does not exist, the same
    404-not-403 reasoning `app/api/CLAUDE.md`'s tag plane gives.
    """
    doc = get_by_uuid(db, Document, document_uuid, error_message="Document not found")
    if current_user.is_admin:
        return doc
    permission = PermissionService.get_document_permission(
        db, doc.id, current_user.id, organization_id=organization_id
    )
    if permission is None or PERMISSION_LEVELS[permission] < PERMISSION_LEVELS[min_permission]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return doc


def _check_file_access(
    db: Session,
    file_uuid: str,
    current_user: User,
    organization_id: OrgScope = UNSCOPED,
) -> MediaFile:
    """Get a media file after verifying the user has at least viewer permission.

    Uses PermissionService to check ownership, direct shares, and group shares.
    ``organization_id`` tenant-gates the lookup (default-deny across scopes).
    """
    media_file = get_file_by_uuid_with_permission(
        db,
        file_uuid,
        current_user.id,
        is_admin=current_user.is_admin,
        organization_id=organization_id,
    )
    return media_file


@router.get("/files/{file_uuid}/comments", response_model=list[CommentSchema])
def get_comments_for_file_nested(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Get all comments for a specific media file (nested route).

    Requires viewer+ permission on the file (via ownership or sharing).
    """
    media_file = _check_file_access(db, file_uuid, current_user, organization_id=ctx.org_id)
    file_id = media_file.id

    # Get comments for this file with user relationship loaded
    comments = (
        db.query(Comment)
        .options(joinedload(Comment.user), joinedload(Comment.media_file))
        .filter(Comment.media_file_id == file_id)
        .all()
    )
    return comments


@router.post("/files/{file_uuid}/comments", response_model=CommentSchema)
def create_comment_for_file_nested(
    file_uuid: str,
    comment: CommentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Create a comment for a specific media file (nested route).

    Requires viewer+ permission on the file (commenting is collaborative).
    """
    media_file = _check_file_access(db, file_uuid, current_user, organization_id=ctx.org_id)
    file_id = media_file.id

    # Create comment with file_id from URL
    db_comment = Comment(
        text=comment.text,
        timestamp=comment.timestamp,
        media_file_id=file_id,
        user_id=current_user.id,
    )
    db.add(db_comment)
    db.commit()
    db.refresh(db_comment)

    # Reload with relationships for UUID mapping
    db_comment_reloaded = (
        db.query(Comment)
        .options(joinedload(Comment.user), joinedload(Comment.media_file))
        .filter(Comment.id == db_comment.id)
        .first()
    )

    return CommentSchema.model_validate(db_comment_reloaded)


@router.get("/documents/{document_uuid}/comments", response_model=list[CommentSchema])
def get_comments_for_document_nested(
    document_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Get all notes for a specific document (v400, #362 lane C5) — the document
    analogue of :func:`get_comments_for_file_nested`. Requires viewer+ permission.
    """
    doc = _check_document_access(db, document_uuid, current_user, organization_id=ctx.org_id)
    comments = (
        db.query(Comment)
        .options(
            joinedload(Comment.user),
            joinedload(Comment.document),
            joinedload(Comment.document_chunk),
        )
        .filter(Comment.document_id == doc.id)
        .order_by(Comment.created_at)
        .all()
    )
    return comments


@router.post("/documents/{document_uuid}/comments", response_model=CommentSchema)
def create_comment_for_document_nested(
    document_uuid: str,
    comment: DocumentCommentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Add a note to a document, optionally anchored to a chunk (v400, #362 lane C5).

    Requires viewer+ permission on the document (commenting is collaborative, same
    rule the media-file nested route applies).
    """
    doc = _check_document_access(db, document_uuid, current_user, organization_id=ctx.org_id)

    document_chunk_id = None
    if comment.document_chunk_index is not None:
        chunk = (
            db.query(DocumentChunk)
            .filter(
                DocumentChunk.document_id == doc.id,
                DocumentChunk.chunk_index == comment.document_chunk_index,
            )
            .first()
        )
        if chunk is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Document has no chunk at index {comment.document_chunk_index}",
            )
        document_chunk_id = chunk.id

    db_comment = Comment(
        text=comment.text,
        document_id=doc.id,
        document_chunk_id=document_chunk_id,
        user_id=current_user.id,
    )
    db.add(db_comment)
    db.commit()
    db.refresh(db_comment)

    db_comment_reloaded = (
        db.query(Comment)
        .options(
            joinedload(Comment.user),
            joinedload(Comment.document),
            joinedload(Comment.document_chunk),
        )
        .filter(Comment.id == db_comment.id)
        .first()
    )

    return CommentSchema.model_validate(db_comment_reloaded)


@router.get("", response_model=list[CommentSchema])
def get_comments_for_file(
    media_file_id: str | None = None,
    media_file_uuid: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
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
    media_file = _check_file_access(db, file_ref, current_user, organization_id=ctx.org_id)
    media_file_pk = media_file.id

    # Get comments for this file (eager-load relationships to avoid N+1)
    comments = (
        db.query(Comment)
        .options(joinedload(Comment.user), joinedload(Comment.media_file))
        .filter(Comment.media_file_id == media_file_pk)
        .order_by(Comment.timestamp)
        .all()
    )

    return comments


@router.post("", response_model=CommentSchema)
def create_comment_standalone(
    comment: CommentCreateStandalone,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Add a comment to a media file referenced by UUID in the request body.

    Fallback route used by the frontend when the nested
    ``POST /files/{file_uuid}/comments`` route is unavailable. Requires
    viewer+ permission on the file.
    """
    media_file = _check_file_access(
        db, str(comment.media_file_id), current_user, organization_id=ctx.org_id
    )
    file_id = media_file.id

    # Create new comment
    db_comment = Comment(
        media_file_id=file_id,
        user_id=current_user.id,  # Always use the authenticated user's ID
        text=comment.text,
        timestamp=comment.timestamp,
    )

    db.add(db_comment)
    db.commit()
    db.refresh(db_comment)

    # Reload with relationships for UUID mapping
    db_comment_reloaded = (
        db.query(Comment)
        .options(joinedload(Comment.user), joinedload(Comment.media_file))
        .filter(Comment.id == db_comment.id)
        .first()
    )

    return CommentSchema.model_validate(db_comment_reloaded)


def _assert_comment_file_in_scope(
    db: Session,
    comment: Comment,
    current_user: User,
    ctx: RequestContext,
    *,
    forbidden_detail: str,
) -> None:
    """Refuse a comment whose file/document is outside the caller's tenant scope.

    Authorship is NOT a tenant. Every mutation below also applies its own
    ownership rule, but ownership alone let a caller edit and delete a comment on
    a file the read path refuses to show them: they authored it while acting in
    another organization, and ``comment.user_id == current_user.id`` stays true
    forever. This gate runs first so the read and write paths agree on what is
    reachable, and the ownership rules only ever narrow that further.

    Admins bypass, matching ``get_comment`` and ``_check_file_access`` — one rule
    for the whole module rather than a second, divergent one.

    v400 (#362 lane C3-remainder/C5): branches on which owner column is set
    (exactly one is, per ``ck_comment_exactly_one_owner``) and reads the matching
    ``PermissionService`` rule — ``get_file_permission`` for a media comment,
    ``get_document_permission`` for a document one.
    """
    if current_user.is_admin:
        return
    if comment.document_id is not None:
        permission = PermissionService.get_document_permission(
            db, int(comment.document_id), current_user.id, organization_id=ctx.org_id
        )
    else:
        # ck_comment_exactly_one_owner guarantees media_file_id is set here (the
        # `if` above took the document_id branch when it was); mypy cannot see a
        # database CHECK, so this narrows explicitly rather than widening the type.
        assert comment.media_file_id is not None
        permission = PermissionService.get_file_permission(
            db, comment.media_file_id, current_user.id, organization_id=ctx.org_id
        )
    if permission is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=forbidden_detail,
        )


@router.get("/{comment_uuid}", response_model=CommentSchema)
def get_comment(
    comment_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
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
):
    """Update a comment. Only the comment author can edit, within their tenant."""
    comment = get_comment_by_uuid(db, comment_uuid)

    _assert_comment_file_in_scope(
        db,
        comment,
        current_user,
        ctx,
        forbidden_detail="You do not have permission to edit this comment",
    )

    require_resource_owner(
        comment,
        current_user,
        forbidden_detail="You do not have permission to edit this comment",
        allow_admin=True,
    )

    # Update fields
    for field, value in comment_update.model_dump(exclude_unset=True).items():
        setattr(comment, field, value)

    db.commit()

    # Reload with relationships for UUID mapping
    db.refresh(comment)
    comment_reloaded = (
        db.query(Comment)
        .options(joinedload(Comment.user), joinedload(Comment.media_file))
        .filter(Comment.id == comment.id)
        .first()
    )

    # Use model_validate to handle UUID conversion automatically
    return CommentSchema.model_validate(comment_reloaded)


@router.delete("/{comment_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_comment(
    comment_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Delete a comment. Admin, comment author, or file/document owner — within
    their tenant.

    The scope gate runs before all three branches on purpose: author and
    file/document owner are both properties that survive the caller moving
    organizations, so either one alone would re-open the asymmetry this gate closes.
    """
    comment = get_comment_by_uuid(db, comment_uuid)

    _assert_comment_file_in_scope(
        db,
        comment,
        current_user,
        ctx,
        forbidden_detail="You do not have permission to delete this comment",
    )

    # Allow deletion by admin
    if current_user.is_admin:
        db.delete(comment)
        db.commit()
        return None

    # Allow deletion by comment author
    if comment.user_id == current_user.id:
        db.delete(comment)
        db.commit()
        return None

    # Allow deletion by the owner of the file/document the comment is on
    # (project only user_id, not full ORM object). v400: branches on which owner
    # column is set, same as _assert_comment_file_in_scope above.
    if comment.document_id is not None:
        owner_row = db.query(Document.user_id).filter(Document.id == comment.document_id).first()
    else:
        owner_row = (
            db.query(MediaFile.user_id).filter(MediaFile.id == comment.media_file_id).first()
        )
    if owner_row and owner_row[0] == current_user.id:
        db.delete(comment)
        db.commit()
        return None

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to delete this comment",
    )
