"""Retry policy and recovery queries for transcript search indexing (issue #1182).

A completed transcript that never reaches the search index is invisible to the user and to
every downstream feature that reads the index. ``index_transcript_search_task`` therefore
retries over a long horizon instead of a fixed handful of attempts, and a periodic sweep
(``tasks/search_index_sweep_task``) re-dispatches whatever still slipped through. This module
holds the two shared halves: the backoff arithmetic and the "which files are not indexed"
query that both the sweep and the ``search_indexing_files_awaiting_reindex`` gauge use.

"Not indexed" is read from the ``search_indexing`` task rows in Postgres, not from OpenSearch:
it is one indexed query rather than a full-index aggregation, and it keeps working while the
cluster is the thing that is down. A file's state is its LATEST such row (by last update), so a
success after an earlier failure clears it.
"""

from __future__ import annotations

import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy import and_
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings

SEARCH_INDEXING_TASK_TYPE = "search_indexing"

#: Beyond this exponent the cap has long since won; it only guards ``2 ** n`` overflow.
_MAX_EXPONENT = 30


def _nominal_delay(retries: int) -> int:
    base = max(1, int(settings.SEARCH_INDEX_RETRY_BASE_DELAY_S))
    cap = max(base, int(settings.SEARCH_INDEX_RETRY_MAX_DELAY_S))
    return int(min(cap, base * 2 ** min(max(retries, 0), _MAX_EXPONENT)))


def retry_delay_seconds(retries: int) -> int:
    """Seconds to wait before retry number ``retries`` (0-based): exponential, capped, jittered.

    "Equal jitter": half the nominal delay is fixed and half is random, so concurrent failures
    (a whole wave of files timing out together) spread out instead of retrying in lockstep,
    while the wait never collapses toward zero. ``secrets`` rather than ``random`` for the same
    reason ``indexing_service._retry_failed_docs`` uses it.
    """
    ceiling = _nominal_delay(retries)
    floor = ceiling // 2
    return floor + secrets.randbelow(ceiling - floor + 1)


def max_retry_attempts() -> int:
    """Number of retries that together span ``SEARCH_INDEX_RETRY_HORIZON_S``.

    Computed from the nominal (un-jittered) delays, so the horizon is a property of the
    configuration rather than of how the dice fell.
    """
    horizon = max(1, int(settings.SEARCH_INDEX_RETRY_HORIZON_S))
    total = 0
    attempts = 0
    while total < horizon:
        total += _nominal_delay(attempts)
        attempts += 1
    return attempts


def record_failure(reason: str) -> None:
    """Count a failed indexing attempt. ``reason``: retrying | exhausted | terminal."""
    from app.core.metrics import search_indexing_failures_total

    search_indexing_failures_total.labels(reason=reason).inc()


def record_retry() -> None:
    """Count a scheduled indexing retry."""
    from app.core.metrics import search_indexing_retries_total

    search_indexing_retries_total.inc()


def _latest_index_status() -> Any:
    """Scalar subquery: status of the file's most recently updated ``search_indexing`` row."""
    from app.models.media import MediaFile
    from app.models.media import Task

    return (
        select(Task.status)
        .where(
            Task.media_file_id == MediaFile.id,
            Task.task_type == SEARCH_INDEXING_TASK_TYPE,
        )
        .order_by(func.coalesce(Task.updated_at, Task.created_at).desc(), Task.created_at.desc())
        .limit(1)
        .correlate(MediaFile)
        .scalar_subquery()
    )


def _completed_with_transcript(db: Session, *columns: Any) -> Any:
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment

    has_segments = exists(
        select(TranscriptSegment.id).where(TranscriptSegment.media_file_id == MediaFile.id)
    )
    return db.query(*columns).filter(MediaFile.status == FileStatus.COMPLETED, has_segments)


def _lookback_cutoff() -> datetime:
    return datetime.now(UTC) - timedelta(
        hours=max(1, int(settings.SEARCH_INDEX_SWEEP_LOOKBACK_HOURS))
    )


def _never_attempted(latest: Any) -> Any:
    """Completed recently enough, with no indexing row at all.

    Bounded by the lookback so enabling the sweep on an existing library does not re-index
    every file that predates the task rows; ``search_index_maintenance`` covers those.
    """
    from app.models.media import MediaFile

    return and_(
        latest.is_(None),
        func.coalesce(MediaFile.completed_at, MediaFile.upload_time) >= _lookback_cutoff(),
    )


def find_files_awaiting_reindex(db: Session, *, limit: int) -> list[tuple[int, str, int]]:
    """Completed files whose search indexing failed for good or never ran.

    A file with a row still ``pending`` / ``in_progress`` is owned by a live attempt (or by
    task recovery if its worker died) and is left alone.

    Returns:
        ``(file_id, file_uuid, user_id)`` tuples, oldest file first, at most ``limit``.
    """
    from app.models.media import MediaFile

    latest = _latest_index_status()
    rows = (
        _completed_with_transcript(db, MediaFile.id, MediaFile.uuid, MediaFile.user_id)
        .filter(or_(latest == "failed", _never_attempted(latest)))
        .order_by(MediaFile.id)
        .limit(limit)
        .all()
    )
    return [(int(r[0]), str(r[1]), int(r[2])) for r in rows]


def count_files_awaiting_index(db: Session) -> int:
    """How many completed files are not (yet) searchable: failed, retrying, or never indexed."""
    from app.models.media import MediaFile

    latest = _latest_index_status()
    return (
        _completed_with_transcript(db, func.count(MediaFile.id))
        .filter(or_(latest.in_(("failed", "pending", "in_progress")), _never_attempted(latest)))
        .scalar()
        or 0
    )
