"""Chat pipeline hooks (cloud-edition seam).

Mirrors ``app.tasks.transcription.hooks`` exactly. Community defaults are no-ops
with zero overhead; the commercial cloud layer registers:
  - a before-message hook for quota reservation (raising
    ``ChatQuotaExceededError`` -> HTTP 402 with an upgrade prompt), and
  - a message-complete hook for token metering (idempotent on ``message_uuid``).

Hook-author contract (identical to the transcription seam):
  - THROWING is contained: any exception except ``ChatQuotaExceededError`` is
    logged and swallowed — a broken hook can never fail a chat message.
  - HANGING is NOT contained: the complete-hook runs inside the streaming
    response's teardown. Every outbound call MUST carry a tight timeout; durable
    work belongs on a queue the hook merely enqueues to.
  - Hooks receive frozen dataclass contexts, never the request's DB session —
    open your own ``session_scope()`` for any DB work.

Fire points are chosen so quota rejection is a clean HTTP 402 rather than a
mid-stream error frame: ``fire_before_message`` runs BEFORE the StreamingResponse
is constructed.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException

logger = logging.getLogger(__name__)


class ChatQuotaExceededError(HTTPException):
    """Raised by a before-message hook when the tenant is over quota."""

    def __init__(self, detail: str = "Chat quota exceeded"):
        super().__init__(status_code=402, detail=detail)


@dataclass(frozen=True)
class ChatDispatchContext:
    """What a quota hook needs to decide whether a message may be sent."""

    conversation_uuid: str
    user_id: int
    organization_id: int | None
    provider: str
    model: str
    request_id: str  # the assistant message uuid


@dataclass(frozen=True)
class ChatCompletionContext:
    """What a metering hook needs to record a finished exchange.

    ``message_uuid`` is the idempotency scope: the cloud hook records
    ``chat.messages``, ``chat.tokens.prompt`` and ``chat.tokens.completion``
    usage events keyed on it, so retries and replays cannot double-charge.
    ``tokens_estimated`` flags counts the provider did not report.
    """

    conversation_uuid: str
    message_uuid: str
    user_id: int
    organization_id: int | None
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    tokens_estimated: bool
    retrieved_chunks: int
    success: bool


BeforeMessageHook = Callable[[ChatDispatchContext], None]
MessageCompleteHook = Callable[[ChatCompletionContext], None]

_before_message_hooks: list[BeforeMessageHook] = []
_message_complete_hooks: list[MessageCompleteHook] = []


def register_before_message(hook: BeforeMessageHook) -> None:
    """Register a pre-send hook (cloud: quota reservation)."""
    _before_message_hooks.append(hook)
    logger.info("Registered before-message chat hook")


def register_message_complete(hook: MessageCompleteHook) -> None:
    """Register a completion hook (cloud: token metering + usage events)."""
    _message_complete_hooks.append(hook)
    logger.info("Registered message-complete chat hook")


def clear_hooks() -> None:
    """Remove all registered hooks (primarily for tests)."""
    _before_message_hooks.clear()
    _message_complete_hooks.clear()


def fire_before_message(ctx: ChatDispatchContext) -> None:
    """Run pre-send hooks. ChatQuotaExceededError propagates (blocks the message);
    any other hook failure is contained so a broken cloud layer can never stop
    community chat."""
    for hook in _before_message_hooks:
        try:
            hook(ctx)
        except ChatQuotaExceededError:
            raise
        except Exception:
            logger.exception("before-message hook failed; allowing message")


def fire_message_complete(ctx: ChatCompletionContext) -> None:
    """Run completion hooks. Failures are contained — a metering problem must
    never turn a delivered answer into an error."""
    for hook in _message_complete_hooks:
        try:
            hook(ctx)
        except Exception:
            logger.exception("message-complete hook failed (contained)")
