"""Persisted pipeline-timing reads for the admin timing routes. None of these commit."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.pipeline_timing import FilePipelineTiming


def get_timing_by_task_id(db: Session, task_id: str) -> FilePipelineTiming | None:
    """The persisted timing row for a Celery task, or ``None``."""
    return db.query(FilePipelineTiming).filter(FilePipelineTiming.task_id == task_id).first()


def list_recent_timing_rows(
    db: Session, limit: int, user_id: int | None = None
) -> list[FilePipelineTiming]:
    """The newest timing rows first, optionally one owner's."""
    q = db.query(FilePipelineTiming).order_by(FilePipelineTiming.created_at.desc())
    if user_id is not None:
        q = q.filter(FilePipelineTiming.user_id == user_id)
    return q.limit(limit).all()
