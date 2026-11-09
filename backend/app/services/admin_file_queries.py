"""Media-file reads for the admin quarantine and retention routes. None of these commit."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.orm import aliased
from sqlalchemy.orm import selectinload

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.user import User


def list_quarantined_files(
    db: Session, *, include_legal_holds: bool, offset: int, limit: int
) -> tuple[list[Any], int]:
    """``(rows, total)`` of quarantined (and optionally held) files with owner context.

    Each row is ``(MediaFile, owner, quarantined_by, Organization)``. A short page already
    tells the total; only a full or empty-past-the-end page pays for a count query.
    """
    owner_alias = aliased(User)
    quarantined_by_alias = aliased(User)
    if include_legal_holds:
        condition = or_(MediaFile.is_quarantined.is_(True), MediaFile.legal_hold.is_(True))
    else:
        condition = MediaFile.is_quarantined.is_(True)

    rows = (
        db.query(MediaFile, owner_alias, quarantined_by_alias, Organization)
        .filter(condition)
        .outerjoin(owner_alias, owner_alias.id == MediaFile.user_id)
        .outerjoin(quarantined_by_alias, quarantined_by_alias.id == MediaFile.quarantined_by)
        .outerjoin(Organization, Organization.id == MediaFile.organization_id)
        .order_by(MediaFile.quarantined_at.desc().nullslast())
        .offset(offset)
        .limit(limit)
        .all()
    )
    if len(rows) < limit and (rows or offset == 0):
        total = offset + len(rows)
    else:
        total = (
            db.query(MediaFile).filter(condition).with_entities(func.count(MediaFile.id)).scalar()
            or 0
        )
    return rows, total


def list_retention_eligible_files(
    db: Session, retention_days: int, delete_error_files: bool, guard: ColumnElement[bool]
) -> list[MediaFile]:
    """Files a retention run would delete.

    ``guard`` is the sweep's own never-take predicate (``tasks.cleanup.retention_guard_pred``),
    passed in by the caller because services do not import tasks.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    eligible_statuses = [FileStatus.COMPLETED]
    if delete_error_files:
        eligible_statuses.append(FileStatus.ERROR)

    return (
        db.query(MediaFile)
        .options(selectinload(MediaFile.speakers))
        .filter(
            or_(
                and_(MediaFile.completed_at.isnot(None), MediaFile.completed_at < cutoff),
                and_(MediaFile.completed_at.is_(None), MediaFile.upload_time < cutoff),
            ),
            MediaFile.status.in_([s.value for s in eligible_statuses]),
            guard,
        )
        .all()
    )
