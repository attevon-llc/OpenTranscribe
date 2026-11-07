"""Usage events for metered LLM calls made outside a task run (chat's bounded calls).

``LLMMeteringMixin._account_call`` settles each call against the token limits. A call inside an
``llm_usage_run`` is folded into that run's single event (``services/llm_usage.py``). A call with
no run open (chat's planner, query rewrite, enrichment and overview map-reduce) used to count
against the limit and leave no event, so the usage panel read lower than the limit counter.
This module closes that gap AT the settle seam: :func:`record_unscoped_call` writes one
``chat.prep.tokens`` event per such call, from the same token count the settle uses.

The answer stream is the one metered call recorded elsewhere: its ``chat.tokens`` event is
written per assistant message by ``services/chat/usage.py``.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import uuid
from collections.abc import Callable
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

from app.services import llm_usage

logger = logging.getLogger(__name__)

#: ``usage_event.event_type`` of a chat turn's bounded LLM calls other than the answer. The usage
#: API reports it under the ``chat.tokens`` feature (``FEATURE_OF_EVENT_TYPE``).
EVENT_TYPE_CHAT_PREP_TOKENS = "chat.prep.tokens"

#: Event types that roll up into another feature in the usage panel.
FEATURE_OF_EVENT_TYPE: dict[str, str] = {EVENT_TYPE_CHAT_PREP_TOKENS: "chat.tokens"}

_scope: contextvars.ContextVar[tuple[int, int | None] | None] = contextvars.ContextVar(
    "llm_call_scope", default=None
)


@contextlib.contextmanager
def chat_call_scope(user_id: int, organization_id: int | None) -> Iterator[None]:
    """Name whose usage the bounded calls inside the block belong to (user and tenant stamp)."""
    token = _scope.set((user_id, organization_id))
    try:
        yield
    finally:
        _scope.reset(token)


def record_unscoped_call(
    *,
    budget_user_id: int | None,
    provider: str | None,
    model: str | None,
    messages: list[dict[str, Any]],
    call: llm_usage.CallUsage,
    success: bool,
    local: Callable[[], bool],
) -> None:
    """Write one event for a settled call that has no open usage run. Never raises."""
    if llm_usage._current_run.get() is not None:
        return
    scope = _scope.get()
    user_id = scope[0] if scope else budget_user_id
    organization_id = scope[1] if scope else None
    prompt, completion, estimated = llm_usage.call_tokens(messages, call)
    usage = call.usage
    try:
        from app.db.session_utils import session_scope
        from app.services.usage_service import record_event

        is_local = bool(local())
        with session_scope() as db:
            record_event(
                db,
                event_type=EVENT_TYPE_CHAT_PREP_TOKENS,
                quantity=Decimal(prompt + completion),
                unit=llm_usage.UNIT_TOKENS,
                user_id=user_id,
                organization_id=organization_id,
                idempotency_key=f"llm-call:{uuid.uuid4().hex}",
                metadata={
                    "provider": provider,
                    "model": model,
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "cache_read_tokens": 0
                    if estimated or usage is None
                    else usage.cache_read_tokens,
                    "cache_write_tokens": 0
                    if estimated or usage is None
                    else usage.cache_write_tokens,
                    "tokens_estimated": estimated,
                    "local": is_local,
                    "success": success,
                },
            )
    except Exception:  # accounting never breaks the feature
        logger.exception("Failed to record usage for an LLM call outside a task run")
