"""Celery priorities for NLP and download work, with per-user fairness (concurrency audit M11).

Transcription already demotes an owner's runs past their fair share
(``services/transcription_fairness.py``). Everything downstream did not: every summary went
in at ``NLPPriority.USER_TRIGGERED`` whether one person asked for it or a bulk action asked
for fifty, and every URL download at ``DownloadPriority.SINGLE_URL``. Within one priority the
Redis transport is FIFO, so one user's batch sat in front of everyone else's single request.

The same two mechanisms, on the same priority scheme (``core/queues.py``; Celery, "Routing
Tasks: Redis Message Priorities"):

* **Bulk work** (a bulk action over more than one file) goes in at the queue's batch
  priority, behind any single request.
* **An owner past their fair share** (``over_fair_share``: their active transcriptions
  reached ``transcription.fair_share_threshold``) has their NLP work go in one step lower,
  as their pipeline stages already do; for downloads the backlog is their in-flight URL
  imports, measured against the same threshold.

Like transcription fairness, a priority is decided once, at dispatch.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.enums import FileStatus
from app.core.queues import DownloadPriority
from app.core.queues import NLPPriority
from app.models.media import MediaFile
from app.services.transcription_fairness import fair_share_threshold
from app.services.transcription_fairness import over_fair_share

#: A URL import still downloading or being processed.
_IN_FLIGHT_URL_STATUSES = (
    FileStatus.PENDING,
    FileStatus.QUEUED,
    FileStatus.DOWNLOADING,
    FileStatus.PROCESSING,
)


def nlp_priority(db: Session, user_id: int | None, *, base: int, bulk: bool = False) -> int:
    """Priority for one NLP task dispatched for ``user_id``.

    Args:
        db: Session for the fairness read.
        user_id: Owner of the work; ``None`` skips the fairness check.
        base: The priority this kind of request normally gets
            (``NLPPriority.USER_TRIGGERED`` or ``NLPPriority.AUTO_PIPELINE``).
        bulk: Part of a multi-file bulk action.
    """
    if bulk:
        return max(base, NLPPriority.ADMIN_BATCH)
    if user_id is not None and over_fair_share(db, int(user_id)):
        return min(base + 1, NLPPriority.BACKGROUND)
    return base


def active_url_imports(db: Session, user_id: int, *, excluding_file_id: int | None = None) -> int:
    """How many of ``user_id``'s URL imports are still downloading or processing."""
    query = db.query(func.count(MediaFile.id)).filter(
        MediaFile.user_id == user_id,
        MediaFile.source_url.isnot(None),
        MediaFile.status.in_(_IN_FLIGHT_URL_STATUSES),
    )
    if excluding_file_id is not None:
        query = query.filter(MediaFile.id != excluding_file_id)
    return int(query.scalar() or 0)


def download_priority(db: Session, user_id: int, *, new_file_id: int | None = None) -> int:
    """Priority for one single-URL download.

    ``new_file_id`` is the import being dispatched, left out of the count so that, as for
    transcription, the owner's first ``threshold`` imports keep the normal priority.
    """
    threshold = fair_share_threshold(db)
    active = active_url_imports(db, user_id, excluding_file_id=new_file_id)
    if threshold > 0 and active >= threshold:
        return DownloadPriority.PLAYLIST
    return DownloadPriority.SINGLE_URL
