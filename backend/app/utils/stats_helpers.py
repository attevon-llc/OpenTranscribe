"""
Consolidated database statistics helpers.

Replaces 15+ individual COUNT queries with 3 efficient aggregate queries
using PostgreSQL FILTER (WHERE ...) clauses for conditional counting.
Also provides processing throughput, ETA, file timing, queue depth,
and model info helpers for the system stats page.
"""

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy import extract
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import Task
from app.models.media import TranscriptSegment
from app.models.user import User
from app.utils.task_utils import TASK_STATUS_COMPLETED
from app.utils.task_utils import TASK_STATUS_FAILED
from app.utils.task_utils import TASK_STATUS_IN_PROGRESS
from app.utils.task_utils import TASK_STATUS_PENDING

logger = logging.getLogger(__name__)


def format_bytes(byte_count: float) -> str:
    """Format a byte count as a human-readable string with a size suffix.

    The one canonical implementation for system/GPU stats display — previously
    duplicated (byte-for-byte identical behavior) between
    ``api/endpoints/admin.py`` and ``tasks/utility.py``.

    Args:
        byte_count: Size in bytes.

    Returns:
        Formatted string, e.g. ``"2.50 GB"``.
    """
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if byte_count < 1024 or unit == "TB":
            return f"{byte_count:.2f} {unit}"
        byte_count /= 1024
    return f"{byte_count:.2f} TB"


def get_user_stats(db: Session, *, include_breakdown: bool = False) -> dict[str, Any]:
    """Get user statistics in a single query.

    Args:
        db: Database session
        include_breakdown: If True, include active/inactive/superuser counts (admin only)

    Returns:
        Dictionary with user statistics
    """
    seven_days_ago = datetime.now(UTC) - timedelta(days=7)

    if include_breakdown:
        row = db.query(
            func.count().label("total"),
            func.count().filter(User.is_active.is_(True)).label("active"),
            func.count().filter(User.is_superuser.is_(True)).label("superuser"),
            func.count().filter(User.created_at >= seven_days_ago).label("new"),
        ).first()
        if not row:
            return {"total": 0, "active": 0, "inactive": 0, "superusers": 0, "new": 0}

        return {
            "total": row.total,
            "active": row.active,
            "inactive": row.total - row.active,
            "superusers": row.superuser,
            "new": row.new,
        }
    else:
        row = db.query(  # type: ignore[assignment]
            func.count().label("total"),
            func.count().filter(User.created_at >= seven_days_ago).label("new"),
        ).first()
        if not row:
            return {"total": 0, "new": 0}

        return {"total": row.total, "new": row.new}


def get_file_stats(db: Session, *, include_status_breakdown: bool = False) -> dict[str, Any]:
    """Get file statistics in a single query.

    Consolidates total, new files, duration, size, segments, speakers, and
    optional per-status counts into one aggregate query.

    Args:
        db: Database session
        include_status_breakdown: If True, include per-status counts (admin only)

    Returns:
        Dictionary with file statistics
    """
    seven_days_ago = datetime.now(UTC) - timedelta(days=7)

    columns = [
        func.count().label("total"),
        func.count().filter(MediaFile.upload_time >= seven_days_ago).label("new"),
        func.coalesce(func.sum(MediaFile.duration), 0).label("total_duration"),
        func.coalesce(func.sum(MediaFile.file_size), 0).label("total_size"),
    ]

    if include_status_breakdown:
        columns.extend(
            [
                func.count().filter(MediaFile.status == "pending").label("pending"),
                func.count().filter(MediaFile.status == "processing").label("processing"),
                func.count().filter(MediaFile.status == "completed").label("completed"),
                func.count().filter(MediaFile.status == "error").label("error"),
            ]
        )

    row = db.query(*columns).first()  # type: ignore[call-overload]

    # Separate queries for related table counts (cheap — indexes exist)
    total_segments = db.query(func.count(TranscriptSegment.id)).scalar() or 0
    total_speakers = db.query(func.count(Speaker.id)).scalar() or 0

    result: dict[str, Any] = {
        "total": row.total if row else 0,
        "new": row.new if row else 0,
        "total_duration": round(float(row.total_duration), 2) if row and row.total_duration else 0,
        "total_size": int(row.total_size) if row and row.total_size else 0,
        "segments": total_segments,
        "speakers": total_speakers,
    }

    if include_status_breakdown and row:
        result["by_status"] = {
            "pending": row.pending,
            "processing": row.processing,
            "completed": row.completed,
            "error": row.error,
        }

    return result


def get_task_stats(db: Session) -> dict[str, Any]:
    """Get task statistics in a single query.

    Uses SQL conditional aggregation for status counts and SQL AVG for
    processing time instead of loading all completed Task objects into Python.

    Args:
        db: Database session

    Returns:
        Dictionary with task statistics
    """
    row = db.query(
        func.count().label("total"),
        func.count().filter(Task.status == TASK_STATUS_PENDING).label("pending"),
        func.count().filter(Task.status == TASK_STATUS_IN_PROGRESS).label("running"),
        func.count().filter(Task.status == TASK_STATUS_COMPLETED).label("completed"),
        func.count().filter(Task.status == TASK_STATUS_FAILED).label("failed"),
        func.avg(extract("epoch", Task.completed_at - Task.created_at))
        .filter(
            Task.status == TASK_STATUS_COMPLETED,
            Task.completed_at.isnot(None),
            Task.created_at.isnot(None),
        )
        .label("avg_processing_time"),
    ).first()

    if not row or row.total == 0:
        return {
            "total": 0,
            "pending": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "success_rate": 0,
            "avg_processing_time": 0,
        }

    success_rate = round((row.completed / row.total) * 100, 2) if row.total > 0 else 0

    return {
        "total": row.total,
        "pending": row.pending,
        "running": row.running,
        "completed": row.completed,
        "failed": row.failed,
        "success_rate": success_rate,
        "avg_processing_time": round(float(row.avg_processing_time or 0), 2),
    }


def get_recent_tasks(db: Session, limit: int = 10) -> list[dict[str, Any]]:
    """Get the most recent tasks with elapsed time.

    Args:
        db: Database session
        limit: Maximum number of tasks to return

    Returns:
        List of task dictionaries
    """
    from sqlalchemy.orm import load_only

    recent_tasks = (
        db.query(Task)
        .options(
            load_only(
                Task.id,  # type: ignore[arg-type]
                Task.task_type,  # type: ignore[arg-type]
                Task.status,  # type: ignore[arg-type]
                Task.created_at,  # type: ignore[arg-type]
                Task.completed_at,  # type: ignore[arg-type]
            )
        )
        .order_by(Task.created_at.desc())
        .limit(limit)
        .all()
    )

    now = datetime.now(UTC)
    result = []
    for task in recent_tasks:
        if task.completed_at and task.created_at:
            elapsed = (task.completed_at - task.created_at).total_seconds()
        elif task.created_at:
            created_at = task.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            elapsed = (now - created_at).total_seconds()
        else:
            elapsed = 0

        result.append(
            {
                "id": task.id,
                "type": getattr(task, "task_type", ""),
                "status": task.status,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "elapsed": int(elapsed) if elapsed else 0,
            }
        )

    return result


def get_throughput_stats(db: Session) -> dict[str, Any]:
    """Get processing throughput using rolling 1h and 3h windows.

    Ported from scripts/bulk-processing-cheatsheet.sh bulk-throughput.

    Args:
        db: Database session

    Returns:
        Dictionary with completion counts and files/hour rates
    """
    now = datetime.now(UTC)
    one_hour_ago = now - timedelta(hours=1)
    three_hours_ago = now - timedelta(hours=3)

    row = (
        db.query(
            func.count().label("total_completed"),
            func.count().filter(MediaFile.completed_at > one_hour_ago).label("last_1h"),
            func.count().filter(MediaFile.completed_at > three_hours_ago).label("last_3h"),
        )
        .filter(
            MediaFile.status == "completed",
            MediaFile.completed_at.isnot(None),
        )
        .first()
    )

    if not row:
        return {
            "total_completed": 0,
            "last_1h": 0,
            "last_3h": 0,
            "rate_1h": 0,
            "rate_3h": 0,
        }

    return {
        "total_completed": row.total_completed,
        "last_1h": row.last_1h,
        "last_3h": row.last_3h,
        "rate_1h": row.last_1h,
        "rate_3h": round(row.last_3h / 3.0, 1) if row.last_3h else 0,
    }


def get_processing_eta(db: Session) -> dict[str, Any]:
    """Calculate ETA for remaining files based on 3h rolling throughput.

    Ported from scripts/bulk-processing-cheatsheet.sh bulk-eta.

    Args:
        db: Database session

    Returns:
        Dictionary with remaining count, files/hour, hours remaining, est completion
    """
    three_hours_ago = datetime.now(UTC) - timedelta(hours=3)

    completed_3h = (
        db.query(func.count())
        .filter(
            MediaFile.status == "completed",
            MediaFile.completed_at.isnot(None),
            MediaFile.completed_at > three_hours_ago,
        )
        .scalar()
    ) or 0

    remaining = (
        db.query(func.count())
        .filter(
            MediaFile.file_size > 0,
            MediaFile.status.in_(["pending", "processing"]),
        )
        .scalar()
    ) or 0

    files_per_hour = round(completed_3h / 3.0, 1) if completed_3h > 0 else 0
    hours_remaining = round(remaining / files_per_hour, 1) if files_per_hour > 0 else None
    est_completion = None
    if hours_remaining is not None:
        est_completion = (datetime.now(UTC) + timedelta(hours=hours_remaining)).isoformat()

    return {
        "remaining": remaining,
        "files_per_hour": files_per_hour,
        "hours_remaining": hours_remaining,
        "est_completion": est_completion,
    }


def get_file_timing_stats(db: Session) -> dict[str, Any]:
    """Get per-file processing timing from the task table.

    Ported from scripts/bulk-processing-cheatsheet.sh bulk-file-timing.

    Args:
        db: Database session

    Returns:
        Dictionary with avg/min/max processing seconds for transcription tasks
    """
    row = (
        db.query(
            func.count().label("files"),
            func.avg(extract("epoch", Task.completed_at - Task.created_at)).label("avg_secs"),
            func.min(extract("epoch", Task.completed_at - Task.created_at)).label("min_secs"),
            func.max(extract("epoch", Task.completed_at - Task.created_at)).label("max_secs"),
        )
        .filter(
            Task.task_type == "transcription",
            Task.status == TASK_STATUS_COMPLETED,
            Task.completed_at.isnot(None),
            Task.created_at.isnot(None),
        )
        .first()
    )

    if not row or not row.files:
        return {"files": 0, "avg_secs": 0, "min_secs": 0, "max_secs": 0, "avg_mins": 0}

    return {
        "files": row.files,
        "avg_secs": round(float(row.avg_secs or 0)),
        "min_secs": round(float(row.min_secs or 0)),
        "max_secs": round(float(row.max_secs or 0)),
        "avg_mins": round(float(row.avg_secs or 0) / 60, 1),
    }


def get_queue_depths() -> dict[str, int]:
    """Get Celery queue depths from Redis using LLEN.

    Ported from scripts/bulk-processing-cheatsheet.sh bulk-queues.

    Returns:
        Dictionary mapping queue name to pending task count
    """
    from app.core.constants import CeleryQueues

    try:
        from app.core.celery import celery_app

        redis_client = celery_app.backend.client
        queues = CeleryQueues.ALL
        depths: dict[str, int] = {}
        for queue_name in queues:
            try:
                depths[queue_name] = redis_client.llen(queue_name) or 0
            except Exception:
                depths[queue_name] = 0

        depths["total"] = sum(depths.values())
        return depths
    except Exception as e:
        logger.error(f"Error getting queue depths: {e}")
        return {q: 0 for q in CeleryQueues.ALL} | {"total": 0}


def get_models_info() -> dict[str, dict[str, Any]]:
    """Get accurate AI model configuration for display.

    Reads actual model names from their definitive sources rather
    than relying on settings defaults (which may be stale).

    Returns:
        Dictionary of model entries with name, description, purpose (plus, for
        "diarization", the configured-vs-effective fields below).
    """
    from app.core.config import settings

    # Whisper model: directly from settings (accurate)
    whisper_name = settings.WHISPER_MODEL

    # Diarization: report the engine actually SERVING, not merely configured (issue #672,
    # both halves). "Configured to serve" was already wrong once — a hardcoded "PyAnnote
    # 3.1" claimed that engine on every install regardless of diarizer_backend — and fixing
    # that by reporting the configured value was not enough either: ModelManager falls back
    # from native to the in-process PyAnnote fork silently whenever the sidecar can't serve,
    # so a deployment configured for native can spend its whole life on PyAnnote while this
    # panel kept saying "native". describe_diarizer_status() is the SINGLE resolver for
    # both this panel and /admin/stats (app/api/endpoints/admin.py) — before this it was a
    # second, hand-rolled copy of the same lookup with its own description table, and the
    # two disagreed under a bad ENGINE_DIARIZER_BACKEND value.
    #
    # The weights are the same either way (both engines serve community-1), so the model
    # name does not vary with the engine; only the description/backend fields do.
    diarization_name = "pyannote/speaker-diarization-community-1"
    try:
        from app.transcription.diarizer_native import describe_diarizer_status

        diarizer_status = describe_diarizer_status()
    except Exception:  # noqa: BLE001 — a stats panel never fails over a display value
        diarizer_status = {
            "configured": "unknown",
            "configured_description": "unknown diarization engine",
            "effective": "unknown",
            "effective_description": "unknown diarization engine",
            "using_fallback": False,
        }

    models: dict[str, dict[str, Any]] = {
        "whisper": {
            "name": whisper_name,
            "description": f"Whisper {whisper_name}",
            "purpose": "Speech Recognition & Transcription",
        },
        "diarization": {
            # "description" reports the EFFECTIVE engine — the one actually running —
            # since that is what this panel exists to answer; "configured_backend" and
            # "using_fallback" carry the other half so a caller can tell "native, and
            # it's working" from "native, and I'm silently on the fallback".
            "name": diarization_name,
            "description": diarizer_status["effective_description"],
            "purpose": "Speaker Identification & Segmentation",
            "configured_backend": diarizer_status["configured"],
            "configured_description": diarizer_status["configured_description"],
            "effective_backend": diarizer_status["effective"],
            "using_fallback": diarizer_status["using_fallback"],
        },
    }

    # Search/embedding model (read from DB for current selection, not stale env var)
    if getattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False):
        try:
            from app.services.search.settings_service import get_search_embedding_model

            neural_model = get_search_embedding_model()
        except Exception:
            neural_model = getattr(settings, "OPENSEARCH_NEURAL_MODEL", "")
        if neural_model:
            short_name = neural_model.rsplit("/", 1)[-1] if "/" in neural_model else neural_model
            models["search_embedding"] = {
                "name": short_name,
                "description": f"OpenSearch Neural Search ({neural_model})",
                "purpose": "Semantic Search & Vector Embeddings",
            }

    # LLM provider (system-level config)
    llm_provider = getattr(settings, "LLM_PROVIDER", "")
    if llm_provider:
        model_name = ""
        provider_map = {
            "vllm": "VLLM_MODEL_NAME",
            "openai": "OPENAI_MODEL_NAME",
            "ollama": "OLLAMA_MODEL_NAME",
            "anthropic": "ANTHROPIC_MODEL_NAME",
            "openrouter": "OPENROUTER_MODEL_NAME",
        }
        attr = provider_map.get(llm_provider, "")
        if attr:
            model_name = getattr(settings, attr, "")

        models["llm"] = {
            "name": model_name or "User-configured",
            "description": f"{llm_provider.title()} LLM Provider",
            "purpose": "Summarization & Speaker Identification",
        }

    return models
