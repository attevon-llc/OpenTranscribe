"""The token stream's helpers: output redaction, generation kwargs, the first token.

``redactor.py`` masks what the model is GIVEN (egress); this is the display half
around ``output_redactor.OutputRedactor``, which masks what the model WRITES. The
policy is resolved on every turn, ``use_context=False`` ones included, and an
unresolvable policy (``None``) makes the redactor mask everything.

``_resolve_output_policy`` is called through ``service.py``'s module globals (from
``ChatService.stream_reply``), which is where tests patch it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from starlette.concurrency import run_in_threadpool

from app.services.chat.output_redactor import OutputRedactor
from app.services.chat.turn import ChatTurn

logger = logging.getLogger(__name__)


def _resolve_output_policy(user_id: int, organization_id: int | None = None):
    """The requesting user's effective redaction config, or None if unresolvable.

    Its own short-lived session: this runs on every turn including
    ``use_context=False`` ones, which never open the retrieval session at all.
    ``None`` is not "no redaction" — ``OutputRedactor`` reads it as "mask
    everything", because being unable to resolve the policy must not mean
    sending generated text out unexamined.

    Args:
        organization_id: The requester's active tenant scope, threaded into
            ``resolve_effective_config`` so a registered per-org redaction
            floor (issue #982/#987) is actually consulted (#988).
    """
    from app.db.session_utils import session_scope
    from app.services.redaction.config import resolve_effective_config

    try:
        with session_scope() as db:
            return resolve_effective_config(db, user_id, organization_id=organization_id)
    except Exception:  # noqa: BLE001 — the redactor fails closed on None
        logger.exception("Could not resolve the output redaction policy for user %s", user_id)
        return None


async def _redact_delta(redactor: OutputRedactor, text: str) -> str:
    """Buffer one generated delta; return what is safe to put on the wire now.

    Returns ``""`` while a sentence is still arriving. Detection is CPU-bound
    (Presidio), so it goes to a thread — buffering itself is string work and
    stays on the loop.
    """
    if not redactor.active:
        return text
    span = redactor.buffer(text)
    if not span:
        return ""
    # Annotated: run_in_threadpool is typed as returning Any, and returning it
    # straight out of a str-declared function trips no-any-return.
    # OutputRedactor.mask genuinely returns str.
    masked: str = await run_in_threadpool(redactor.mask, span)
    return masked


def _record_output_redaction(
    turn: ChatTurn, answer: OutputRedactor | None, reasoning: OutputRedactor | None
) -> None:
    """Stamp what output redaction did on the turn's metadata.

    Only when it was active, so a deployment with redaction off carries no new
    key. ``withheld_spans`` is the one that matters: a sentence replaced by the
    failsafe placeholder is otherwise indistinguishable from a short answer.
    """
    if answer is None or not answer.active:
        return
    withheld = answer.withheld_spans + (reasoning.withheld_spans if reasoning else 0)
    turn.metadata["output_redaction"] = {
        "masked_spans": answer.masked_spans + (reasoning.masked_spans if reasoning else 0),
        "withheld_spans": withheld,
    }
    turn.metadata.setdefault("timings_ms", {})["output_redaction"] = answer.mask_ms + (
        reasoning.mask_ms if reasoning else 0
    )
    if withheld:
        logger.warning(
            "Chat output redaction withheld %d generated span(s): detectors were "
            "unavailable for an enabled category, so the text was replaced rather than sent",
            withheld,
        )


async def _flush_redactor(redactor: OutputRedactor | None) -> str:
    """Mask and return the unemitted tail. Idempotent — later calls return ``""``."""
    if redactor is None:
        return ""
    tail = redactor.drain()
    if not tail:
        return ""
    if not redactor.active:
        return tail
    masked_tail: str = await run_in_threadpool(redactor.mask, tail)
    return masked_tail


def _generation_kwargs(
    answer_tokens: int,
    *,
    temperature: float | None,
    top_p: float | None,
    enable_thinking: bool | None,
) -> dict[str, Any]:
    """The provider call's keyword arguments: only what this turn actually overrides."""
    kwargs: dict[str, Any] = {"max_tokens": answer_tokens}
    if temperature is not None:
        kwargs["temperature"] = temperature
    # Omitted entirely when unset: not every provider accepts top_p, and
    # sending a "default" would override a provider-side tuned value.
    if top_p is not None:
        kwargs["top_p"] = top_p
    # Only ever narrows: None leaves the provider payload untouched.
    if enable_thinking is not None:
        kwargs["enable_thinking"] = enable_thinking
    return kwargs


def _mark_first_token(turn: ChatTurn, got_first_token: bool, started: float) -> bool:
    """Stamp ``timings_ms.first_token`` on the provider's first text event; return True."""
    if not got_first_token:
        turn.metadata.setdefault("timings_ms", {})["first_token"] = int(
            (time.monotonic() - started) * 1000
        )
    return True
