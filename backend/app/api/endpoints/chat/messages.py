"""Chat messages: history, streaming send, regenerate, cancel, context estimate.

The send endpoint is the feature's hot path. Everything that can fail cleanly —
authorization, rate limits, quota, LLM availability, scope resolution — is
resolved BEFORE the ``StreamingResponse`` is constructed, so those failures are
ordinary HTTP status codes. Once the stream opens, the status line is committed
and every remaining failure has to arrive as an SSE ``error`` frame instead.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.chat.common import get_owned_conversation
from app.api.endpoints.chat.common import read_user_chat_settings
from app.api.endpoints.chat.common import resolve_use_context
from app.auth.rate_limit import limiter
from app.core.tenant_limits import resolve_allowed_models
from app.db.base import get_db
from app.models.chat import ROLE_ASSISTANT
from app.models.chat import ROLE_USER
from app.models.chat import STATUS_SUPERSEDED
from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.schemas.chat import MAX_MESSAGE_CHARS
from app.schemas.chat import ChatScope
from app.schemas.chat import ContextEstimate
from app.schemas.chat import MessageCreate
from app.schemas.chat import MessageList
from app.services.chat import limits
from app.services.chat.context_resolver import count_scope_files
from app.services.chat.context_resolver import resolve_scope_file_uuids
from app.services.chat.hooks import ChatDispatchContext
from app.services.chat.hooks import fire_before_message
from app.services.chat.prompting import build_system_prompt
from app.services.chat.service import ChatService
from app.services.chat.service import sse
from app.services.chat.settings import apply_tenant_limits
from app.services.chat.settings import get_chat_settings
from app.services.llm_service import LLMService

logger = logging.getLogger(__name__)

router = APIRouter()

# Streaming responses must not be buffered by nginx or any intermediary, or the
# whole answer arrives at once and the feature loses its point.
STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# Rough tokens-per-file estimate for the context sizing hint (a typical
# transcript chunk set, not the full transcript — chat retrieves excerpts).
_ESTIMATED_TOKENS_PER_FILE = 700


def _serialize_message(message: ChatMessage) -> dict:
    return {
        "uuid": str(message.uuid),
        "role": message.role,
        "content": message.content,
        "citations": message.citations,
        "msg_metadata": message.msg_metadata,
        "prompt_tokens": message.prompt_tokens,
        "completion_tokens": message.completion_tokens,
        "total_tokens": message.total_tokens,
        "tokens_estimated": bool(message.tokens_estimated),
        "provider": message.provider,
        "model": message.model,
        "status": message.status,
        "error": message.error,
        "created_at": message.created_at,
    }


def _history_for_prompt(db: Session, conversation_id: int, max_turns: int) -> list[dict[str, str]]:
    """Recent completed turns, oldest first. Superseded/errored turns are skipped."""
    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.status.notin_([STATUS_SUPERSEDED, "error"]),
        )
        .order_by(ChatMessage.id.desc())
        .limit(max_turns * 2)
        .all()
    )
    return [{"role": row.role, "content": row.content} for row in reversed(rows) if row.content]


@router.get("/conversations/{conversation_uuid}/messages", response_model=MessageList)
def list_messages(
    conversation_uuid: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> MessageList:
    """Replay a conversation, oldest first, with citations inline."""
    conversation = get_owned_conversation(db, ctx, conversation_uuid)

    query = db.query(ChatMessage).filter(ChatMessage.conversation_id == conversation.id)
    total = query.count()
    rows = query.order_by(ChatMessage.id.asc()).offset(offset).limit(limit).all()

    return MessageList(
        messages=[_serialize_message(row) for row in rows],  # type: ignore[misc]
        total=total,
        limit=limit,
        offset=offset,
    )


def _prepare_turn(
    request: Request,
    db: Session,
    ctx: RequestContext,
    conversation: ChatConversation,
    content: str,
    search_mode: str | None,
) -> dict:
    """Resolve everything that can fail with a clean HTTP status.

    Returns the keyword arguments for :meth:`ChatService.stream_reply`.

    Raises:
        HTTPException: 400 (no LLM / oversized scope), 402 (quota), 429 (limits).
    """
    # Narrow the admin's settings by any per-tenant ceiling before anything reads
    # them, so the rate-limit checks and the retrieval budget below both see the
    # tightened values. A no-op in the community edition.
    chat_settings = apply_tenant_limits(get_chat_settings(db), ctx.org_id)
    user_defaults = read_user_chat_settings(db, ctx.user.id)

    allowed, retry_after = limits.check_hourly_limit(ctx.user.id, chat_settings.messages_per_hour)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Hourly chat limit reached. Try again shortly.",
            headers={"Retry-After": str(retry_after)},
        )

    if not limits.acquire_stream_slot(ctx.user.id, chat_settings.max_concurrent_streams):
        raise HTTPException(
            status_code=429,
            detail="Too many chats streaming at once. Wait for one to finish.",
            headers={"Retry-After": "5"},
        )

    try:
        llm = (
            LLMService.create_from_config_id(ctx.user.id, int(conversation.llm_config_id))
            if conversation.llm_config_id is not None
            else LLMService.create_from_settings(user_id=ctx.user.id)
        )
        if llm is None:
            raise HTTPException(
                status_code=400,
                detail="No LLM is configured. Add a provider in Settings → AI to use chat.",
            )

        # A tenant may be restricted to a subset of models. Enforced HERE rather than
        # only in the UI: the per-conversation model is user-supplied
        # (`conversation.llm_config_id`), so a client that skips the picker would
        # otherwise pin any model it liked. An empty set means no model is permitted,
        # which is how a suspended tenant is expressed.
        allowed_models = resolve_allowed_models(ctx.org_id)
        if allowed_models is not None and str(llm.config.model) not in allowed_models:
            raise HTTPException(
                status_code=403,
                detail="That model is not available on your plan. Choose another in chat settings.",
            )

        conv_settings = conversation.settings or {}
        use_context = resolve_use_context(conversation, user_defaults)

        file_uuids = None
        speakers: list[str] = []
        if use_context:
            scope = ChatScope(**conversation.scope)
            file_uuids = resolve_scope_file_uuids(db, ctx, scope)
            # Speakers filter WITHIN the resolved recordings rather than
            # reducing them, so it is passed straight to retrieval.
            speakers = scope.speakers

        assistant_uuid = str(uuid_pkg.uuid4())
        fire_before_message(
            ChatDispatchContext(
                conversation_uuid=str(conversation.uuid),
                user_id=ctx.user.id,
                organization_id=ctx.org_id,
                provider=str(llm.config.provider.value),
                model=str(llm.config.model),
                request_id=assistant_uuid,
            )
        )

        history = _history_for_prompt(db, conversation.id, chat_settings.history_max_turns)
        is_first_exchange = not history

        user_message = ChatMessage(
            conversation_id=conversation.id,
            role=ROLE_USER,
            content=content,
        )
        db.add(user_message)
        conversation.last_message_at = datetime.now(UTC)
        db.commit()
        db.refresh(user_message)

        system_prompt = build_system_prompt(
            use_context=use_context,
            user_system_prompt=user_defaults["system_prompt"],
            conversation_system_prompt=conv_settings.get("system_prompt"),
            speakers=speakers,
        )

        return {
            "conversation_id": conversation.id,
            "conversation_uuid": str(conversation.uuid),
            "user_id": ctx.user.id,
            "organization_id": ctx.org_id,
            "question": content,
            "history": history,
            "file_uuids": file_uuids,
            "speakers": speakers,
            "settings": chat_settings,
            "use_context": use_context,
            "system_prompt": system_prompt,
            "search_mode": search_mode
            or conv_settings.get("search_mode")
            or user_defaults["default_search_mode"],
            "temperature": conv_settings.get("temperature"),
            "llm": llm,
            "assistant_message_uuid": assistant_uuid,
            "user_message_uuid": str(user_message.uuid),
            "is_first_exchange": is_first_exchange,
        }
    except Exception:
        # Anything that fails before the stream starts must give the slot back.
        limits.release_stream_slot(ctx.user.id)
        raise


def _streaming_response(kwargs: dict, user_id: int) -> StreamingResponse:
    """Wrap the turn generator so the concurrency slot is always released."""

    async def guarded():
        try:
            async for frame in ChatService.stream_reply(**kwargs):
                yield frame
        except Exception:  # noqa: BLE001 — the stream is already open
            logger.exception("Chat stream generator failed")
            yield sse("error", {"code": "provider_error", "message": "Generation failed."})
        finally:
            limits.release_stream_slot(user_id)

    return StreamingResponse(guarded(), media_type="text/event-stream", headers=STREAM_HEADERS)


@router.post("/conversations/{conversation_uuid}/messages")
@limiter.limit("20/minute")
async def send_message(
    request: Request,
    conversation_uuid: str,
    body: MessageCreate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
):
    """Send a message and stream the assistant's reply as SSE.

    Frames: ``start``, ``status``, ``sources``, ``delta``, ``usage``, ``done``,
    ``error``. The request body is POSTed (never a query string) so message text
    never lands in access logs or browser history.
    """
    conversation = get_owned_conversation(db, ctx, conversation_uuid)
    # _prepare_turn is fully synchronous (Redis, key decryption, per-file
    # permission checks, a commit). Calling it directly from an async handler
    # would block the event loop — and with up to 100 files each fanning out into
    # permission queries, that stalls every other request in the process,
    # including other users' in-flight SSE streams.
    kwargs = await run_in_threadpool(
        _prepare_turn, request, db, ctx, conversation, body.content, body.search_mode
    )
    return _streaming_response(kwargs, ctx.user.id)


@router.post("/conversations/{conversation_uuid}/regenerate")
@limiter.limit("20/minute")
async def regenerate(
    request: Request,
    conversation_uuid: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
):
    """Re-answer the last user message, superseding the previous reply."""
    conversation = get_owned_conversation(db, ctx, conversation_uuid)

    last_user = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.conversation_id == conversation.id,
            ChatMessage.role == ROLE_USER,
        )
        .order_by(ChatMessage.id.desc())
        .first()
    )
    if last_user is None:
        raise HTTPException(status_code=400, detail="Nothing to regenerate")

    # Prepare FIRST. _prepare_turn can still fail with 429/400/402, and
    # superseding before that would retire the user's question with nothing to
    # replace it — it vanishes from the thread, from prompt history and from
    # export, with no answer and no way back.
    question = str(last_user.content)
    kwargs = await run_in_threadpool(_prepare_turn, request, db, ctx, conversation, question, None)

    db.query(ChatMessage).filter(
        ChatMessage.conversation_id == conversation.id,
        ChatMessage.role == ROLE_ASSISTANT,
        ChatMessage.id > last_user.id,
    ).update({ChatMessage.status: STATUS_SUPERSEDED}, synchronize_session=False)
    # The question is re-sent as a fresh turn, so retire the original too.
    last_user.status = STATUS_SUPERSEDED
    db.commit()

    return _streaming_response(kwargs, ctx.user.id)


class MessageEdit(BaseModel):
    """Replace a user message and re-answer from that point."""

    content: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS)


@router.post("/conversations/{conversation_uuid}/messages/{message_uuid}/edit")
@limiter.limit("20/minute")
async def edit_message(
    request: Request,
    conversation_uuid: str,
    message_uuid: str,
    body: MessageEdit,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
):
    """Rewrite one of your questions and re-answer from there.

    A conversation is a chain: changing an earlier question invalidates every
    answer that followed it. Rather than delete that history (which would lose
    the audit trail and any citations already acted on), the tail is marked
    ``superseded`` — hidden from the thread and excluded from prompt history,
    but still on record.
    """
    conversation = get_owned_conversation(db, ctx, conversation_uuid)

    target = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.uuid == message_uuid,
            ChatMessage.conversation_id == conversation.id,
            ChatMessage.role == ROLE_USER,
        )
        .first()
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Message not found")

    # Prepare FIRST — see regenerate() for why superseding before a possible
    # 429/400/402 would lose the user's question outright. Note the ordering
    # consequence: _prepare_turn persists the REPLACEMENT question, so the
    # supersede below must be bounded to rows that existed before it, or it
    # would immediately retire the very message it just created.
    boundary = (
        db.query(func.max(ChatMessage.id))
        .filter(ChatMessage.conversation_id == conversation.id)
        .scalar()
    )
    kwargs = await run_in_threadpool(
        _prepare_turn, request, db, ctx, conversation, body.content, None
    )

    # Retire the edited question and everything downstream of it.
    db.query(ChatMessage).filter(
        ChatMessage.conversation_id == conversation.id,
        ChatMessage.id >= target.id,
        ChatMessage.id <= boundary,
    ).update({ChatMessage.status: STATUS_SUPERSEDED}, synchronize_session=False)
    db.commit()

    return _streaming_response(kwargs, ctx.user.id)


@router.post("/messages/{message_uuid}/cancel")
@limiter.limit("60/minute")
def cancel_message(
    request: Request,
    message_uuid: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> dict:
    """Ask an in-flight generation to stop.

    Belt-and-braces beside client disconnect: a user on a flaky connection may
    hit Stop over a new connection while the original request is still open.

    Ownership is checked even though the id is a uuid4. Without it any
    authenticated user could cancel a generation whose id they learned, and —
    more practically — an unvalidated path segment became a Redis key, letting a
    loop of requests write arbitrary 600s-TTL entries. The rate limit bounds that
    further.
    """
    try:
        parsed = uuid_pkg.UUID(message_uuid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Message not found") from exc

    owned = (
        db.query(ChatMessage.id)
        .join(ChatConversation, ChatConversation.id == ChatMessage.conversation_id)
        .filter(
            ChatMessage.uuid == parsed,
            ChatConversation.user_id == ctx.user.id,
        )
        .first()
    )
    if owned is None:
        raise HTTPException(status_code=404, detail="Message not found")

    limits.request_cancel(str(parsed))
    return {"status": "cancelling", "message_uuid": message_uuid}


@router.post("/context/estimate", response_model=ContextEstimate)
def estimate_context(
    body: ChatScope,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> ContextEstimate:
    """Estimate how much of the model's context a selection would occupy.

    Advisory only — the prompt builder enforces the real budget. This exists so
    the picker can warn before someone selects 400 recordings.
    """
    file_count = count_scope_files(db, ctx, body)
    llm = LLMService.create_from_settings(user_id=ctx.user.id)
    context_window = llm.user_context_window if llm else 8192

    estimated = file_count * _ESTIMATED_TOKENS_PER_FILE
    pct = (estimated / context_window * 100) if context_window else 0.0
    level = "ok" if pct < 70 else ("warn" if pct < 100 else "over")

    return ContextEstimate(
        file_count=file_count,
        estimated_tokens=estimated,
        context_window=context_window,
        pct=round(pct, 1),
        warning_level=level,  # type: ignore[arg-type]
    )
