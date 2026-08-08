"""Fail-closed gate for transcript text that is about to leave the deployment.

Three background tasks post transcript text to whatever provider the owner
configured — summarization, speaker identification and topic extraction. When
the effective policy says ``redact_before_llm``, that text must be masked first,
and "masked" has to actually mean masked: a path that quietly emits the original
text on a miss is worse than having no policy, because the setting reads as on.

Two conditions defeat naive masking, and both are the *common* case rather than
the edge case:

* **Detection has not finished.** ``redaction_detect_task`` is dispatched by the
  same post-processing step that dispatches these three tasks
  (``tasks/transcription/postprocess.py``), onto a separate CPU queue. Nothing
  orders them, so the LLM tasks routinely reach a transcript whose
  ``TranscriptSegment.redactions`` are still NULL — and
  ``RedactionService.mask_segment`` with an empty span list masks nothing and
  returns the text unchanged. The call looks masked and isn't.
* **Masking raised.** Returning the input on an exception hands the provider
  exactly the content the policy exists to withhold.

``resolve_llm_masking`` collapses both into one decision a caller cannot get
subtly wrong, and :class:`RedactionNotReadyError` gives batch callers the option
interactive chat does not have: come back later. Chat cannot wait for a
detection pass mid-request, so it masks inline instead
(``services/chat/redactor.py``) — same guarantee, different tradeoff.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core import constants as C  # noqa: N812
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.config import resolve_effective_config

logger = logging.getLogger(__name__)


class RedactionNotReadyError(Exception):
    """The policy requires masking but trustworthy spans are unavailable.

    Raised instead of returning ``None``, because ``None`` is the legitimate
    "no masking required" answer — conflating the two is precisely the bug this
    module exists to prevent.

    Attributes:
        retryable: True when detection is still in flight and deferring the task
            will resolve it; False when detection failed and waiting won't help.
        file_id: File awaiting detection, so the deferring caller can kick off a
            scan that was never dispatched.
        never_started: True when ``redaction_status`` was NULL — nothing is
            running, so waiting alone would never resolve.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        file_id: int | None = None,
        never_started: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.file_id = file_id
        self.never_started = never_started


def resolve_llm_masking(db: Session, media_file) -> EffectiveRedactionConfig | None:
    """Decide how to mask a file's transcript before it goes to an LLM provider.

    Args:
        db: Database session.
        media_file: The ``MediaFile`` whose transcript is being sent.

    Returns:
        The config to pass to the transcript builders, or ``None`` when the
        owner's policy does not require pre-LLM masking (send text as-is).

    Raises:
        RedactionNotReadyError: Masking is required but cached spans are missing or
            untrustworthy. Never downgrade this to "send unmasked".
        Exception: Whatever ``resolve_effective_config`` raises. An unresolvable
            policy must not be read as an absent policy — if we cannot tell
            whether masking is required, we must not send.
    """
    cfg = resolve_effective_config(db, int(media_file.user_id))
    if not (cfg.enabled and cfg.redact_before_llm):
        return None

    status = getattr(media_file, "redaction_status", None)
    if status == C.REDACTION_STATUS_DONE:
        return cfg

    if status == C.REDACTION_STATUS_FAILED:
        raise RedactionNotReadyError(
            f"Redaction detection failed for file {media_file.id}; "
            "refusing to send unmasked transcript to an LLM provider",
            retryable=False,
            file_id=int(media_file.id),
        )

    # pending / processing / NULL. A NULL status means detection was never
    # dispatched — the owner turned redaction on after upload, and the scan is
    # otherwise only queued lazily when they next open the file. Waiting alone
    # would time out, so the caller dispatches it (see defer_for_redaction).
    raise RedactionNotReadyError(
        f"Redaction detection for file {media_file.id} is {status or 'not started'}; "
        "deferring the LLM call until spans are cached",
        retryable=True,
        file_id=int(media_file.id),
        never_started=status is None,
    )


def defer_for_redaction(task, exc: RedactionNotReadyError, *, countdown: int = 60) -> None:
    """Re-queue a bound Celery task until detection lands, or give up safely.

    Args:
        task: The bound task (``self`` in a ``bind=True`` task).
        exc: The raised readiness error.
        countdown: Seconds to wait before the next attempt.

    Raises:
        Retry: To defer the task (Celery's normal control-flow exception).
        RedactionNotReadyError: When deferring is pointless or retries are exhausted,
            so the task fails loudly instead of silently leaking.
    """
    if not exc.retryable:
        raise exc

    retries = task.request.retries or 0
    if retries >= C.REDACTION_LLM_MAX_DEFERRALS:
        logger.error("Giving up after %d deferrals waiting for redaction spans: %s", retries, exc)
        raise exc

    # Nothing is scanning this file, so deferring alone would just burn retries.
    # Only on the first attempt: redaction_detect_task recomputes spans from
    # scratch, and re-dispatching every 60s would pile up duplicate CPU scans.
    if exc.never_started and retries == 0 and exc.file_id is not None:
        _dispatch_detection(exc.file_id)

    logger.info("Deferring %s for redaction (attempt %d): %s", task.name, retries + 1, exc)
    raise task.retry(exc=exc, countdown=countdown, max_retries=C.REDACTION_LLM_MAX_DEFERRALS)


def _dispatch_detection(file_id: int) -> None:
    """Queue the scan this file never got. Best-effort — the deferral stands either way."""
    try:
        from app.tasks.redaction_task import redaction_detect_task

        redaction_detect_task.delay(file_id=file_id)
        logger.info("Dispatched missing redaction detection for file %s", file_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not dispatch redaction detection for file %s", file_id)
