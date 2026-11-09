"""Queries behind the watch-source routes: sources, and the per-file import history."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile
from app.utils.sql_like import escape_like


def find_source(db: Session, source_uuid: str) -> WatchSource | None:
    """The watch source with this uuid, whoever owns it (callers apply the access rules)."""
    return db.query(WatchSource).filter(WatchSource.uuid == source_uuid).first()


def list_sources(
    db: Session, *, user_id: int, organization_id: int | None, all_owners: bool
) -> list[WatchSource]:
    """Sources in the ACTIVE tenant (NULL-safe: personal = org-less), newest first.

    ``all_owners`` widens the owner half only; the tenant filter always applies.
    """
    query = db.query(WatchSource)
    org_filter = (
        WatchSource.organization_id.is_(None)
        if organization_id is None
        else WatchSource.organization_id == organization_id
    )
    if all_owners:
        query = query.filter(org_filter)
    else:
        query = query.filter(WatchSource.user_id == user_id, org_filter)
    return query.order_by(WatchSource.created_at.desc()).all()


def list_file_history(
    db: Session,
    source_id: int,
    *,
    status: str | None,
    name_contains: str | None,
    page: int,
    page_size: int,
) -> tuple[list[WatchSourceFile], int]:
    """A page of the source's tracking rows, newest first, and the filtered total.

    ``name_contains`` is a case-insensitive substring match with the wildcards escaped, so
    a filename containing ``%`` or ``_`` is searched for literally. The linked media file is
    loaded eagerly because the caller reads it on every row (``page_size`` goes up to 200).
    """
    query = (
        db.query(WatchSourceFile)
        .options(selectinload(WatchSourceFile.media_file))
        .filter(WatchSourceFile.watch_source_id == source_id)
    )
    if status:
        query = query.filter(WatchSourceFile.status == status)
    if name_contains and name_contains.strip():
        needle = escape_like(name_contains.strip())
        query = query.filter(WatchSourceFile.filename.ilike(f"%{needle}%", escape="\\"))
    total = query.count()
    rows = (
        query.order_by(WatchSourceFile.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return rows, total


def file_status_counts(db: Session, source_id: int) -> dict[str, int]:
    """Tracking rows of the source grouped by stored status."""
    return {
        status: count
        for status, count in db.query(WatchSourceFile.status, func.count(WatchSourceFile.id))
        .filter(WatchSourceFile.watch_source_id == source_id)
        .group_by(WatchSourceFile.status)
        .all()
    }


def find_tracked_file(db: Session, source_id: int, file_uuid: str) -> WatchSourceFile | None:
    """The tracking row with this uuid within this source, or ``None``."""
    return (
        db.query(WatchSourceFile)
        .filter(WatchSourceFile.uuid == file_uuid, WatchSourceFile.watch_source_id == source_id)
        .first()
    )


def tracked_files_by_uuid(
    db: Session, source_id: int, file_uuids: list[str]
) -> list[WatchSourceFile]:
    """The source's tracking rows among ``file_uuids``."""
    return (
        db.query(WatchSourceFile)
        .filter(
            WatchSourceFile.watch_source_id == source_id,
            WatchSourceFile.uuid.in_(file_uuids),
        )
        .all()
    )
