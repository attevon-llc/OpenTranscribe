"""Token metering and limits around every provider call (mixed into ``LLMService``).

The ONE seam every LLM feature passes through (issue #790): :meth:`chat_completion` and
:meth:`chat_completion_stream` check the token limits (``llm_budget``) before the provider is
reached and report each call to the open usage run (``llm_usage``) afterwards. The provider
dispatch itself lives in ``LLMService._chat_completion_untracked`` /
``_chat_completion_stream_untracked``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import TYPE_CHECKING
from typing import Any

from app.services import llm_call_events
from app.services.llm_stream import LLMStreamEvent
from app.services.llm_types import LLMHost
from app.services.llm_types import LLMResponse

if TYPE_CHECKING:  # pragma: no cover
    from app.services.llm_budget import Reservation
    from app.services.llm_usage import CallUsage


class LLMMeteringMixin(LLMHost):
    """Token limits and usage accounting for each provider call."""

    #: Whose daily token limit this service's calls count against. Set by the per-user
    #: factories; ``None`` falls back to the owner of the open usage run's file
    #: (``llm_usage.current_run_owner``), then to the instance limit alone.
    budget_user_id: int | None = None
    _local: bool | None = None

    if TYPE_CHECKING:

        def _chat_completion_untracked(
            self, messages: list[dict[str, str]], call: CallUsage, **kwargs: Any
        ) -> LLMResponse: ...

        def _chat_completion_stream_untracked(
            self,
            messages: list[dict[str, str]],
            cancel_event: threading.Event | None = None,
            **kwargs: Any,
        ) -> Iterator[LLMStreamEvent]: ...

    def is_local(self) -> bool:
        """Whether this service reaches a model on this deployment (``is_local_provider``).

        Once a call has pinned the endpoint, the answer is taken from the address it dials
        (no second lookup); before that it may resolve the host name. Computed once per service.
        """
        if self._local is None:
            from app.services.redaction.llm_guard import is_local_provider
            from app.services.redaction.llm_guard import is_local_provider_at

            pinned = getattr(self, "_pinned", None)
            self._local = (
                is_local_provider_at(self.config, pinned[2].address)
                if pinned is not None
                else is_local_provider(self.config)
            )
        return self._local

    def _budget_subject(self) -> int | None:
        if self.budget_user_id is not None:
            return self.budget_user_id
        from app.services import llm_usage

        return llm_usage.current_run_owner()

    def _reserve_budget(
        self, messages: list[dict[str, str]], kwargs: dict[str, Any]
    ) -> Reservation:
        """Reserve this call's tokens under the token limits (``services/llm_budget.py``).

        Raises:
            LLMBudgetExceededError: refused; also recorded on the open usage run.
        """
        from app.services import llm_budget
        from app.services import llm_usage

        reply_budget = kwargs.get("max_tokens") or self.config.response_tokens
        try:
            return llm_budget.reserve(
                user_id=self._budget_subject,
                amount=llm_budget.estimate_tokens(messages, reply_budget),
                local=self.is_local,
            )
        except llm_budget.LLMBudgetExceededError as refused:
            llm_usage.note_refusal(refused)
            raise

    def chat_completion(
        self, messages: list[dict[str, str]], *, enforce_budget: bool = True, **kwargs: Any
    ) -> LLMResponse:
        """Send a chat completion request to the LLM provider.

        Every call, successful or not, is reported to the open usage run
        (``services/llm_usage.py``) so no task has to count tokens itself, and is checked
        against the token limits first (``services/llm_budget.py``).

        Args:
            messages: Messages in OpenAI format.
            enforce_budget: ``False`` only for calls that are not product features: the
                connection health probe and the offline summary benchmark.
            **kwargs: Passed to the payload builder.

        Raises:
            LLMBudgetExceededError: The token limits refused the call; nothing was sent.
        """
        from app.services import llm_budget
        from app.services import llm_usage

        reservation = (
            self._reserve_budget(messages, kwargs) if enforce_budget else llm_budget.NO_RESERVATION
        )
        call = llm_usage.CallUsage()
        try:
            response = self._chat_completion_untracked(messages, call, **kwargs)
        except Exception:
            self._account_call(
                messages, call, reservation, success=False, feature_call=enforce_budget
            )
            raise
        call.content = response.content
        self._account_call(messages, call, reservation, success=True, feature_call=enforce_budget)
        return response

    def _account_call(
        self,
        messages: list[dict[str, str]],
        call: CallUsage,
        reservation: Reservation,
        *,
        success: bool,
        feature_call: bool = True,
    ) -> None:
        from app.services import llm_budget
        from app.services import llm_usage

        llm_usage.note_call(
            provider=self.config.provider.value,
            model=self.config.model,
            messages=messages,
            call=call,
            success=success,
            local=self.is_local,
        )
        if feature_call:
            llm_call_events.record_unscoped_call(
                budget_user_id=self.budget_user_id,
                provider=self.config.provider.value,
                model=self.config.model,
                messages=messages,
                call=call,
                success=success,
                local=self.is_local,
            )
        prompt, completion, _ = llm_usage.call_tokens(messages, call)
        llm_budget.settle(reservation, prompt + completion)

    def chat_completion_stream(
        self,
        messages: list[dict[str, str]],
        cancel_event: threading.Event | None = None,
        **kwargs: Any,
    ) -> Iterator[LLMStreamEvent]:
        """Stream a completion, checked against the token limits first.

        A refusal is yielded as an ``error`` event carrying the limit's ``code`` (the stream
        reports every failure in-band), and nothing is sent to the provider. The reservation
        is settled to the reported usage (or the chars / 4 estimate of what streamed) when
        the stream ends, including when the consumer stops early.
        """
        from app.services import llm_budget

        try:
            reservation = self._reserve_budget(messages, kwargs)
        except llm_budget.LLMBudgetExceededError as refused:
            yield LLMStreamEvent(type="error", message=str(refused), code=refused.code)
            return

        reported: int | None = None
        streamed_chars = 0
        try:
            for event in self._chat_completion_stream_untracked(messages, cancel_event, **kwargs):
                if event.type in ("delta", "reasoning"):
                    streamed_chars += len(event.text)
                elif event.type == "usage" and (
                    event.prompt_tokens is not None or event.completion_tokens is not None
                ):
                    reported = (event.prompt_tokens or 0) + (event.completion_tokens or 0)
                yield event
        finally:
            if reported is None:
                prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
                reported = (prompt_chars + streamed_chars) // 4
            llm_budget.settle(reservation, reported)
