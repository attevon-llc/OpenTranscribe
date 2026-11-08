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
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.media import Task
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
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


# Below this many rows an exact count is cheap, so the dashboard keeps showing exact numbers on
# small installs; above it a full count of a 10M-row table costs ~700 ms per poll.
EXACT_COUNT_BELOW = 100_000


def table_row_counts(
    db: Session, *models: Any, exact_below: int = EXACT_COUNT_BELOW
) -> list[tuple[int, bool]]:
    """Row counts of the models' tables, estimated from planner statistics when large.

    ``pg_class.reltuples`` is the planner's own row estimate, refreshed by autovacuum and
    ``ANALYZE`` (https://www.postgresql.org/docs/17/catalog-pg-class.html); reading it is a
    primary-key lookup instead of a scan. It is ``-1`` before the first vacuum/analyze, and a
    count below ``exact_below`` is cheap, so both of those fall back to ``count(*)``, run for
    all the small tables in ONE statement. ``to_regclass`` resolves each name through
    ``search_path`` exactly like the application's own queries do.

    Returns:
        ``(count, is_estimate)`` per model, in argument order.
    """
    names = [m.__tablename__ for m in models]
    rows = db.execute(
        text(
            "SELECT n, (SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(n)) "
            "FROM unnest(CAST(:names AS text[])) AS n"
        ),
        {"names": names},
    ).all()
    by_name: dict[str, int | None] = {row[0]: row[1] for row in rows}
    estimates = [by_name.get(n) for n in names]
    result: list[tuple[int, bool]] = [(0, False)] * len(models)
    exact: list[int] = []
    for i, estimate in enumerate(estimates):
        if estimate is not None and estimate >= exact_below:
            result[i] = (int(estimate), True)
        else:
            exact.append(i)
    if exact:
        # Scalar subqueries, not bare counts: two FROM tables in one SELECT would cross-join.
        row = db.query(*(select(func.count(models[i].id)).scalar_subquery() for i in exact)).one()
        for i, n in zip(exact, row, strict=True):
            result[i] = (int(n or 0), False)
    return result


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
    from app.services.file_availability import unavailable_pred

    seven_days_ago = datetime.now(UTC) - timedelta(days=7)

    columns = [
        func.count().label("total"),
        func.count().filter(MediaFile.upload_time >= seven_days_ago).label("new"),
        func.coalesce(func.sum(MediaFile.duration), 0).label("total_duration"),
        func.coalesce(func.sum(MediaFile.file_size), 0).label("total_size"),
        # Trashed files (issue #1101) still hold storage until their purge, so the totals keep
        # them and these say how much of it is waiting in the trash.
        func.count().filter(unavailable_pred(include_quarantined=True)).label("trashed_files"),
        func.coalesce(
            func.sum(MediaFile.file_size).filter(unavailable_pred(include_quarantined=True)), 0
        ).label("trashed_bytes"),
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

    # Segment and speaker tables reach millions of rows: an exact count is a ~700 ms scan per
    # poll (#1337 A1), so large tables report the planner's estimate and say so.
    (total_segments, segments_estimated), (total_speakers, speakers_estimated) = table_row_counts(
        db, TranscriptSegment, Speaker
    )

    result: dict[str, Any] = {
        "total": row.total if row else 0,
        "new": row.new if row else 0,
        "total_duration": round(float(row.total_duration), 2) if row and row.total_duration else 0,
        "total_size": int(row.total_size) if row and row.total_size else 0,
        "trashed_files": int(row.trashed_files) if row else 0,
        "trashed_bytes": int(row.trashed_bytes) if row and row.trashed_bytes else 0,
        "segments": total_segments,
        "speakers": total_speakers,
        "segments_estimated": segments_estimated,
        "speakers_estimated": speakers_estimated,
    }

    if include_status_breakdown and row:
        result["by_status"] = {
            "pending": row.pending,
            "processing": row.processing,
            "completed": row.completed,
            "error": row.error,
        }

    return result


def _task_aggregate_row(db: Session) -> Any:
    """One statement over ``task`` for both the status counts and the transcription timing."""
    transcribed = (
        Task.task_type == "transcription",
        Task.status == TASK_STATUS_COMPLETED,
        Task.completed_at.isnot(None),
        Task.created_at.isnot(None),
    )
    elapsed = extract("epoch", Task.completed_at - Task.created_at)
    return db.query(
        func.count().label("total"),
        func.count().filter(Task.status == TASK_STATUS_PENDING).label("pending"),
        func.count().filter(Task.status == TASK_STATUS_IN_PROGRESS).label("running"),
        func.count().filter(Task.status == TASK_STATUS_COMPLETED).label("completed"),
        func.count().filter(Task.status == TASK_STATUS_FAILED).label("failed"),
        func.avg(elapsed)
        .filter(
            Task.status == TASK_STATUS_COMPLETED,
            Task.completed_at.isnot(None),
            Task.created_at.isnot(None),
        )
        .label("avg_processing_time"),
        func.count().filter(*transcribed).label("timing_files"),
        func.avg(elapsed).filter(*transcribed).label("avg_secs"),
        func.min(elapsed).filter(*transcribed).label("min_secs"),
        func.max(elapsed).filter(*transcribed).label("max_secs"),
    ).first()


def get_task_and_timing_stats(db: Session) -> tuple[dict[str, Any], dict[str, Any]]:
    """Task status counts and per-file timing from a single scan of ``task``.

    ``/system/stats`` used to run these as two statements over the same table (#1337 A2).

    Returns:
        ``(task_stats, file_timing_stats)``.
    """
    row = _task_aggregate_row(db)

    task_stats: dict[str, Any]
    if not row or row.total == 0:
        task_stats = {
            "total": 0,
            "pending": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "success_rate": 0,
            "avg_processing_time": 0,
        }
    else:
        task_stats = {
            "total": row.total,
            "pending": row.pending,
            "running": row.running,
            "completed": row.completed,
            "failed": row.failed,
            "success_rate": round((row.completed / row.total) * 100, 2),
            "avg_processing_time": round(float(row.avg_processing_time or 0), 2),
        }

    timing: dict[str, Any]
    if not row or not row.timing_files:
        timing = {"files": 0, "avg_secs": 0, "min_secs": 0, "max_secs": 0, "avg_mins": 0}
    else:
        timing = {
            "files": row.timing_files,
            "avg_secs": round(float(row.avg_secs or 0)),
            "min_secs": round(float(row.min_secs or 0)),
            "max_secs": round(float(row.max_secs or 0)),
            "avg_mins": round(float(row.avg_secs or 0) / 60, 1),
        }
    return task_stats, timing


def get_task_stats(db: Session) -> dict[str, Any]:
    """Task status counts, success rate and average processing time (admin dashboard)."""
    return get_task_and_timing_stats(db)[0]


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
                Task.id,
                Task.task_type,
                Task.status,
                Task.created_at,
                Task.completed_at,
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


def eta_from_counts(completed_3h: int, remaining: int) -> dict[str, Any]:
    """ETA for ``remaining`` files at the last three hours' completion rate (pure arithmetic)."""
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


def get_throughput_and_eta(db: Session) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rolling 1h/3h completion throughput and the remaining-work ETA in ONE ``media_file`` scan.

    Ported from scripts/bulk-processing-cheatsheet.sh bulk-throughput / bulk-eta. The three-hour
    completed count is the same number for both, so they share it (#1337 A2: three statements
    became one).

    Returns:
        ``(throughput, eta)``.
    """
    now = datetime.now(UTC)
    one_hour_ago = now - timedelta(hours=1)
    three_hours_ago = now - timedelta(hours=3)
    done = (MediaFile.status == "completed", MediaFile.completed_at.isnot(None))

    row = db.query(
        func.count().filter(*done).label("total_completed"),
        func.count().filter(*done, MediaFile.completed_at > one_hour_ago).label("last_1h"),
        func.count().filter(*done, MediaFile.completed_at > three_hours_ago).label("last_3h"),
        func.count()
        .filter(MediaFile.file_size > 0, MediaFile.status.in_(["pending", "processing"]))
        .label("remaining"),
    ).first()

    if not row:
        throughput = {"total_completed": 0, "last_1h": 0, "last_3h": 0, "rate_1h": 0, "rate_3h": 0}
        return throughput, eta_from_counts(0, 0)

    throughput = {
        "total_completed": row.total_completed,
        "last_1h": row.last_1h,
        "last_3h": row.last_3h,
        "rate_1h": row.last_1h,
        "rate_3h": round(row.last_3h / 3.0, 1) if row.last_3h else 0,
    }
    return throughput, eta_from_counts(row.last_3h, row.remaining)


def get_queue_depths() -> dict[str, int]:
    """Get Celery queue depths (pending + reserved) per queue.

    Backed by ``app.core.celery_metrics.queue_snapshot`` — the single source
    of truth for this measurement, also used by the ``/metrics`` gauges
    (issue #892). Each returned value is ``pending + reserved``: pending tasks
    sitting in a queue's 10 priority sub-lists, plus tasks a worker has
    already picked up and not yet acknowledged (prefetched, or RUNNING under
    ``acks_late=True`` — see that module's docstring for the full caveat).
    Summing the two fixes the inversion where a saturated GPU fleet reported
    queue depth 0 while every worker was fully loaded.

    Returns:
        Dictionary mapping queue name to task count, plus a ``"total"`` key.
    """
    from app.core.celery_metrics import queue_snapshot
    from app.core.queues import CeleryQueues

    try:
        snapshot = queue_snapshot()
        depths: dict[str, int] = {
            name: counts["pending"] + counts["reserved"] for name, counts in snapshot.items()
        }
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
        from app.transcription.diar_sidecar_status import describe_diarizer_status

        diarizer_status = describe_diarizer_status()
    except Exception:  # noqa: BLE001 — a stats panel never fails over a display value
        diarizer_status = {
            "configured": "unknown",
            "configured_description": "unknown diarization engine",
            "effective": "unknown",
            "effective_description": "unknown diarization engine",
            "using_fallback": False,
            "require_sidecar": False,
            "refusing": False,
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
            "require_sidecar": diarizer_status["require_sidecar"],
            "refusing": diarizer_status["refusing"],
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
