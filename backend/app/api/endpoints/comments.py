"""API endpoints for media file comments with sharing-aware permissions."""

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.media import Comment
from app.models.media import MediaFile
from app.models.user import User
from app.schemas.media import Comment as CommentSchema
from app.schemas.media import CommentCreate
from app.schemas.media import CommentCreateStandalone
from app.schemas.media import CommentUpdate
from app.services.permission_service import PermissionService
from app.utils.uuid_helpers import get_comment_by_uuid
from app.utils.uuid_helpers import get_file_by_uuid_with_permission

router = APIRouter()


def _check_file_access(db: Session, file_uuid: str, current_user: User) -> MediaFile:
    """Get a media file after verifying the user has at least viewer permission.

    Uses PermissionService to check ownership, direct shares, and group shares.
    """
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, int(current_user.id), is_admin=current_user.is_admin
    )
    return media_file


@router.get("/files/{file_uuid}/comments", response_model=list[CommentSchema])
def get_comments_for_file_nested(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get all comments for a specific media file (nested route).

    Requires viewer+ permission on the file (via ownership or sharing).
    """
    media_file = _check_file_access(db, file_uuid, current_user)
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
):
    """Create a comment for a specific media file (nested route).

    Requires viewer+ permission on the file (commenting is collaborative).
    """
    media_file = _check_file_access(db, file_uuid, current_user)
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


@router.get("", response_model=list[CommentSchema])
def get_comments_for_file(
    media_file_id: str | None = None,
    media_file_uuid: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
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
    media_file = _check_file_access(db, file_ref, current_user)
    media_file_id = media_file.id

    # Get comments for this file (eager-load relationships to avoid N+1)
    comments = (
        db.query(Comment)
        .options(joinedload(Comment.user), joinedload(Comment.media_file))
        .filter(Comment.media_file_id == media_file_id)
        .order_by(Comment.timestamp)
        .all()
    )

    return comments


@router.post("", response_model=CommentSchema)
def create_comment_standalone(
    comment: CommentCreateStandalone,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Add a comment to a media file referenced by UUID in the request body.

    Fallback route used by the frontend when the nested
    ``POST /files/{file_uuid}/comments`` route is unavailable. Requires
    viewer+ permission on the file.
    """
    media_file = _check_file_access(db, str(comment.media_file_id), current_user)
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


@router.get("/{comment_uuid}", response_model=CommentSchema)
def get_comment(
    comment_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get a single comment by UUID.

    Requires viewer+ permission on the comment's file.
    """
    comment = get_comment_by_uuid(db, comment_uuid)

    # Verify the user has access to the comment's file via PermissionService
    if not current_user.is_admin:
        permission = PermissionService.get_file_permission(
            db, int(comment.media_file_id), int(current_user.id)
        )
        if permission is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to view this comment",
            )

    return comment


@router.put("/{comment_uuid}", response_model=CommentSchema)
def update_comment(
    comment_uuid: str,
    comment_update: CommentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Update a comment. Only the comment author can edit."""
    comment = get_comment_by_uuid(db, comment_uuid)

    if not current_user.is_admin and comment.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to edit this comment",
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
):
    """Delete a comment. Admin, comment author, or file owner can delete."""
    comment = get_comment_by_uuid(db, comment_uuid)

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

    # Allow deletion by file owner (project only user_id, not full ORM object)
    owner_row = db.query(MediaFile.user_id).filter(MediaFile.id == comment.media_file_id).first()
    if owner_row and owner_row[0] == current_user.id:
        db.delete(comment)
        db.commit()
        return None

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to delete this comment",
    )
