"""Bulk ``retry`` and ``reprocess``: the two actions that reset a file and re-dispatch it.

They mirror the single-file ``/retry`` (``management``) and ``/reprocess`` (``reprocess``)
routes gate for gate -- status, legal hold, retry ceiling, media availability -- and report
a refusal as that file's ``BulkActionResult`` instead of aborting the batch.
"""

from sqlalchemy.orm import Session

from app.api.endpoints.files.bulk_action_handlers import BulkActionResult
from app.core.env import env_bool
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services import system_settings_service
from app.tasks.transcription import dispatch_transcription_pipeline
from app.utils.file_retry import reset_file_for_retry
from app.utils.file_retry import transcript_is_regenerable


def _held_result(db: Session, file_uuid: str, file_id: int) -> BulkActionResult | None:
    """The per-file refusal of a bulk reprocess or retry on a held file; ``None`` when free."""
    from app.services.hold_guard import LEGAL_HOLD_ERROR_CODE
    from app.services.hold_guard import REPROCESS_REFUSAL
    from app.services.hold_guard import hold_state

    if not hold_state(db, int(file_id)).held:
        return None
    return BulkActionResult(
        file_uuid=file_uuid, success=False, message=REPROCESS_REFUSAL, error=LEGAL_HOLD_ERROR_CODE
    )


def _handle_retry_action(
    db: Session, file_uuid: str, file_id: int, reset_retry_count: bool, is_admin: bool
) -> BulkActionResult:
    """Handle retry action for bulk operations.

    Mirrors the single-file `POST /{file_uuid}/retry` in `management`: same status gate (a bulk
    "retry" on a COMPLETED file used to sail through and `reset_file_for_retry` would
    delete its transcript segments), same admin-only `reset_retry_count`, and the same
    ceiling check via `retry_ceiling_message`. This handler used to skip all three, so
    the ceiling stayed fully bypassable through the gallery's bulk-retry button even
    after the single-file route was hardened (issue #545-adjacent — a fix landed on
    fewer than every route reachable for the same action).
    """

    db_file = db.query(MediaFile).filter_by(id=file_id).first()
    if not db_file:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="File not found",
            error="NOT_FOUND",
        )

    if db_file.status not in [FileStatus.ERROR, FileStatus.CANCELLED, FileStatus.ORPHANED]:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message=f"File cannot be retried in current status: {db_file.status}",
            error="INVALID_STATUS",
        )

    held = _held_result(db, file_uuid, file_id)
    if held is not None:
        return held

    if reset_retry_count and not is_admin:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="Only an admin may reset the retry count.",
            error="FORBIDDEN",
        )

    if not reset_retry_count and not is_admin:
        ceiling_message = system_settings_service.retry_ceiling_message(
            db, int(db_file.retry_count or 0)
        )
        if ceiling_message:
            return BulkActionResult(
                file_uuid=file_uuid,
                success=False,
                message=ceiling_message,
                error="RETRY_CEILING_REACHED",
            )

    success = reset_file_for_retry(db, file_id, reset_retry_count)
    if not success:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="Failed to reset file for retry",
            error="RESET_FAILED",
        )

    if not env_bool("SKIP_CELERY"):
        task_id = dispatch_transcription_pipeline(file_uuid=file_uuid, reuse_requested_options=True)
        message = f"Retry started (task: {task_id})"
    else:
        message = "Retry prepared (test mode)"

    return BulkActionResult(file_uuid=file_uuid, success=True, message=message)


def _handle_reprocess_action(
    db: Session,
    file_uuid: str,
    file_id: int,
    is_admin: bool,
    stages: list[str] | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    disable_diarization: bool = False,
    *,
    user_id: int | None,
) -> BulkActionResult:
    """Handle reprocess action for bulk operations.

    Ceiling check + `reset_retry_count` mirror the single-file
    `files/reprocess.py::reprocess_file` exactly: a non-admin is blocked once the
    ceiling is reached (`retry_ceiling_message`), and `reset_file_for_retry` is always
    called with `reset_retry_count=False` — reprocessing increments the counter like a
    normal attempt, it never zeroes it, admin or not. This handler used to hardcode
    `True` for every caller, which zeroed the counter unconditionally and defeated the
    ceiling on every future retry of the file too, including through the routes that
    do enforce it.
    """

    from app.api.endpoints.files.reprocess import MEDIA_REQUIRING_STAGES

    db_file = db.query(MediaFile).filter_by(id=file_id).first()
    if not db_file:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="File not found",
            error="NOT_FOUND",
        )

    # Only reprocess completed or error files
    if db_file.status not in [FileStatus.COMPLETED, FileStatus.ERROR, FileStatus.CANCELLED]:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message=f"Cannot reprocess file in {db_file.status} status",
            error="INVALID_STATUS",
        )

    held = _held_result(db, file_uuid, file_id)
    if held is not None:
        return held

    if not is_admin:
        ceiling_message = system_settings_service.retry_ceiling_message(
            db, int(db_file.retry_count or 0)
        )
        if ceiling_message:
            return BulkActionResult(
                file_uuid=file_uuid,
                success=False,
                message=ceiling_message,
                error="RETRY_CEILING_REACHED",
            )

    # Issue #872: this is the bulk gallery action, so one click can reach many
    # files. Mirror the single-file endpoint's refusal — `reset_file_for_retry`
    # guards its own deletes, but `clear_selective_data` below runs even when
    # `transcription` is not among the stages (`rediarize` deletes speakers).
    needs_media = not stages or bool(MEDIA_REQUIRING_STAGES.intersection(stages))
    if needs_media and not transcript_is_regenerable(db, db_file):
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message=(
                "This file's media could not be found in storage, so reprocessing "
                "would delete its existing transcript with nothing to regenerate it from."
            ),
            error="MEDIA_UNAVAILABLE",
        )

    # The single-file endpoint's pipeline-plan refusal (#1228), before anything is cleared:
    # the same decision, reported per file with the plan's fixed sentence.
    from app.api.endpoints.files.reprocess import unrunnable_rerun_code
    from app.services.pipeline_plan import ISSUE_MESSAGES

    refused_code = unrunnable_rerun_code(
        db,
        int(db_file.user_id),
        stages,
        whisper_model=None,
        disable_diarization=disable_diarization,
    )
    if refused_code is not None:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message=ISSUE_MESSAGES[refused_code],
            error=refused_code,
        )

    if stages:
        # Selective reprocessing
        from app.api.endpoints.files.reprocess_clear import clear_selective_data
        from app.api.endpoints.files.reprocess_dispatch import dispatch_selective_tasks

        if "transcription" in stages:
            success = reset_file_for_retry(db, file_id, False)
            if not success:
                return BulkActionResult(
                    file_uuid=file_uuid,
                    success=False,
                    message="Failed to reset file for reprocessing",
                    error="RESET_FAILED",
                )

        clear_selective_data(db, db_file, stages, user_id=user_id)
        dispatch_selective_tasks(
            file_uuid,
            stages,
            min_speakers,
            max_speakers,
            num_speakers,
            file_id=file_id,
            user_id=int(db_file.user_id),
            disable_diarization=disable_diarization,
            owner_id=int(db_file.user_id),
        )
        message = f"Selective reprocessing started (stages: {', '.join(stages)})"
        return BulkActionResult(file_uuid=file_uuid, success=True, message=message)

    # Full reprocess (backward compatible)
    success = reset_file_for_retry(db, file_id, False)
    if not success:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="Failed to reset file for reprocessing",
            error="RESET_FAILED",
        )

    if not env_bool("SKIP_CELERY"):
        task_id = dispatch_transcription_pipeline(
            file_uuid=file_uuid,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            num_speakers=num_speakers,
            # False must reach the pipeline as "not asked" (see start_reprocessing_task).
            disable_diarization=True if disable_diarization else None,
        )
        message = f"Reprocessing started (task: {task_id})"
    else:
        message = "Reprocessing prepared (test mode)"

    return BulkActionResult(file_uuid=file_uuid, success=True, message=message)
