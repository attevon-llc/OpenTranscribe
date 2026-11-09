"""Comment reads and the one insert behind the comment routes.

Plain ``Session`` helpers: none of them commits, so each route keeps its own
transaction boundary (flush, hold annotation, commit) exactly where it was.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.models.media import Comment
from app.models.media import MediaFile


def list_file_comments(
    db: Session, media_file_id: int, *, order_by_timestamp: bool
) -> list[Comment]:
    """Every comment on a file with author and file loaded (no N+1 when serialised)."""
    query = (
        db.query(Comment)
        .options(joinedload(Comment.user), joinedload(Comment.media_file))
        .filter(Comment.media_file_id == media_file_id)
    )
    if order_by_timestamp:
        query = query.order_by(Comment.timestamp)
    return query.all()


def add_comment(
    db: Session, *, media_file_id: int, user_id: int, text: str, timestamp: float | None
) -> Comment:
    """Stage a new comment and flush it so it has an id; the caller commits."""
    comment = Comment(
        text=text,
        timestamp=timestamp,
        media_file_id=media_file_id,
        user_id=user_id,
    )
    db.add(comment)
    db.flush()
    return comment


def load_comment(db: Session, comment_id: int) -> Comment | None:
    """One comment by primary key with author and file loaded."""
    return (
        db.query(Comment)
        .options(joinedload(Comment.user), joinedload(Comment.media_file))
        .filter(Comment.id == comment_id)
        .first()
    )


def get_comment_file(db: Session, media_file_id: int) -> MediaFile | None:
    """The file a comment hangs off, or None when it is gone."""
    return db.query(MediaFile).filter(MediaFile.id == media_file_id).first()


def file_organization_id(db: Session, media_file_id: int) -> int | None:
    """The organization a file belongs to (None for a personal file or a missing file)."""
    organization_id: int | None = (
        db.query(MediaFile.organization_id).filter(MediaFile.id == media_file_id).scalar()
    )
    return organization_id


def file_org_and_owner(db: Session, media_file_id: int) -> tuple[int | None, int] | None:
    """``(organization_id, owner user_id)`` of a file, or None when it is gone."""
    row = (
        db.query(MediaFile.organization_id, MediaFile.user_id)
        .filter(MediaFile.id == media_file_id)
        .first()
    )
    return None if row is None else (row[0], row[1])


def file_owner_id(db: Session, media_file_id: int) -> int | None:
    """The owning user of a file, or None when it is gone."""
    row = db.query(MediaFile.user_id).filter(MediaFile.id == media_file_id).first()
    return None if row is None else row[0]
