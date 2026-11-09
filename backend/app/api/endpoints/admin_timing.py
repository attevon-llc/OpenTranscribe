"""Admin endpoints for inspecting end-to-end pipeline timing.

Exposes the ``benchmark:{task_id}`` Redis hash and the durable
``file_pipeline_timing`` table. See ``docs/PIPELINE_TIMING.md`` for the
marker reference and interpretation notes.

Guarded by the standard admin dependency — the timing data can include
file titles and other metadata that should not be exposed to regular users.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_admin_user
from app.db.base import get_db
from app.models.pipeline_timing import FilePipelineTiming
from app.models.user import User
from app.services.pipeline_timing_service import build_row_payload
from app.utils import benchmark_timing

logger = logging.getLogger(__name__)

router = APIRouter()


def _row_to_dict(row: FilePipelineTiming) -> dict[str, Any]:
    """Serialize a ``FilePipelineTiming`` row into a JSON-friendly dict."""
    data: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if column.name == "created_at" and value is not None:
            data[column.name] = value.isoformat()
        else:
            data[column.name] = value
    return data


@router.get("/timing/{task_id}")
def get_timing(
    task_id: str,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return all timing data for a single task_id.

    Merges the live Redis ``benchmark:{task_id}`` hash (volatile, 24h TTL)
    with the durable ``file_pipeline_timing`` row (persisted at
    postprocess_end). When both are present, derived durations are
    recomputed from the Redis hash.
    """
    try:
        raw = benchmark_timing.fetch_all_strict(task_id)
    except RedisError as exc:
        logger.exception("Timing read for task_id=%s failed: Redis unavailable", task_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Timing store (Redis) is unavailable; this is not the same as no data.",
        ) from exc
    persisted = db.query(FilePipelineTiming).filter(FilePipelineTiming.task_id == task_id).first()

    if not raw and not persisted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No timing data found for task_id={task_id}",
        )

    response: dict[str, Any] = {
        "task_id": task_id,
        "source": [],
        "markers_raw": raw,
        "derived_from_redis": build_row_payload(raw) if raw else {},
        "persisted": _row_to_dict(persisted) if persisted else None,
    }
    if raw:
        response["source"].append("redis")
    if persisted:
        response["source"].append("postgres")
    return response


@router.get("/timing")
def list_recent_timings(
    limit: int = Query(50, ge=1, le=500),
    user_id: int | None = Query(None, description="Filter by owning user"),
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the most recent persisted timing rows, most-recent first."""
    q = db.query(FilePipelineTiming).order_by(FilePipelineTiming.created_at.desc())
    if user_id is not None:
        q = q.filter(FilePipelineTiming.user_id == user_id)
    rows = q.limit(limit).all()
    return {
        "count": len(rows),
        "items": [_row_to_dict(r) for r in rows],
    }


@router.get("/timing-summary/recent")
def list_recent_timings_summary(
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compact projection — headline durations + context only.

    Useful for dashboards that don't need the 30+ individual markers.
    """
    rows = (
        db.query(FilePipelineTiming)
        .order_by(FilePipelineTiming.created_at.desc())
        .limit(limit)
        .all()
    )
    items = []
    for r in rows:
        items.append(
            {
                "task_id": r.task_id,
                "file_id": r.file_id,
                "user_id": r.user_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "audio_duration_s": r.audio_duration_s,
                "file_size_bytes": r.file_size_bytes,
                "whisper_model": r.whisper_model,
                "asr_provider": r.asr_provider,
                "http_flow": r.http_flow,
                "user_perceived_duration_ms": r.user_perceived_duration_ms,
                "fully_indexed_duration_ms": r.fully_indexed_duration_ms,
                "cpu_worker_cold": r.cpu_worker_cold,
                "gpu_worker_cold": r.gpu_worker_cold,
                "concurrent_files_at_dispatch": r.concurrent_files_at_dispatch,
            }
        )
    return {"count": len(items), "items": items}
