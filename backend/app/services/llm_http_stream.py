"""The streamed chat completion for :class:`LLMService` (audit H1).

Asynchronous end to end for every HTTP provider: ``httpx.AsyncClient.stream`` with
``aiter_lines`` (httpx docs, "Async Support" and "Streaming Responses"), each line fed to
the provider's incremental parser (``llm_stream``). A reply waiting on the model holds no
thread. Only two steps leave the event loop, both on chat's own thread limiter: the
endpoint validation (one DNS lookup, ``_endpoint_session``) and Bedrock's boto3 stream,
which has no async API. Metering wraps this in ``LLMMeteringMixin.chat_completion_stream``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncGenerator
from collections.abc import AsyncIterator
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

if TYPE_CHECKING:  # pragma: no cover
    import requests

    from app.utils.url_validation import PinnedTarget

logger = logging.getLogger(__name__)

#: Connect within 10 s; a 120 s gap between chunks means the provider stopped producing.
_STREAM_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
#: httpx transport retries apply to failed connection attempts only.
_STREAM_CONNECT_RETRIES = 3


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
        Transport ``retries`` cover connection failures only; a capacity status (429/5xx)
        is reported in-band as ``transient`` for the chat layer to explain.
        """
        return httpx.AsyncClient(
            timeout=_STREAM_TIMEOUT,
            follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(retries=_STREAM_CONNECT_RETRIES),
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

        connected = False
        try:
            async with (
                self._stream_client() as client,
                client.stream(
                    "POST",
                    target.url,
                    json=payload,
                    headers={**headers, **target.headers},
                    extensions=target.httpx_extensions,
                ) as response,
            ):
                connected = True
                if response.status_code != 200:
                    detail = (await response.aread()).decode("utf-8", errors="replace")[:500]
                    logger.error(f"LLM stream error ({response.status_code}): {detail}")
                    yield LLMStreamEvent(
                        type="error",
                        message=f"LLM API error ({response.status_code}): {detail}",
                        transient=response.status_code in TRANSIENT_HTTP_STATUSES,
                    )
                    return

                async for event in parse_stream(parser, self._stream_lines(response, cancel_event)):
                    if event.type == "done" and cancel_event is not None and cancel_event.is_set():
                        yield LLMStreamEvent(type="done", finish_reason="cancelled")
                        return
                    yield event
                    if event.type in ("done", "error"):
                        return
        except httpx.HTTPError as e:
            if not connected:
                logger.error(f"LLM stream connection failed for {provider}: {type(e).__name__}")
                yield LLMStreamEvent(type="error", message=f"Connection error: {e}")
            else:
                logger.error(f"LLM stream interrupted for {provider}: {type(e).__name__}: {e}")
                yield LLMStreamEvent(type="error", message=f"Stream interrupted: {e}")
