"""Transcription task context and the shared failure/validation handlers.

``TranscriptionContext`` is the state bundle every stage of the pipeline
passes around; the helpers here own the error paths that mark a file
FAILED and notify the SPA.
"""

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import NoReturn

from celery.exceptions import Reject

from app.core.constants import DIAR_SIDECAR_MAX_RETRIES
from app.core.constants import DIAR_SIDECAR_RETRY_BASE
from app.core.constants import DIAR_SIDECAR_RETRY_MAX
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.services.error_categorization_service import ErrorCategorizationService
from app.transcription import cuda_health
from app.utils.task_utils import update_media_file_status
from app.utils.task_utils import update_task_status

if TYPE_CHECKING:
    from app.transcription.diarizer_native import DiarSidecarUnavailableError

logger = logging.getLogger(__name__)


def retry_on_diar_sidecar_unavailable(
    task, exc: "DiarSidecarUnavailableError", file_uuid: str
) -> NoReturn:
    """Shared retry-scheduling helper for issue #656 Step 5.

    Used by both ``transcribe_gpu_task`` (``core.py``) and ``diarize_gpu_task``
    (``diarize_task.py``) — extracted so each task's own ``except`` clause stays a single
    call, keeping cyclomatic complexity under the repo's C901 gate rather than raising it.

    ``task.retry()`` always raises ``celery.exceptions.Retry``, so this function never
    returns normally; callers still write ``raise retry_on_diar_sidecar_unavailable(...)``
    for readability even though the raise happens inside.

    MUST be caught by an ``except DiarSidecarUnavailableError`` clause placed BEFORE the
    task's generic ``except Exception`` — see both call sites' comments for why: raised from
    inside the generic handler, ``_handle_transcription_failure`` would already have marked
    the file ERROR and sent an error notification on every attempt, not just the last.
    """
    countdown = exc.retry_after or min(
        DIAR_SIDECAR_RETRY_BASE * 2**task.request.retries, DIAR_SIDECAR_RETRY_MAX
    )
    logger.warning(
        "diar-native sidecar unavailable (%s) for file %s (attempt %d/%d), retrying in %.0fs",
        exc.reason,
        file_uuid,
        task.request.retries + 1,
        DIAR_SIDECAR_MAX_RETRIES,
        countdown,
    )
    raise task.retry(exc=exc, countdown=countdown, max_retries=DIAR_SIDECAR_MAX_RETRIES) from exc


def retry_on_asr_rate_limit(task, exc, file_uuid: str) -> NoReturn:
    """Shared retry-scheduling helper for a cloud-ASR vendor throttle/quota response
    (``ASRRateLimitedError``). Extracted alongside :func:`retry_on_diar_sidecar_unavailable`
    for the same reason — keeping ``transcribe_gpu_task``'s own cyclomatic complexity under
    the repo's C901 gate. Deliberately a SEPARATE policy from the task's
    ``autoretry_for=(ConnectionError, TimeoutError)``: that one's ``retry_backoff_max=30`` /
    ``max_retries=1`` are tuned for a GPU-path connection blip, far too short for a vendor
    throttle, and ``autoretry_for`` cannot honor ``Retry-After``.
    """
    from app.core.constants import CLOUD_ASR_MAX_RETRIES
    from app.core.constants import CLOUD_ASR_RETRY_BASE
    from app.core.constants import CLOUD_ASR_RETRY_MAX

    countdown = exc.retry_after or min(
        CLOUD_ASR_RETRY_BASE * 2**task.request.retries, CLOUD_ASR_RETRY_MAX
    )
    logger.warning(
        "Cloud ASR rate-limited for file %s (attempt %d/%d), retrying in %.0fs: %s",
        file_uuid,
        task.request.retries + 1,
        CLOUD_ASR_MAX_RETRIES,
        countdown,
        exc,
    )
    raise task.retry(exc=exc, countdown=countdown, max_retries=CLOUD_ASR_MAX_RETRIES) from exc


def retry_transcribe_gpu_exception(task, exc, file_uuid: str) -> NoReturn:
    """Single call site for ``transcribe_gpu_task``'s ``except (ASRRateLimitedError,
    DiarSidecarUnavailableError)`` clause — dispatches to the matching ``retry_on_*`` helper.
    Keeping the isinstance check here (rather than in the task body) keeps that function's
    own cyclomatic complexity under the repo's C901 gate; always raises.
    """
    from app.transcription.diarizer_native import DiarSidecarUnavailableError

    if isinstance(exc, DiarSidecarUnavailableError):
        retry_on_diar_sidecar_unavailable(task, exc, file_uuid)
    else:
        retry_on_asr_rate_limit(task, exc, file_uuid)


def requeue_after_abort(file_uuid: str, abort: Exception, *, stage: str) -> NoReturn:
    """Stand a shutdown-aborted pipeline stage down without failing the file (#809).

    THE single implementation, shared by all three tasks that can reach a cooperative-abort
    checkpoint — ``transcribe_gpu_task`` (``core.py``), ``diarize_gpu_task``
    (``diarize_task.py``) and ``transcribe_cpu_task`` (``cpu_task.py``). It lives here rather
    than in any one of them because three copies of a "do NOT travel the failure path" rule is
    three chances for one of them to drift into marking the file errored, which is the exact
    defect this function exists to prevent.

    The work was INTERRUPTED, not broken, so this deliberately does not travel the failure
    path -- no ``_handle_transcription_failure``, no error notification. Marking the file
    errored would turn a clean restart into a user-visible failure and stop it being retried.

    ``Reject(requeue=True)``, never a bare ``raise``: under ``acks_late=True`` celery acks on
    RETURN -- success or exception -- so raising anything else here would ACK the message and
    LOSE the work. That is worse than the SIGKILL this replaces, since after a SIGKILL the
    message is redelivered.

    ``Reject`` also does not fire the chain's ``link_error`` errback (celery's tracer handles
    it in its own ``except Reject`` arm, which never reaches ``handle_failure``), so a
    graceful shutdown does not present to the user as a pipeline failure. Pinned by
    ``tests/unit/test_cooperative_abort.py::TestRejectDoesNotFireErrbacks``.

    Redis is not AMQP, so #809 required this be verified rather than inferred from the AMQP
    contract. Measured against a real celery worker on a real Redis broker with
    ``acks_late=True``, rejecting on first delivery: DELIVERY #1, DELIVERY #2, second
    completed. ``Reject(requeue=True)`` does redeliver on the Redis transport.

    Position matters too. kombu answers ``Reject(requeue=True)`` with an LPUSH -- the BACK of
    the queue -- so a file interrupted by a deploy waited behind everything submitted while it
    ran. The message is therefore first moved to the HEAD of its priority list with kombu's
    own ``restore_by_tag`` (``core/broker_orphans.requeue_own_delivery_to_front``) and the
    stage raises ``Reject(requeue=False)``, whose kombu reject is then a no-op on the moved
    entry. If the message cannot be found, the plain ``Reject(requeue=True)`` still applies:
    a late place in line is better than a lost transcription.

    Args:
        file_uuid: The file whose stage stood down, for the log line.
        abort: The ``TranscriptionAbortedError`` that reached the task layer; chained onto the
            ``Reject`` so the checkpoint that fired is still readable from the traceback.
        stage: Which task stood down ("GPU transcription", "GPU diarization", ...). Named
            explicitly rather than derived, so the log says which leg of the pipeline is being
            requeued when several are draining at once.

    Raises:
        Reject: always -- this function exists to convert an abort into a requeue.
    """
    logger.warning(
        "%s for file %s stood down (%s) -- requeueing",
        stage,
        file_uuid,
        abort,
    )
    if _requeue_at_front():
        raise Reject(requeue=False) from abort
    raise Reject(requeue=True) from abort


def _requeue_at_front() -> bool:
    """Move the executing stage's own message to the head of its queue (see the caller)."""
    from app.core.broker_orphans import requeue_own_delivery_to_front
    from app.core.task_liveness import _current_stage_id

    stage_id = _current_stage_id()
    return bool(stage_id) and requeue_own_delivery_to_front(str(stage_id))


#: Redis counter of poisoned-context requeues per task, so a message that breaks every worker
#: it lands on fails after a few attempts instead of cycling workers forever.
_POISONED_REQUEUE_KEY = "gpu_poisoned_requeues:{task_id}"
_POISONED_REQUEUE_TTL_S = 86_400


def _redis_client():
    from app.core.redis import get_redis

    return get_redis()


def _poisoned_requeue_allowed(task_id: str) -> bool:
    """Count one more poisoned-context requeue for ``task_id``; False once past the cap.

    ``GPU_POISONED_MAX_REQUEUES`` (default 2). A Redis failure allows the requeue: Redis is
    the broker, so if it is unreachable the requeue cannot loop anyway.
    """
    try:
        cap = max(0, int(os.getenv("GPU_POISONED_MAX_REQUEUES", "2")))
    except ValueError:
        cap = 2
    key = _POISONED_REQUEUE_KEY.format(task_id=task_id)
    try:
        client = _redis_client()
        count = int(client.incr(key))
        client.expire(key, _POISONED_REQUEUE_TTL_S)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("Could not count poisoned-context requeues for %s: %s", task_id, exc)
        return True
    return count <= cap


def requeue_if_context_poisoned(
    task_id: str, file_uuid: str, exc: Exception, *, stage: str
) -> None:
    """Handle a GPU failure caused by a broken CUDA context (issue #1081).

    Returns normally when ``exc`` is anything else, or when its message looks fatal but a
    probe shows the context still works, so the caller's failure path runs.
    Otherwise this worker is taken out of service (it exits and is restarted, see
    ``cuda_health.mark_context_poisoned``) and the task is requeued for a healthy worker:
    the file is not at fault. Past ``GPU_POISONED_MAX_REQUEUES`` for the same task it
    returns, so the file fails normally instead of cycling workers forever.
    """
    if not cuda_health.is_context_poisoned_error(exc) or cuda_health.cuda_context_healthy():
        return
    cuda_health.mark_context_poisoned(str(exc))
    if not _poisoned_requeue_allowed(task_id):
        logger.error(
            "%s for file %s hit a broken CUDA context again; not requeueing it any more",
            stage,
            file_uuid,
        )
        return
    requeue_after_abort(file_uuid, exc, stage=f"{stage} (broken CUDA context)")


@dataclass
class TranscriptionContext:
    """Context holder for transcription task state."""

    task_id: str
    file_id: int
    file_uuid: str
    user_id: int
    file_path: str
    file_name: str
    content_type: str
    # Tenant scope (cloud-edition seam; None = personal / community)
    organization_id: int | None = None


def _get_media_file_context(file_uuid: str, task_id: str) -> TranscriptionContext | None:
    """Get media file and create transcription context."""
    from app.utils.uuid_helpers import get_file_by_uuid

    with session_scope() as db:
        media_file = get_file_by_uuid(db, file_uuid)
        if not media_file:
            logger.error(f"Media file with UUID {file_uuid} not found")
            return None

        ctx = TranscriptionContext(
            task_id=task_id,
            file_id=int(media_file.id),
            file_uuid=file_uuid,
            user_id=int(media_file.user_id),
            file_path=str(media_file.storage_path),
            file_name=str(media_file.filename),
            content_type=str(media_file.content_type),
            organization_id=media_file.organization_id,
        )
        update_media_file_status(db, ctx.file_id, FileStatus.PROCESSING)
        return ctx


def cancelled_payload(file_uuid: str, file_id: int, task_id: str) -> dict:
    """The chain payload of a run recorded as cancelled (see ``finalize_cancelled_run``)."""
    return {"status": "cancelled", "file_uuid": file_uuid, "file_id": file_id, "task_id": task_id}


def is_cancelled(payload: object) -> bool:
    """Whether a stage's failure handling recorded the run as cancelled (issue #1163)."""
    return isinstance(payload, dict) and payload.get("status") == "cancelled"


def _handle_transcription_failure(
    ctx: TranscriptionContext, task_id: str, raw_error: str, error_type: str
) -> dict:
    """Handle a stage failure through the one retry policy (``services/transcription_retry``).

    ``raw_error`` is classified once, here, and never stored (issue #959): the task and file
    rows carry the fixed user-facing sentence and ``error_category`` the retry code derived
    from the raw text. Callers log the raw exception before calling.

    A PERMANENT failure (the input is unusable) marks the file ERROR and notifies at once. A
    TRANSIENT one (infrastructure, or unclassified) dispatches a replacement run at retry
    priority after a short backoff, and only marks the file ERROR once the retry budget is
    spent. The policy also fires the completion hook (success=False) either way, so a quota
    reservation taken at dispatch is released.

    A run whose cancellation was requested is recorded as cancelled instead (issue #1163),
    and the caller must RETURN the ``{"status": "cancelled", ...}`` payload rather than
    re-raise: returning acks the message and lets ``finalize_transcription`` release the temp
    audio, and Celery records no failure for a stage the user stopped.

    Returns:
        The chain payload for the rest of this run: ``{"status": "error", ...}`` when the file
        failed, ``{"status": "cancelled", ...}`` when the run was being cancelled, or a
        superseded marker when a replacement run now owns the file -- the next stage then
        stands down without touching the temp audio that run is using.
    """
    from app.services.transcription_retry import RunOutcome
    from app.services.transcription_retry import finish_failed_run

    from .run_ownership import SUPERSEDED

    failure = ErrorCategorizationService.classify_failure(raw_error)
    outcome = finish_failed_run(task_id, ctx.file_id, failure)
    if outcome == RunOutcome.CANCELLED:
        return cancelled_payload(ctx.file_uuid, ctx.file_id, task_id)
    if outcome == RunOutcome.RETRIED:
        return {
            "status": SUPERSEDED,
            "file_uuid": ctx.file_uuid,
            "file_id": ctx.file_id,
            "task_id": task_id,
        }
    return {"status": "error", "message": failure.user_message, "error_type": error_type}


def _validate_transcription_result(
    result: dict, ctx: TranscriptionContext, task_id: str
) -> dict | None:
    """Validate transcription result has valid content. Returns error dict if invalid, None if valid."""
    if not result or not result.get("segments") or len(result["segments"]) == 0:
        error_msg = (
            "No audio content could be detected in this file. "
            "The file may be corrupted, contain only silence, or be in an unsupported format. "
            "Please check the file and try uploading again."
        )
        logger.warning(f"No valid audio content found in file {ctx.file_id}: {ctx.file_name}")
        return _handle_transcription_failure(ctx, task_id, error_msg, "no_valid_audio")

    # Check if segments contain actual transcribable content
    has_content = any(segment.get("text", "").strip() for segment in result["segments"])
    if not has_content:
        error_msg = (
            "No speech could be detected in this file. "
            "The file may contain only music, background noise, or silence. "
            "Please verify the file contains clear speech and try again."
        )
        logger.warning(f"No speech content found in file {ctx.file_id}: {ctx.file_name}")
        return _handle_transcription_failure(ctx, task_id, error_msg, "no_speech_content")

    return None


def _handle_outer_exception(
    ctx: TranscriptionContext | None, task_id: str, error: Exception
) -> dict:
    """Handle top-level exception in transcription task (through the one retry policy)."""
    logger.error(f"Error processing file {ctx.file_id if ctx else None}: {error}")
    if ctx is not None:
        return _handle_transcription_failure(ctx, task_id, str(error), "processing_error")
    # Classified once from the raw exception; only the fixed sentence is stored (#959).
    failure = ErrorCategorizationService.classify_failure(str(error))
    try:
        with session_scope() as db:
            update_task_status(
                db, task_id, "failed", error_message=failure.user_message, completed=True
            )
    except Exception as update_err:
        logger.error(f"Error updating task status: {update_err}")
    return {"status": "error", "message": failure.user_message}
