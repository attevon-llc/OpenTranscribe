"""Progress reporting types and helpers for the engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class ProgressEvent:
    """A single progress update emitted by any engine stage."""

    progress: float  # 0.0–1.0
    message: str
    stage: str = ""  # e.g. "preprocess", "transcribe", "diarize", "finalize"


ProgressCallback = Callable[[ProgressEvent], None]

# Legacy callback type used by TranscriptionPipeline and Celery tasks.
# Signature: (progress: float, message: str) -> None
LegacyProgressCallback = Callable[[float, str], None]


def adapt_legacy(callback: LegacyProgressCallback | None) -> ProgressCallback | None:
    """Wrap a legacy (float, str) callback into a ProgressCallback."""
    if callback is None:
        return None

    def _wrapped(evt: ProgressEvent) -> None:
        callback(evt.progress, evt.message)

    return _wrapped


def adapt_to_legacy(callback: ProgressCallback | None) -> LegacyProgressCallback | None:
    """Wrap a ProgressCallback into the legacy (float, str) signature."""
    if callback is None:
        return None

    def _wrapped(progress: float, message: str) -> None:
        callback(ProgressEvent(progress=progress, message=message))

    return _wrapped


def emit(
    callback: ProgressCallback | None,
    progress: float,
    message: str,
    stage: str = "",
) -> None:
    """Helper: emit a ProgressEvent if a callback is set."""
    if callback is not None:
        callback(ProgressEvent(progress=progress, message=message, stage=stage))
