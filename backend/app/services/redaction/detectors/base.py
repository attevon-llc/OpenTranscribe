"""Detector protocol for content redaction.

Detectors emit ``RedactionSpan`` lists (toxicity emits per-segment scores instead).
Heavy ML detectors are loaded lazily and run only in the celery-redaction worker.
"""

from __future__ import annotations

from typing import Protocol
from typing import runtime_checkable

from app.services.redaction.spans import RedactionSpan


@runtime_checkable
class Detector(Protocol):
    """A span-producing detector (wordlist, presidio/gliner, llm)."""

    name: str

    def detect(self, text: str, words: list[dict] | None, cfg: dict) -> list[RedactionSpan]:
        """Return redaction spans for a single segment's text."""
        ...
