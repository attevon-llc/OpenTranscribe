"""An in-memory, bounded ``TraceRecorder`` for tests that assert what a turn recorded."""

from __future__ import annotations

from app.services.chat.trace import TraceEvent


class ListTraceRecorder:
    """Collects events in memory, up to ``cap``; later events are dropped and flagged.

    Bounded on purpose, like the production recorders: a pathological fan-out must not grow a
    turn's memory without limit, and a trace is a diagnostic, not a ledger.
    """

    __slots__ = ("events", "_cap", "_truncated")

    def __init__(self, cap: int = 200) -> None:
        self.events: list[TraceEvent] = []
        self._cap = cap
        self._truncated = False

    @property
    def truncated(self) -> bool:
        """True when events were dropped, so a reader is never silently misled."""
        return self._truncated

    def record(self, event: TraceEvent) -> None:
        if len(self.events) >= self._cap:
            self._truncated = True
            return
        self.events.append(event)
