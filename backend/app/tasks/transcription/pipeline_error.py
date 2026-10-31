"""Safety-net error handler for transcription pipeline chain failures.

``on_pipeline_error`` is attached as ``link_error`` to every dispatched chain (see
``dispatch.py``): it cleans up temp audio and makes sure the file and task do not stay
looking busy after a stage raised.
"""

import contextlib
import logging

from app.core.celery import celery_app
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.utils.task_utils import update_media_file_status
from app.utils.task_utils import update_task_status

logger = logging.getLogger(__name__)


@celery_app.task(name="transcription.pipeline_error", ignore_result=True)
def on_pipeline_error(file_uuid: str, task_id: str) -> None:
    """Safety-net error handler for pipeline chain failures.

    Ensures temp audio is cleaned up and file/task status is marked ERROR
    even if the failing task's internal error handler didn't complete.
    Called via link_error when any task in the chain raises an exception.

    Special handling:
    - OOM errors: logs VRAM state and suggests reducing batch size
    - Postprocess failures: segments are already saved by GPU task, so
      the file is marked COMPLETED with a warning rather than ERROR
    """
    from app.services.minio_service import cleanup_temp_audio
    from app.services.uuid_helpers import get_file_by_uuid
    from app.utils import benchmark_timing

    from .notifications import send_error_notification

    logger.warning(f"Pipeline error handler triggered for file {file_uuid}")

    # A failure that the retry policy already answered with a NEW run must not touch the file:
    # the temp audio is keyed by file and the replacement run is using it, and marking the file
    # ERROR here would overwrite the replacement's PROCESSING status.
    from .run_ownership import replaced_by_newer_run

    if replaced_by_newer_run(task_id):
        logger.info(
            "Pipeline error for file %s belongs to run %s, which a newer run has replaced "
            "-- leaving the file to it",
            file_uuid,
            task_id,
        )
        return

    # Clean up temp audio
    with contextlib.suppress(Exception):
        cleanup_temp_audio(file_uuid)

    # Record a terminal marker so the analysis layer can distinguish
    # error-mode completions from successful ones (Phase 2 PR #8, G27).
    benchmark_timing.mark(task_id, "pipeline_error_end")

    # Issue #1163: an exception that ended a run whose cancellation was requested is a
    # cancellation. Marking the file ERROR here is what turned user cancels into "Transcription
    # pipeline failed unexpectedly" and an error notification.
    cancelled_run = _cancelled_run_owner(file_uuid, task_id)
    if cancelled_run is not None:
        from app.services.transcription_retry import finish_cancelled_run

        file_id, user_id = cancelled_run
        logger.info(
            "Pipeline error for file %s ended a cancelled run; recording it cancelled", file_uuid
        )
        finish_cancelled_run(task_id, file_id, file_uuid, user_id)
        _flush_error_timing(task_id, file_id, user_id)
        return

    # Track the captured file id for the error-path timing flush below —
    # avoids running the flush inside the closed session_scope.
    flushed_file_id: int | None = None
    flushed_user_id: int | None = None

    # Ensure file is marked as errored
    try:
        with session_scope() as db:
            media_file = get_file_by_uuid(db, file_uuid)
            if media_file is not None:
                flushed_file_id = int(media_file.id)
                flushed_user_id = int(media_file.user_id)

            # Check task error to determine failure stage
            from app.models.media import Task

            task = db.query(Task).filter(Task.id == task_id).first()
            error_msg = (task.error_message or "") if task else ""

            is_oom = "CUDA out of memory" in error_msg or "OutOfMemoryError" in error_msg
            is_postprocess_only = task and task.status == "completed"

            if is_oom:
                _log_oom_diagnostics(error_msg)

            # If postprocess failed but task was already completed (segments saved),
            # keep the file as COMPLETED — the transcription data is intact
            if is_postprocess_only:
                logger.info(
                    f"Postprocess failed but segments already saved for {file_uuid}, "
                    "keeping COMPLETED status"
                )
                _flush_error_timing(task_id, flushed_file_id, flushed_user_id)
                return

            if media_file and media_file.status not in (
                FileStatus.ERROR,
                FileStatus.COMPLETED,
                FileStatus.CANCELLED,
            ):
                update_media_file_status(db, int(media_file.id), FileStatus.ERROR)

            # Only update task if not already finalized
            if task and task.status not in ("completed", "failed"):
                user_error = _get_pipeline_error_message(error_msg, is_oom)
                update_task_status(
                    db,
                    task_id,
                    "failed",
                    error_message=user_error,
                    completed=True,
                )

                if media_file:
                    send_error_notification(
                        int(media_file.user_id),
                        int(media_file.id),
                        user_error,
                    )
    except Exception as e:
        logger.error(f"Error in pipeline error handler: {e}")
    finally:
        _flush_error_timing(task_id, flushed_file_id, flushed_user_id)


def _cancelled_run_owner(file_uuid: str, task_id: str) -> tuple[int, int] | None:
    """``(file_id, user_id)`` when run ``task_id`` was being cancelled, else None.

    None also when the file or the cancel state cannot be read: the error handler then
    proceeds as it always has.
    """
    from app.services.transcription_retry import run_cancel_requested
    from app.services.uuid_helpers import get_file_by_uuid

    try:
        with session_scope() as db:
            media_file = get_file_by_uuid(db, file_uuid)
            ids = (int(media_file.id), int(media_file.user_id))
    except Exception as e:  # noqa: BLE001 - see docstring
        logger.debug("Could not read file %s for the cancel check: %s", file_uuid, e)
        return None
    return ids if run_cancel_requested(task_id, ids[0]) else None


def _flush_error_timing(task_id: str, file_id: int | None, user_id: int | None) -> None:
    """Persist the Redis timing hash into ``file_pipeline_timing`` on failure.

    Mirrors the success-path flush that finalize_transcription performs, so
    the error wall-clock stays queryable. Best-effort: instrumentation
    failures never propagate.
    """
    from app.utils import benchmark_timing

    if not benchmark_timing.benchmark_enabled() or file_id is None:
        return
    try:
        from app.services.pipeline_timing_service import record_pipeline_timing

        record_pipeline_timing(task_id=task_id, file_id=file_id, user_id=user_id)
    except Exception as e:
        logger.debug(f"Error-path timing flush failed for {task_id}: {e}")


def _log_oom_diagnostics(error_msg: str) -> None:
    """Log VRAM diagnostics when an OOM error occurs."""
    logger.error(f"GPU OOM detected: {error_msg[:200]}")
    try:
        import torch

        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                free, total = torch.cuda.mem_get_info(i)
                logger.error(
                    f"GPU {i} VRAM: {free / 1024**2:.0f}MB free / {total / 1024**2:.0f}MB total"
                )
    except Exception as e:
        logger.debug(f"Could not read GPU VRAM during OOM diagnostics: {e}")


def _get_pipeline_error_message(error_msg: str, is_oom: bool) -> str:
    """Generate a user-friendly error message for pipeline failures."""
    if is_oom:
        return (
            "GPU ran out of memory during processing. "
            "Try reducing the number of concurrent tasks or using a smaller model."
        )
    return "Transcription pipeline failed unexpectedly"
