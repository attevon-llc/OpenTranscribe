"""Single file-to-tag associations: look up, add once, remove.

Callers decide who may change a file's tags; these helpers only stage the
change in the session. The caller flushes (or commits) it.

A held file's tags are part of its preserved record (legal-hold freeze): both writers raise
``HoldRefusedError`` for one. A route refuses (and audits) before it gets here; this is the
backstop every caller shares.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.constants import TAG_SOURCE_MANUAL
from app.models.tag import FileTag
from app.models.tag import Tag
from app.services.hold_guard import assert_content_mutable


def add_file_tag(db: Session, file_id: int, tag_id: int) -> bool:
    """Stage a manual association of the tag with the file unless it already exists.

    The row is added through the ORM session so ``before_flush`` hooks see it.

    Returns:
        True when an association was staged.

    Raises:
        HoldRefusedError: The file is under a legal hold.
    """
    assert_content_mutable(db, [file_id])
    present = (
        db.query(FileTag.id)
        .filter(FileTag.media_file_id == file_id, FileTag.tag_id == tag_id)
        .first()
    )
    if present is not None:
        return False
    db.add(FileTag(media_file_id=file_id, tag_id=tag_id, source=TAG_SOURCE_MANUAL))
    return True


def remove_file_tag(db: Session, file_id: int, tag_id: int) -> bool:
    """Stage removal of the file's association with the tag; the tag row is kept.

    Returns:
        True when an association was staged for deletion.

    Raises:
        HoldRefusedError: The file is under a legal hold.
    """
    assert_content_mutable(db, [file_id])
    link = (
        db.query(FileTag).filter(FileTag.media_file_id == file_id, FileTag.tag_id == tag_id).first()
    )
    if link is None:
        return False
    db.delete(link)
    return True


def attached_tag_named(db: Session, file_id: int, name: str) -> Tag | None:
    """The tag on this file whose stored name is exactly ``name`` (case-sensitive).

    Only the file's own associations are searched, so two accounts' same-named
    tags never interfere.
    """
    tag: Tag | None = (
        db.query(Tag)
        .join(FileTag, FileTag.tag_id == Tag.id)
        .filter(FileTag.media_file_id == file_id, Tag.name == name)
        .order_by(Tag.id)
        .first()
    )
    return tag
