"""Transcription pipeline hooks (cloud-edition seam).

Two extension points fired by the core pipeline; community defaults are
no-ops with zero overhead. The commercial cloud layer registers:
  - a before-dispatch hook for quota reservation (raising
    ``QuotaExceededError`` -> HTTP 402 with an upgrade prompt), and
  - a transcription-complete hook for usage metering + analytics events
    (idempotent on ``file_id:run_id`` against retries/replays).

Hook-author contract (covered by ``CLOUD_SEAM_VERSION``, app.auth.constants):
  - THROWING is contained, with exactly TWO deliberate exceptions:
    ``QuotaExceededError`` and ``DispatchBlockedError`` propagate and stop the
    job. Anything else a hook raises is logged and swallowed -- a broken hook
    can never fail a transcription. A hook that MEANS to refuse must say so by
    raising one of those two; an unexpected crash is not a policy decision.
  - HANGING is NOT contained: hooks run synchronously inside the completion
    path (an open session_scope holding the task's final status). A hook that
    blocks on network I/O delays the completion commit + user notification
    for as long as it blocks. Every outbound call inside a hook MUST carry a
    tight socket/request timeout (a few seconds); durable/slow work belongs
    on a queue the hook merely enqueues to.
  - Hooks receive dataclass contexts, never the task's DB session -- open
    your own ``session_scope()`` for any DB work.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from fastapi import HTTPException

logger = logging.getLogger(__name__)


class QuotaExceededError(HTTPException):
    """Raised by a before-dispatch hook when the tenant is over quota.

    Subclasses HTTPException so API-triggered dispatches surface as a clean
    402 Payment Required without extra translation layers; Celery-triggered
    dispatches (watch sources) catch it like any pipeline error.
    """

    def __init__(self, detail: str = "Transcription quota exceeded", hours_over: float = 0.0):
        super().__init__(status_code=402, detail=detail)
        self.hours_over = hours_over


class DispatchBlockedError(HTTPException):
    """Raised by a before-dispatch hook to refuse a job for a NON-quota reason.

    Containment in :func:`fire_before_dispatch` exists so a buggy third-party
    hook can never stop community transcription. The cost was that a hook with a
    *considered* reason to refuse — a suspended tenant, a legal hold, a content
    policy — had no way to say so: its exception was logged and the job
    dispatched anyway. This is the explicit "fail closed" signal, and raising it
    is the ONLY way (besides ``QuotaExceededError``) a hook can block dispatch.

    Subclasses HTTPException so an API-triggered dispatch surfaces a clean
    status without a translation layer, exactly as ``QuotaExceededError`` does
    with 402. The default is 403 Forbidden — "you may not run this", as opposed
    to quota's "pay for more and you may". ``status_code`` is settable for a
    blocker that needs a different one (451 for a legal hold, say).
    """

    def __init__(self, detail: str = "Transcription blocked", status_code: int = 403):
        super().__init__(status_code=status_code, detail=detail)


@dataclass(frozen=True)
class DispatchContext:
    """What a quota hook needs to decide whether a job may run."""

    file_id: int
    file_uuid: str
    user_id: int
    organization_id: int | None
    est_audio_hours: Decimal | None  # None when duration not yet known
    task_id: str


@dataclass(frozen=True)
class CompletionContext:
    """What a metering hook needs to charge/record a finished run."""

    file_id: int
    file_uuid: str
    user_id: int
    organization_id: int | None
    audio_duration_s: float
    run_id: str  # task_id of this pipeline run — idempotency scope
    provider: str  # "local" | cloud-ASR provider name
    success: bool


BeforeDispatchHook = Callable[[DispatchContext], None]
TranscriptionCompleteHook = Callable[[CompletionContext], None]

_before_dispatch_hooks: list[BeforeDispatchHook] = []
_transcription_complete_hooks: list[TranscriptionCompleteHook] = []


def register_before_dispatch(hook: BeforeDispatchHook) -> None:
    """Register a pre-dispatch hook (cloud: quota reservation)."""
    _before_dispatch_hooks.append(hook)
    logger.info("Registered before-dispatch pipeline hook")


def register_transcription_complete(hook: TranscriptionCompleteHook) -> None:
    """Register a completion hook (cloud: metering + usage events)."""
    _transcription_complete_hooks.append(hook)
    logger.info("Registered transcription-complete pipeline hook")


def clear_hooks() -> None:
    """Remove all registered hooks (primarily for tests)."""
    _before_dispatch_hooks.clear()
    _transcription_complete_hooks.clear()


def fire_before_dispatch(ctx: DispatchContext) -> None:
    """Run pre-dispatch hooks. Three outcomes, only two of which block the job.

    * Returns normally — no hook objected, the job dispatches.
    * ``QuotaExceededError`` (402) or ``DispatchBlockedError`` (403 by default)
      propagates — a hook DELIBERATELY refused. Nothing dispatches, and the
      remaining hooks do not run.
    * Any other exception is logged and swallowed, and dispatch proceeds. A
      broken cloud layer must never stop community transcription, so the
      default stays fail-open for *unexpected* failures; a hook that intends to
      refuse raises one of the two errors above instead.

    Args:
        ctx: The dispatch context handed to every registered hook.

    Raises:
        QuotaExceededError: A hook refused because the tenant is over quota.
        DispatchBlockedError: A hook refused for any other deliberate reason.
    """
    for hook in _before_dispatch_hooks:
        try:
            hook(ctx)
        except (QuotaExceededError, DispatchBlockedError):
            raise
        except Exception:
            logger.exception("before-dispatch hook failed; allowing dispatch")


def fire_transcription_complete(ctx: CompletionContext) -> None:
    """Run completion hooks. Failures are contained — metering problems must
    never mark a successful transcription as failed."""
    for hook in _transcription_complete_hooks:
        try:
            hook(ctx)
        except Exception:
            logger.exception("transcription-complete hook failed (contained)")
