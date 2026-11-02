"""Record LLM token usage for every LLM-backed task run (not only chat turns).

Chat records its own ``chat.tokens`` event per assistant message
(``services/chat/usage.py``). This module is the same accounting for everything
else that talks to a model: summarization, topic extraction, speaker
identification and the LLM redaction detector. Same table (``usage_event``), same
unit (tokens), same metadata vocabulary, same containment rule (accounting never
fails the feature).

**Shape.** One row per task *run*, not per provider call: a map-reduce summary of
a long recording makes many calls, and a reader asking "what did summarizing this
file cost" wants one number. ``event_type`` is ``<stage>.tokens`` (see
:data:`STAGE_EVENT_TYPES`), ``quantity`` the total tokens, ``unit`` ``tokens``.

**How a caller cannot forget.** ``LLMService.chat_completion`` reports every call
it makes (success or failure) to the run that is open on the current context via
:func:`note_call`. A task therefore only has to open :func:`llm_usage_run` around
the work; it never counts tokens itself. A static guard
(``tests/unit/test_llm_usage_guard.py``) fails when a module outside chat calls the
LLM service without opening a run.

**Idempotency.** ``llm:<stage>:<file_id>:<run_id>`` where ``run_id`` is the Celery
task id. A retry (``self.retry``) keeps the task id and a broker redelivery
re-runs the same id, so neither can add a second row. The consequence, chosen
deliberately: tokens spent by a *later* attempt of the same task id are not added
to the row an earlier attempt already wrote.

**Tenancy.** Organization and user come from the ``media_file`` row, never from
request state.

**Tokens.** Provider-reported usage is recorded as measured. When a provider
returns none (or the call failed before one arrived) the prompt and reply are
estimated at ~4 characters per token and ``tokens_estimated`` is set, exactly as
chat does, so a guess is never mistaken for a measurement.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

#: ``usage_event.event_type`` per LLM-backed stage. Mirrors chat's ``chat.tokens``.
STAGE_EVENT_TYPES: dict[str, str] = {
    "summary": "summary.tokens",
    "topics": "topics.tokens",
    "speaker_id": "speaker_id.tokens",
    "redaction": "redaction.tokens",
}

UNIT_TOKENS = "tokens"

#: Rough characters per token for the estimate fallback (same ratio chat uses).
_CHARS_PER_TOKEN = 4


@dataclass
class Usage:
    """Provider-reported token counts for one call. ``None`` = not reported."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def reported(self) -> bool:
        return self.prompt_tokens is not None or self.completion_tokens is not None


@dataclass
class CallUsage:
    """Scratch space a provider path fills while one ``chat_completion`` runs."""

    usage: Usage | None = None
    content: str | None = None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def split_usage(data: dict[str, Any]) -> Usage | None:
    """Pull prompt/completion counts out of an OpenAI, Anthropic or Ollama body."""
    usage = data.get("usage")
    if isinstance(usage, dict):
        prompt = _as_int(usage.get("prompt_tokens"))
        completion = _as_int(usage.get("completion_tokens"))
        if prompt is None and completion is None:  # Anthropic names
            prompt = _as_int(usage.get("input_tokens"))
            completion = _as_int(usage.get("output_tokens"))
        if prompt is not None or completion is not None:
            details = usage.get("prompt_tokens_details")
            cached = _as_int(details.get("cached_tokens")) if isinstance(details, dict) else None
            return Usage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                cache_read_tokens=_as_int(usage.get("cache_read_input_tokens")) or cached or 0,
                cache_write_tokens=_as_int(usage.get("cache_creation_input_tokens")) or 0,
            )
    prompt = _as_int(data.get("prompt_eval_count"))
    completion = _as_int(data.get("eval_count"))
    if prompt is not None or completion is not None:  # Ollama
        return Usage(prompt_tokens=prompt, completion_tokens=completion)
    return None


@dataclass
class LLMUsageRun:
    """Accumulator for one task run. Created by :func:`llm_usage_run`."""

    stage: str
    file_id: int | None
    run_id: str
    calls: int = 0
    failed_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tokens_estimated: bool = False
    provider: str | None = None
    model: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def idempotency_key(self) -> str:
        return f"llm:{self.stage}:{self.file_id}:{self.run_id}"[:128]


_current_run: contextvars.ContextVar[LLMUsageRun | None] = contextvars.ContextVar(
    "llm_usage_run", default=None
)


def _estimate(chars: int) -> int:
    return max(1, chars // _CHARS_PER_TOKEN) if chars else 0


def note_call(
    *,
    provider: str | None,
    model: str | None,
    messages: list[dict[str, Any]],
    call: CallUsage,
    success: bool,
) -> None:
    """Fold one provider call into the open run. No-op outside a run (e.g. chat)."""
    run = _current_run.get()
    if run is None:
        return
    usage = call.usage
    with run._lock:
        run.calls += 1
        if not success:
            run.failed_calls += 1
        run.provider = provider or run.provider
        run.model = model or run.model
        if usage is not None and usage.reported:
            run.prompt_tokens += usage.prompt_tokens or 0
            run.completion_tokens += usage.completion_tokens or 0
            run.cache_read_tokens += usage.cache_read_tokens
            run.cache_write_tokens += usage.cache_write_tokens
        else:
            run.tokens_estimated = True
            run.prompt_tokens += _estimate(sum(len(str(m.get("content", ""))) for m in messages))
            run.completion_tokens += _estimate(len(call.content or ""))


def _resolve_owner(db: Any, file_id: int | None) -> tuple[int | None, int | None]:
    if file_id is None:
        return None, None
    from app.models.media import MediaFile

    row = (
        db.query(MediaFile.user_id, MediaFile.organization_id)
        .filter(MediaFile.id == file_id)
        .first()
    )
    if row is None:
        return None, None
    return row[0], row[1]


def _flush(run: LLMUsageRun) -> None:
    """Write the run's single usage event. Raises on a genuine write failure."""
    from app.db.session_utils import session_scope
    from app.services.usage_service import record_event

    total = run.prompt_tokens + run.completion_tokens
    with session_scope() as db:
        user_id, org_id = _resolve_owner(db, run.file_id)
        record_event(
            db,
            event_type=STAGE_EVENT_TYPES[run.stage],
            quantity=Decimal(total),
            unit=UNIT_TOKENS,
            user_id=user_id,
            organization_id=org_id,
            file_id=run.file_id,
            idempotency_key=run.idempotency_key,
            metadata={
                "stage": run.stage,
                "provider": run.provider,
                "model": run.model,
                "prompt_tokens": run.prompt_tokens,
                "completion_tokens": run.completion_tokens,
                "cache_read_tokens": run.cache_read_tokens,
                "cache_write_tokens": run.cache_write_tokens,
                "tokens_estimated": run.tokens_estimated,
                "llm_calls": run.calls,
                "failed_calls": run.failed_calls,
                # False when the run raised or any call failed: the row still carries what
                # was spent before the failure.
                "success": run.failed_calls == 0,
            },
        )


def _default_run_id() -> str:
    with contextlib.suppress(Exception):
        from celery import current_task

        task_id = getattr(getattr(current_task, "request", None), "id", None)
        if task_id:
            return str(task_id)
    return uuid.uuid4().hex


@contextlib.contextmanager
def llm_usage_run(
    stage: str, *, file_id: int | None, run_id: str | None = None
) -> Iterator[LLMUsageRun]:
    """Open a usage run; on exit write exactly one event if any LLM call was made.

    Written on the failure path too (``finally``), so a run that spent tokens and
    then raised still records them. A recording failure is logged at ERROR and
    swallowed: accounting never fails the task. A run that made no provider call
    (no LLM configured) writes nothing.

    Args:
        stage: Key of :data:`STAGE_EVENT_TYPES`.
        file_id: The media file the run works on; owner and organization are read
            from its row.
        run_id: Stable identity of the run. Defaults to the current Celery task id.
    """
    if stage not in STAGE_EVENT_TYPES:
        raise ValueError(f"Unknown LLM usage stage {stage!r}")
    run = LLMUsageRun(stage=stage, file_id=file_id, run_id=run_id or _default_run_id())
    token = _current_run.set(run)
    try:
        yield run
    finally:
        _current_run.reset(token)
        if run.calls:
            try:
                _flush(run)
            except Exception:  # accounting never breaks the feature
                logger.exception(
                    "Failed to record %s LLM usage for file %s (run %s)",
                    stage,
                    file_id,
                    run.run_id,
                )
