"""File-count queries behind the speaker embedding and attribute migration status endpoints."""

from typing import NamedTuple
from uuid import UUID

from sqlalchemy import case
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.speaker import Speaker


class AttributeMigrationCounts(NamedTuple):
    """Files that have speakers, and how many of them still lack predicted attributes."""

    total: int
    pending: int


def completed_file_ids(db: Session) -> list[int]:
    """Ids of every COMPLETED file."""
    return [
        int(row[0])
        for row in db.query(MediaFile.id).filter(MediaFile.status == FileStatus.COMPLETED).all()
    ]


def completed_file_count(db: Session) -> int:
    """Number of COMPLETED files."""
    return db.query(MediaFile).filter(MediaFile.status == FileStatus.COMPLETED).count()


def completed_file_id_uuid_rows(db: Session) -> list[Row[tuple[int, UUID]]]:
    """``(id, uuid)`` rows of every COMPLETED file, projecting only those two columns."""
    return (
        db.query(MediaFile.id, MediaFile.uuid)
        .filter(MediaFile.status == FileStatus.COMPLETED)
        .all()
    )


def attribute_migration_counts(db: Session) -> AttributeMigrationCounts:
    """One aggregate over COMPLETED files that have speakers.

    ``pending`` counts files with at least one speaker whose attributes were never predicted.
    """
    has_speakers = exists().where(Speaker.media_file_id == MediaFile.id)
    has_unpredicted = exists().where(
        (Speaker.media_file_id == MediaFile.id) & (Speaker.attributes_predicted_at.is_(None))
    )
    row = (
        db.query(
            func.count(MediaFile.id).label("total"),
            func.sum(case((has_unpredicted, 1), else_=0)).label("pending"),
        )
        .filter(MediaFile.status == FileStatus.COMPLETED, has_speakers)
        .one()
    )
    return AttributeMigrationCounts(total=row.total or 0, pending=int(row.pending or 0))
