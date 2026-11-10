"""The streamed chat completion for :class:`LLMService` (audit H1).

Asynchronous end to end for every HTTP provider: ``httpx.AsyncClient.stream`` with
``aiter_lines`` (httpx docs, "Async Support" and "Streaming Responses"), each line fed to
the provider's incremental parser (``llm_stream``). A reply waiting on the model holds no
thread. Only two steps leave the event loop, both on chat's own thread limiter: the
endpoint validation (one DNS lookup, ``_endpoint_session``) and Bedrock's boto3 stream,
which has no async API. Metering wraps this in ``LLMMeteringMixin.chat_completion_stream``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
from collections.abc import AsyncGenerator
from collections.abc import AsyncIterator
from datetime import UTC
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from typing import Any

import httpx

from app.core.threadpool import iterate_in_chat_thread
from app.core.threadpool import run_in_chat_thread
from app.services.llm_stream import TRANSIENT_HTTP_STATUSES
from app.services.llm_stream import LLMStreamEvent
from app.services.llm_stream import apply_stream_payload
from app.services.llm_stream import get_stream_parser
from app.services.llm_stream import parse_stream
from app.services.llm_types import LLMHost
from app.services.llm_types import LLMProvider

if TYPE_CHECKING:
    import requests

    from app.utils.url_validation import PinnedTarget

logger = logging.getLogger(__name__)

#: Connect within 10 s; a 120 s gap between chunks means the provider stopped producing.
_STREAM_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
#: Attempts, in all, before a streamed request gives up (urllib3's Retry(total=3) on the
#: non-streaming path). Only before the first byte; see ``_open_stream``.
STREAM_MAX_ATTEMPTS = 3
#: Statuses that mean "at capacity, try again" (urllib3's status_forcelist minus 500).
RETRYABLE_STREAM_STATUSES = frozenset({429, 502, 503, 504})
STREAM_BACKOFF_BASE_S = 0.5
STREAM_RETRY_AFTER_CAP_S = 10.0
#: Jitter is not security-sensitive; SystemRandom just avoids a shared seeded generator.
_jitter = random.SystemRandom()
#: Indirection so tests can record the waits instead of sleeping.
_sleep = asyncio.sleep


class LLMHttpStreamMixin(LLMHost):
    """Token-by-token completions over an async HTTP stream."""

    if TYPE_CHECKING:

        def _prepare_payload(self, messages: list[dict[str, str]], **kwargs: Any) -> dict: ...

        def _endpoint_session(self, url: str) -> tuple[requests.Session, PinnedTarget]: ...

    @staticmethod
    async def _stream_lines(
        response: httpx.Response, cancel_event: threading.Event | None
    ) -> AsyncIterator[str]:
        """Yield decoded response lines, stopping early when cancellation is requested."""
        async for line in response.aiter_lines():
            if cancel_event is not None and cancel_event.is_set():
                return
            yield line

    def _stream_client(self) -> httpx.AsyncClient:
        """The HTTP client one streamed completion is sent through.

        A fresh client per stream: an ``httpx.AsyncClient``'s pool belongs to the event
        loop it was first used on, and a service instance outlives no more than one turn.
        ``follow_redirects`` stays off for the reason given in :meth:`_send_llm_request`.
        Retries (connection failures and 429/502/503/504, before the first byte only)
        are ``_open_stream``'s; what is still failing after them is reported in-band.
        """
        return httpx.AsyncClient(
            timeout=_STREAM_TIMEOUT,
            follow_redirects=False,
        )

    async def _chat_completion_stream_untracked(
        self,
        messages: list[dict[str, str]],
        cancel_event: threading.Event | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[LLMStreamEvent, None]:
        """Stream a chat completion token-by-token from the configured provider.

        The interactive counterpart to :meth:`chat_completion`. Payload construction is
        shared with the non-streaming path (``_prepare_payload``), so model, temperature
        and token settings can never drift between the two.

        Asynchronous end to end for every HTTP provider (``httpx.AsyncClient.stream`` +
        ``aiter_lines``, per the httpx docs' "Streaming responses"), so a reply that is
        waiting on the model holds no thread (audit H1). Only the one-off endpoint
        validation (a DNS lookup) and Bedrock's boto3 stream, which has no async API,
        run in chat worker threads.

        Failures are yielded as a single ``error`` event rather than raised: a streaming
        HTTP response has already committed its status line by the time most provider
        problems surface, so the caller relays the error in-band as an SSE frame.

        Args:
            messages: Chat messages in OpenAI format.
            cancel_event: Set by the caller (client disconnect / stop button) to abort
                the stream; the generator closes the connection and reports
                ``finish_reason="cancelled"``.
            **kwargs: Overrides forwarded to the payload builder (max_tokens,
                temperature, ...).

        Yields:
            :class:`LLMStreamEvent` values, always terminated by exactly one ``done``
            or ``error`` event.
        """
        from app.services.llm_service import LLMEndpointBlockedError

        # Bedrock is an SDK call, not an HTTP endpoint we POST to, so it branches out
        # ahead of the URL lookup below (its entry in `endpoints` is None by design).
        if self.config.provider == LLMProvider.BEDROCK:
            from app.services.bedrock_settings import resolve_region
            from app.services.llm_bedrock import stream_converse

            bedrock_stream = stream_converse(
                model=self.config.model,
                region=resolve_region(self.config.bedrock),
                connection=self.config.bedrock,
                messages=messages,
                max_tokens=kwargs.get("max_tokens", self.config.response_tokens),
                temperature=kwargs.get("temperature"),
                top_p=kwargs.get("top_p"),
                cancel_event=cancel_event,
                attribution=kwargs.get("attribution"),
            )
            async for event in iterate_in_chat_thread(bedrock_stream):
                yield event
            return

        url = self.endpoints[self.config.provider]
        if url is None:
            yield LLMStreamEvent(
                type="error", message=f"No endpoint configured for provider {self.config.provider}"
            )
            return

        provider = self.config.provider.value
        headers = self._get_headers()
        payload = apply_stream_payload(self._prepare_payload(messages, **kwargs), provider)
        parser = get_stream_parser(provider)()

        logger.info(
            f"Starting LLM stream: {provider}/{self.config.model}, "
            f"{len(messages)} messages, "
            f"{sum(len(m.get('content', '')) for m in messages)} chars"
        )

        # Same guard, same construction point as the non-streaming path — see
        # `_endpoint_session`. Its DNS resolution blocks, so it runs in a chat thread; the
        # pinned target it returns is what the stream dials (address in the URL, the
        # original name as Host and as the TLS server name).
        try:
            _session, target = await run_in_chat_thread(self._endpoint_session, url)
        except LLMEndpointBlockedError as e:
            # In-band, like every other failure here: the SSE status line is already sent.
            yield LLMStreamEvent(type="error", message=str(e))
            return

        async with self._stream_client() as client:
            request = client.build_request(
                "POST",
                target.url,
                json=payload,
                headers={**headers, **target.headers},
                extensions=target.httpx_extensions,
            )
            opened = await _open_stream(client, request, cancel_event)
            if isinstance(opened, LLMStreamEvent):
                logger.error(f"LLM stream connection failed for {provider}: {opened.message}")
                yield opened
                return
            response = opened
            try:
                if response.status_code != 200:
                    detail = (await response.aread()).decode("utf-8", errors="replace")[:500]
                    logger.error(f"LLM stream error ({response.status_code}): {detail}")
                    yield LLMStreamEvent(
                        type="error",
                        message=f"LLM API error ({response.status_code}): {detail}",
                        transient=response.status_code in TRANSIENT_HTTP_STATUSES,
                    )
                    return

                # Streaming has started: from here a failure is reported, never retried,
                # since the tokens already sent cannot be taken back.
                async for event in parse_stream(parser, self._stream_lines(response, cancel_event)):
                    if event.type == "done" and cancel_event is not None and cancel_event.is_set():
                        yield LLMStreamEvent(type="done", finish_reason="cancelled")
                        return
                    yield event
                    if event.type in ("done", "error"):
                        return
            except httpx.HTTPError as e:
                logger.error(f"LLM stream interrupted for {provider}: {type(e).__name__}: {e}")
                yield LLMStreamEvent(
                    type="error", message=f"Stream interrupted: {type(e).__name__}"
                )
            finally:
                await response.aclose()


async def _open_stream(
    client: httpx.AsyncClient, request: httpx.Request, cancel_event: threading.Event | None
) -> httpx.Response | LLMStreamEvent:
    """Send ``request`` and return the response once its headers arrive, retrying first.

    Before the first byte of the body nothing has reached the user, so a refused
    connection or a capacity status (429/502/503/504) is retried: up to
    ``STREAM_MAX_ATTEMPTS`` attempts with exponential backoff and full jitter, or the
    provider's ``Retry-After`` capped at ``STREAM_RETRY_AFTER_CAP_S`` (the same policy the
    non-streaming path gets from urllib3's ``Retry``). No retry library is a dependency
    here, which is why this is a helper rather than ``tenacity``.

    Returns:
        The open response (any status; the caller reports a non-200), or an ``error``
        event when every attempt failed to connect.
    """
    for attempt in range(1, STREAM_MAX_ATTEMPTS + 1):
        last = attempt == STREAM_MAX_ATTEMPTS
        try:
            response = await client.send(request, stream=True)
        except httpx.TransportError as e:
            if last or _cancelled(cancel_event):
                logger.error(f"LLM stream connection failed: {type(e).__name__}: {e}")
                return LLMStreamEvent(type="error", message=f"Connection error: {type(e).__name__}")
            await _sleep(retry_delay(attempt, None))
            continue
        if (
            response.status_code not in RETRYABLE_STREAM_STATUSES
            or last
            or _cancelled(cancel_event)
        ):
            return response
        retry_after = response.headers.get("Retry-After")
        await response.aclose()
        logger.warning(
            "LLM stream got %s, retrying (attempt %d of %d)",
            response.status_code,
            attempt + 1,
            STREAM_MAX_ATTEMPTS,
        )
        await _sleep(retry_delay(attempt, retry_after))
    raise AssertionError("unreachable: the last attempt always returns")


def _cancelled(cancel_event: threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def retry_delay(attempt: int, retry_after: str | None) -> float:
    """Seconds to wait before attempt ``attempt + 1``.

    ``Retry-After`` (delta-seconds or an HTTP-date, RFC 9110 section 10.2.3) wins when
    the provider sent one, capped at ``STREAM_RETRY_AFTER_CAP_S`` so a long value cannot
    hold the reply open. Otherwise exponential backoff with full jitter (a uniform draw
    up to ``STREAM_BACKOFF_BASE_S * 2 ** (attempt - 1)``), so many replies refused at the
    same moment do not all retry at the same moment.
    """
    if retry_after:
        seconds = _parse_retry_after(retry_after)
        if seconds is not None:
            return min(max(seconds, 0.0), STREAM_RETRY_AFTER_CAP_S)
    ceiling = min(STREAM_BACKOFF_BASE_S * 2 ** (attempt - 1), STREAM_RETRY_AFTER_CAP_S)
    return _jitter.uniform(0.0, ceiling)


def _parse_retry_after(value: str) -> float | None:
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (when - datetime.now(UTC)).total_seconds()
