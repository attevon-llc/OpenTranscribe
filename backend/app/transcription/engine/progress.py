"""Progress reporting types and helpers for the engine."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass

from app.core.enums import TranscriptionStage


@dataclass
class ProgressEvent:
    """A single progress update emitted by any engine stage."""

    progress: float  # 0.0–1.0
    message: str
    stage: TranscriptionStage


ProgressCallback = Callable[[ProgressEvent], None]

# Callback type used by TranscriptionPipeline and Celery tasks: (progress, message), or
# (progress, message, stage=...) for a callback that forwards the stage to the client.
LegacyProgressCallback = Callable[..., None]


def _accepts_stage(callback: Callable[..., None]) -> bool:
    """Whether ``callback`` can be called as ``callback(progress, message, stage=...)``."""
    try:
        inspect.signature(callback).bind(0.0, "", stage=TranscriptionStage.PREPARE)
    except (TypeError, ValueError):
        return False
    return True


def adapt_legacy(callback: LegacyProgressCallback | None) -> ProgressCallback | None:
    """Wrap a (progress, message) callback into a ProgressCallback.

    A callback that also takes a ``stage`` keyword receives the event's stage; a plain
    two-argument one keeps working and simply does not see it. Decided once, at wrap time.
    """
    if callback is None:
        return None

    if _accepts_stage(callback):

        def _staged(evt: ProgressEvent) -> None:
            callback(evt.progress, evt.message, stage=evt.stage)

        return _staged

    def _wrapped(evt: ProgressEvent) -> None:
        callback(evt.progress, evt.message)

    return _wrapped


def emit(
    callback: ProgressCallback | None,
    progress: float,
    message: str,
    stage: TranscriptionStage,
) -> None:
    """Helper: emit a ProgressEvent if a callback is set."""
    if callback is not None:
        callback(ProgressEvent(progress=progress, message=message, stage=stage))
