"""Transcription pipeline hooks (cloud-edition seam).

Two extension points fired by the core pipeline; community defaults are
no-ops with zero overhead. The commercial cloud layer registers:
  - a before-dispatch hook for quota reservation (raising
    ``QuotaExceededError`` -> HTTP 402 with an upgrade prompt), and
  - a transcription-complete hook for usage metering + analytics events
    (idempotent on ``file_id:run_id`` against retries/replays).

Hook-author contract (covered by ``CLOUD_SEAM_VERSION``, app.auth.constants):
  - THROWING is contained: any exception except ``QuotaExceededError`` is
    logged and swallowed -- a broken hook can never fail a transcription.
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
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable
from typing import Optional

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


@dataclass(frozen=True)
class DispatchContext:
    """What a quota hook needs to decide whether a job may run."""

    file_id: int
    file_uuid: str
    user_id: int
    organization_id: Optional[int]
    est_audio_hours: Optional[Decimal]  # None when duration not yet known
    task_id: str


@dataclass(frozen=True)
class CompletionContext:
    """What a metering hook needs to charge/record a finished run."""

    file_id: int
    file_uuid: str
    user_id: int
    organization_id: Optional[int]
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
    """Run pre-dispatch hooks. QuotaExceededError propagates (blocks the job);
    any other hook failure is contained so a broken cloud layer can never
    stop community transcription."""
    for hook in _before_dispatch_hooks:
        try:
            hook(ctx)
        except QuotaExceededError:
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
