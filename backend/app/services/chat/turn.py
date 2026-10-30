"""One chat turn's state and its cancellation signal.

Shared by the orchestrator (``service.py``) and the phase helpers it calls: the
helpers raise :class:`_TurnCancelled` at their cancellation checkpoints, and the
streaming half accumulates everything it persists on a :class:`ChatTurn`.
"""

from __future__ import annotations

from typing import Any

from app.models.chat import STATUS_CANCELLED
from app.models.chat import STATUS_COMPLETE
from app.models.chat import STATUS_ERROR
from app.services.chat import limits


class _TurnCancelled(Exception):  # noqa: N818 — internal control flow, not surfaced
    """Raised inside ``_prepare_context`` when a Stop lands mid-phase.

    #403 threads a cancel check into ``_prepare_context``'s phase
    boundaries, between leg submissions, and before the planner and
    enrichment calls specifically — those are the calls a cancellation must
    prevent from ever billing, unlike the phases already in flight when Stop
    is pressed. Caught in ``stream_reply`` exactly like ``GeneratorExit``:
    no error is set, so the turn's ``finally`` records it as the ordinary
    cancellation it is.
    """


def _check_cancelled(assistant_message_uuid: str) -> None:
    """Raise :class:`_TurnCancelled` if a Stop was requested for this turn."""
    if limits.is_cancelled(assistant_message_uuid):
        raise _TurnCancelled()


class ChatTurn:
    """One user message and the assistant reply it produces.

    Holds the mutable state a turn accumulates (answer text, token counts,
    diagnostics) so the streaming generator stays readable and the ``finally``
    block has one place to persist from.
    """

    def __init__(self) -> None:
        self.answer_parts: list[str] = []
        # Separate from answer_parts so the collapsible reasoning block in the UI
        # never gets reasoning tokens mixed into the rendered answer.
        self.reasoning_parts: list[str] = []
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None
        # Cache tokens are priced differently from ordinary input tokens — reads far
        # below, writes above — so they are carried separately rather than folded in.
        self.cache_read_tokens: int | None = None
        self.cache_write_tokens: int | None = None
        self.tokens_estimated = False
        # #403: tokens spent on bounded, non-generation LLM calls this
        # turn made (planner, enrichment, and the planner's share of a
        # follow-up's extended rewrite call) — folded into `prompt_tokens`/
        # `completion_tokens` in `_finalize_turn`, right before
        # `record_chat_usage` reads them, so those calls are metered through
        # the SAME single hook the answer stream already goes through
        # rather than needing a second accounting path.
        self.extra_prompt_tokens = 0
        self.extra_completion_tokens = 0
        self.finish_reason: str | None = None
        self.error: str | None = None
        self.error_code: str | None = None
        self.offered_citations: list[dict] = []
        self.metadata: dict[str, Any] = {}
        # Filled in by _finalize_turn so the trailing frames can read them.
        self.total_tokens = 0
        self.title: str | None = None

    @property
    def answer(self) -> str:
        return "".join(self.answer_parts)

    @property
    def reasoning(self) -> str:
        return "".join(self.reasoning_parts)

    def status(self) -> str:
        if self.error:
            return STATUS_ERROR
        if self.finish_reason == "cancelled":
            return STATUS_CANCELLED
        return STATUS_COMPLETE
