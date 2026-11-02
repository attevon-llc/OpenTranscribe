"""Per-file import history of one watch source: list, stats, delete, batch retry/delete.

Rows are the tracking records (``WatchSourceFile``), not gallery files. Every route resolves
the source through the tenant gate in ``watch_sources_access.py`` (owner or admin).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.watch_sources_access import _get_source_or_404
from app.db.base import get_db
from app.models.user import User
from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile
from app.schemas.watch_source import RETRYABLE_FILE_STATUSES
from app.schemas.watch_source import WatchSourceFileActionRequest
from app.schemas.watch_source import WatchSourceFileActionResponse
from app.schemas.watch_source import WatchSourceFileActionResult
from app.schemas.watch_source import WatchSourceFilesList
from app.schemas.watch_source import WatchSourceStats
from app.services.request_context import RequestContext
from app.utils.sql_like import escape_like

router = APIRouter()


# --------------------------------------------------------------------------- #
# File history
# --------------------------------------------------------------------------- #
@router.get("/{source_uuid}/files", response_model=WatchSourceFilesList)
def list_source_files(
    source_uuid: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status_filter: str | None = Query(None, alias="status"),
    q: str | None = Query(None, max_length=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> dict:
    """Paginated per-file import history for one watch source (owner or admin).

    Consumed by the settings page's history table and by scripts auditing what a
    source did or did not import. ``?status=`` filters on the raw tracking status
    (``imported``, ``pending``, ``importing``, ``downloading``, ``error``,
    ``waiting_for_parts``, or a ``skipped*`` variant carrying ``skip_reason``);
    ``?q=`` is a case-insensitive substring match on the filename, for a source
    tracking more files than anyone can page through looking for one.

    Rows are the tracking records, not gallery files: ``media_file_uuid`` is null for
    anything skipped, errored or still in flight, and stays populated for an imported
    row. Newest first.
    """
    source = _get_source_or_404(db, source_uuid, ctx, need="read")
    # ``selectinload`` because the serialization below reads ``r.media_file`` on every
    # row: lazily that is one query per row, and ``page_size`` goes up to 200.
    query = (
        db.query(WatchSourceFile)
        .options(selectinload(WatchSourceFile.media_file))
        .filter(WatchSourceFile.watch_source_id == source.id)
    )
    if status_filter:
        query = query.filter(WatchSourceFile.status == status_filter)
    if q and q.strip():
        # ``ilike`` with the wildcards escaped, so a filename containing % or _ is
        # searched for literally rather than turning into a match-everything pattern.
        needle = escape_like(q.strip())
        query = query.filter(WatchSourceFile.filename.ilike(f"%{needle}%", escape="\\"))
    total = query.count()
    rows = (
        query.order_by(WatchSourceFile.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    files = []
    for r in rows:
        files.append(
            {
                "uuid": str(r.uuid),
                "remote_path": r.remote_path,
                "filename": r.filename,
                "file_size": r.file_size,
                "file_modified_at": r.file_modified_at,
                "imohash": r.imohash,
                "media_file_uuid": str(r.media_file.uuid) if r.media_file else None,
                "status": r.status,
                "skip_reason": r.skip_reason,
                "part_group": r.part_group,
                "part_number": r.part_number,
                "error_message": r.error_message,
                "retry_count": r.retry_count,
                "processed_at": r.processed_at,
                "created_at": r.created_at,
            }
        )
    return {"files": files, "total": total, "page": page, "page_size": page_size}


@router.get("/{source_uuid}/files/stats", response_model=WatchSourceStats)
def source_file_stats(
    source_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> WatchSourceStats:
    """Import-status counts for one watch source, as one GROUP BY (owner or admin).

    Consumed by the settings page's summary badges and by monitoring scripts that
    want a source's health without paging through ``/files``.

    The buckets are coarser than the stored statuses on purpose: ``skipped`` sums
    every ``skipped*`` variant (there are several ``skip_reason`` flavours and the
    caller rarely cares which), and ``pending`` folds ``pending`` + ``importing`` +
    ``downloading`` into one "in flight" number.
    """
    from sqlalchemy import func

    source = _get_source_or_404(db, source_uuid, ctx, need="read")
    counts: dict[str, int] = {
        status: count
        for status, count in db.query(WatchSourceFile.status, func.count(WatchSourceFile.id))
        .filter(WatchSourceFile.watch_source_id == source.id)
        .group_by(WatchSourceFile.status)
        .all()
    }
    skipped = sum(v for k, v in counts.items() if k.startswith("skipped"))
    return WatchSourceStats(
        total=sum(counts.values()),
        imported=counts.get("imported", 0),
        skipped=skipped,
        error=counts.get("error", 0),
        pending=counts.get("pending", 0)
        + counts.get("importing", 0)
        + counts.get("downloading", 0),
        waiting_for_parts=counts.get("waiting_for_parts", 0),
    )


@router.delete("/{source_uuid}/files/{file_uuid}")
def delete_source_file(
    source_uuid: str,
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> dict:
    """Remove a tracking row (does NOT delete from the source or the gallery)."""
    source = _get_source_or_404(db, source_uuid, ctx)
    row = (
        db.query(WatchSourceFile)
        .filter(
            WatchSourceFile.uuid == file_uuid,
            WatchSourceFile.watch_source_id == source.id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Tracked file not found")
    db.delete(row)
    db.commit()
    return {"success": True}


def _rows_for_action(
    db: Session, source: WatchSource, file_uuids: list[str]
) -> tuple[dict[str, WatchSourceFile], list[WatchSourceFileActionResult]]:
    """Resolve requested uuids to rows of this source, reporting misses per row.

    An unknown or foreign uuid is one row's problem, not the batch's — the caller is
    usually acting on a page of results that another scan may have changed underneath
    them, and failing the whole request would discard the work for every valid row.
    """
    rows = (
        db.query(WatchSourceFile)
        .filter(
            WatchSourceFile.watch_source_id == source.id,
            WatchSourceFile.uuid.in_(file_uuids),
        )
        .all()
    )
    by_uuid = {str(r.uuid): r for r in rows}
    missing = [
        WatchSourceFileActionResult(
            file_uuid=requested, success=False, error="Tracked file not found"
        )
        for requested in file_uuids
        if requested not in by_uuid
    ]
    return by_uuid, missing


@router.post("/{source_uuid}/files/retry", response_model=WatchSourceFileActionResponse)
def retry_source_files(
    source_uuid: str,
    data: WatchSourceFileActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> WatchSourceFileActionResponse:
    """Re-queue tracked files for import, then dispatch ONE scan (owner or admin).

    Consumed by the settings page's file table (its per-row Retry is a batch of one)
    and by scripts clearing a backlog of failures. Resets each eligible row to
    ``pending`` and clears its ``skip_reason``/``error_message``, which is what makes
    a *terminal* row importable again: ``_get_or_create_tracking_row`` refuses to
    reuse a row in a terminal state, so before this there was no way to retry a
    skipped file at all. An ``error`` row was already retried by the next scan; this
    just stops the wait.

    **Batch by design.** ``scan_single`` holds a Redis lock per source, so a per-file
    endpoint would dispatch one scan per file and every one after the first would
    silently no-op. One reset pass plus one dispatch is both correct and what the
    table's "retry all failed" needs.

    ``retry_count`` is deliberately NOT incremented: ``_record_error`` already counts
    each failed attempt, and bumping it here would double-count one.

    **The rows are queued, not retried.** The dispatched scan may find a scan already
    running (the lock), or may not reach this file within ``max_imports_per_scan``, and
    it only re-imports files still present at their ``remote_path``. Callers should
    report "queued" and watch the row's status, not assume the import happened.
    """
    source = _get_source_or_404(db, source_uuid, ctx)
    if not source.is_enabled:
        # 409 rather than the /scan endpoint's 400: this is a conflict with the
        # source's STATE, not a malformed request, and resetting rows for a scan that
        # `_load_scan_plan` will refuse to run would be a lie dressed as success.
        raise HTTPException(status_code=409, detail="Enable the source before retrying its files")

    by_uuid, results = _rows_for_action(db, source, data.file_uuids)
    reset_any = False
    for requested in data.file_uuids:
        row = by_uuid.get(requested)
        if row is None:
            continue
        status_value = str(row.status)
        if status_value not in RETRYABLE_FILE_STATUSES:
            results.append(
                WatchSourceFileActionResult(
                    file_uuid=requested,
                    success=False,
                    status=status_value,
                    error=f"A file in state {status_value!r} cannot be retried",
                )
            )
            continue
        warning = None
        if row.skip_reason == "too_old" and source.skip_files_older_than_days is not None:
            warning = (
                "This file was skipped for age and the source still limits imports to the "
                f"last {source.skip_files_older_than_days} days, so the next scan will skip "
                "it again. Clear that limit first."
            )
        row.status = "pending"
        row.skip_reason = None
        row.error_message = None
        reset_any = True
        results.append(
            WatchSourceFileActionResult(
                file_uuid=requested, success=True, status="pending", warning=warning
            )
        )

    if reset_any:
        db.commit()
        from app.tasks.watch_source_tasks import scan_single

        scan_single.delay(source.id)

    return WatchSourceFileActionResponse(results=results, scan_dispatched=reset_any)


@router.post("/{source_uuid}/files/bulk-delete", response_model=WatchSourceFileActionResponse)
def bulk_delete_source_files(
    source_uuid: str,
    data: WatchSourceFileActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> WatchSourceFileActionResponse:
    """Remove many tracking rows at once (owner or admin).

    Same semantics as the single-row ``DELETE`` beside it — the tracking record goes,
    the file stays in the source and in the library — but clearing a few hundred
    skipped records one confirmation at a time is not a usable admin surface.

    Dispatches no scan: deleting a row is not a request to re-import it. (An untracked
    path does become a candidate again on the next scheduled scan, where content dedup
    decides what happens to it.)
    """
    source = _get_source_or_404(db, source_uuid, ctx)
    by_uuid, results = _rows_for_action(db, source, data.file_uuids)
    for requested in data.file_uuids:
        row = by_uuid.get(requested)
        if row is None:
            continue
        db.delete(row)
        results.append(WatchSourceFileActionResult(file_uuid=requested, success=True))
    db.commit()
    return WatchSourceFileActionResponse(results=results, scan_dispatched=False)
