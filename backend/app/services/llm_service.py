"""
LLM Service for OpenTranscribe

Provides unified interface for multiple LLM providers using synchronous HTTP requests.
Designed specifically for Celery tasks - no asyncio conflicts.
"""

import contextlib
import json
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import settings
from app.services.bedrock_settings import BedrockConnection
from app.services.bedrock_settings import connection_from_encrypted
from app.services.bedrock_settings import resolve_region
from app.services.llm_chunking import LLMChunkingMixin
from app.services.llm_health import LLMHealthMixin
from app.services.llm_json_extract import LLMJsonExtractMixin
from app.services.llm_payloads import LLMPayloadMixin
from app.services.llm_speaker_id import LLMSpeakerIdMixin
from app.services.llm_stream import TRANSIENT_HTTP_STATUSES
from app.services.llm_stream import LLMStreamEvent
from app.services.llm_stream import apply_stream_payload
from app.services.llm_stream import get_stream_parser
from app.services.llm_summarization import LLMSummarizationMixin
from app.services.llm_types import SDK_PROVIDERS
from app.services.llm_types import LLMConfig
from app.services.llm_types import LLMProvider
from app.services.llm_types import LLMResponse
from app.utils.llm_log_safety import describe_llm_text
from app.utils.llm_log_safety import log_llm_text_excerpt

if TYPE_CHECKING:  # pragma: no cover - import cost is paid only by type checkers
    from sqlalchemy.orm import Session

    from app.models.user_llm_settings import UserLLMSettings
    from app.utils.url_validation import PinnedTarget

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResolvedUserLLM:
    """A user's active LLM configuration row, validated but not yet built into a client."""

    settings: "UserLLMSettings"
    provider: LLMProvider
    api_key: str | None
    base_url: str | None
    bedrock: BedrockConnection | None = None


class LLMEndpointBlockedError(Exception):
    """The configured LLM endpoint is not a permitted outbound target.

    Raised *before* any connection is attempted, by the SSRF guard on the shared
    request path. Distinct from a network error so callers can tell "we refused to
    dial this" from "we dialled it and it failed".
    """


class LLMService(
    LLMPayloadMixin,
    LLMSummarizationMixin,
    LLMChunkingMixin,
    LLMSpeakerIdMixin,
    LLMJsonExtractMixin,
    LLMHealthMixin,
):
    """
    Synchronous LLM service for Celery tasks - no asyncio conflicts
    """

    #: Returned to the operator when the endpoint is refused. Deliberately does NOT say
    #: *why* — the reason distinguishes "private address" from "cannot resolve", which
    #: turns an operator-visible error into a network scanner. The reason is logged.
    BLOCKED_ENDPOINT_MESSAGE = (
        "The configured LLM endpoint is not a permitted outbound target. It must be a "
        "publicly reachable http(s) address. Set LLM_ALLOW_PRIVATE_ENDPOINTS=true to "
        "allow a self-hosted endpoint on a private network."
    )

    def __init__(self, config: LLMConfig):
        self.config = config
        self.user_context_window = config.max_tokens  # Store user's context window setting

        # Derive response token budget from user's context window
        # For 121K context → 16384, for 8K → 4000 (floor)
        self.response_tokens = max(4000, min(16384, self.user_context_window // 4))
        # Keep the config in sync. `_prepare_payload` sends `config.response_tokens` as
        # max_tokens while callers (chat's prompt budget, summarization) reserve the
        # DERIVED value above — before this assignment the dataclass default of 4000 was
        # never overwritten, so on a large-context model the prompt reserved up to 16384
        # tokens for an answer that was hard-capped at 4000. The two must agree.
        self.config.response_tokens = self.response_tokens

        # Retry policy for the outbound session. The session itself is built lazily by
        # `_endpoint_session`, because it cannot exist until the endpoint has been
        # validated and pinned — see that method.
        self._retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST"],
        )
        self._session_lock = threading.Lock()
        self._pinned_stack: ExitStack | None = None
        self._pinned: tuple[str, requests.Session, PinnedTarget] | None = None

        self.endpoints = self._build_endpoints(config.base_url)

        if not self._endpoint_resolves(config.provider, config.base_url):
            raise ValueError(f"Invalid provider configuration for {config.provider}")

        # Log the resolved endpoint for debugging (helps diagnose connection issues like Issue #100)
        resolved_endpoint = self.endpoints.get(config.provider)
        logger.info(
            f"Initialized LLMService: {config.provider}/{config.model}, "
            f"endpoint={resolved_endpoint}, "
            f"base_url={config.base_url or 'default'}, "
            f"context_window={self.user_context_window}, "
            f"response_tokens={self.response_tokens}"
        )

    @staticmethod
    def _build_endpoints(base_url: str | None) -> dict[LLMProvider, str | None]:
        """Provider -> chat endpoint for ``base_url``; ``None`` where one cannot be derived."""

        def build_endpoint(url: str) -> str:
            """Build chat completions endpoint"""
            clean_url = url.strip().rstrip("/")
            if clean_url.endswith("/v1"):
                return f"{clean_url}/chat/completions"
            else:
                return f"{clean_url}/v1/chat/completions"

        def build_ollama_endpoint(url: str) -> str:
            """Build Ollama chat endpoint using native API"""
            clean_url = url.strip().rstrip("/")
            # Remove /v1 suffix if present since we're using native API
            if clean_url.endswith("/v1"):
                clean_url = clean_url[:-3]
            return f"{clean_url}/api/chat"

        return {
            # Dynamic endpoints - respect custom base_url for OpenAI-compatible servers (vLLM, etc.)
            LLMProvider.OPENAI: build_endpoint(base_url)
            if base_url
            else "https://api.openai.com/v1/chat/completions",
            LLMProvider.VLLM: build_endpoint(base_url) if base_url else None,
            LLMProvider.OLLAMA: build_ollama_endpoint(base_url)
            if base_url
            else "http://localhost:11434/api/chat",
            LLMProvider.CUSTOM: build_endpoint(base_url) if base_url else None,
            LLMProvider.OPENROUTER: build_endpoint(base_url)
            if base_url
            else "https://openrouter.ai/api/v1/chat/completions",
            # Fixed endpoints - these providers don't support custom base URLs
            LLMProvider.CLAUDE: "https://api.anthropic.com/v1/messages",
            LLMProvider.ANTHROPIC: "https://api.anthropic.com/v1/messages",
            # Not a real HTTP endpoint — boto3 resolves it from the region. This entry
            # exists only so `self.endpoints[...]` (validate_connection, the
            # /llm-settings/test endpoint) has something descriptive to read instead of
            # raising KeyError or logging a bare "None".
            LLMProvider.BEDROCK: "boto3 bedrock-runtime",
        }

    @staticmethod
    def _endpoint_resolves(provider: LLMProvider, base_url: str | None) -> bool:
        """Would ``__init__`` accept this provider/base_url pair?

        SDK-based providers have no HTTP endpoint to validate — Bedrock is reached
        through boto3, which resolves the endpoint from the region itself.
        """
        if provider in SDK_PROVIDERS:
            return True
        return bool(LLMService._build_endpoints(base_url).get(provider))

    def _get_headers(self) -> dict[str, str]:
        """
        Get headers for API request based on provider.

        Constructs the appropriate HTTP headers for API requests, including
        authentication headers specific to each LLM provider.

        Returns:
            Dictionary containing HTTP headers for the request

        Note:
            - OpenAI: Uses Bearer token authorization
            - Claude/Anthropic: Uses x-api-key header with anthropic-version
            - OpenRouter: Uses Bearer token with referrer headers
            - vLLM/Ollama: May or may not require authentication
        """
        headers = {"Content-Type": "application/json"}

        if self.config.provider == LLMProvider.OPENAI and self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        elif self.config.provider == LLMProvider.VLLM:
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
        elif self.config.provider == LLMProvider.OLLAMA:
            pass  # Ollama typically doesn't require auth
        elif self.config.provider in [LLMProvider.CLAUDE, LLMProvider.ANTHROPIC]:
            if self.config.api_key:
                headers["x-api-key"] = self.config.api_key
                headers["anthropic-version"] = "2023-06-01"
        elif self.config.provider == LLMProvider.OPENROUTER and self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
            if settings.OPENROUTER_HTTP_REFERER:
                headers["HTTP-Referer"] = settings.OPENROUTER_HTTP_REFERER
            headers["X-Title"] = "OpenTranscribe"
        elif self.config.provider == LLMProvider.CUSTOM and self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        return headers

    def _endpoint_session(self, url: str) -> tuple[requests.Session, "PinnedTarget"]:
        """Validate *url*, pin it, and return the session to send it with.

        **This is the single construction point for every outbound LLM request.**
        ``chat_completion`` (via :meth:`_send_llm_request`) and
        ``chat_completion_stream`` both go through it, so the guard cannot be true of
        one and false of the other — which is exactly the defect it closes (issue #444).
        ``validate_connection``/``health_check`` were guarded; ``chat_completion``, the
        path that runs on **every real request**, was not, so an operator-configured
        ``base_url`` was checked when someone pressed "Test connection" and unchecked
        forever after.

        The endpoint is operator-supplied configuration, so:

        * the address is **pinned** — ``resolve_pinned_target`` resolves once and returns
          the IP to dial plus the hostname to verify TLS against. Validate-then-let-the-
          client-re-resolve loses to a hostname that alternates between a public address
          and ``127.0.0.1``; there is exactly one resolution here and the address that
          was judged is the address that is dialled.
        * ``LLM_ALLOW_PRIVATE_ENDPOINTS`` is honoured exactly as on the other call sites —
          a self-hosted Ollama/vLLM on the LAN is a legitimate configuration. The flag
          loosens the address range only; instance metadata stays refused.

        The session is built **once per service instance** and reused. That keeps the
        connection pooling and retry policy this service has always had — the LLM
        redaction detector issues one ``chat_completion`` per transcript segment on a
        single instance, so a per-request session would mean a TLS handshake per segment
        — and it means one DNS resolution per instance rather than one per call.

        Args:
            url: The endpoint about to be POSTed to (``self.endpoints[provider]``).

        Returns:
            ``(session, target)``. Send ``target.url`` with ``target.headers`` merged
            into your own, and ``allow_redirects=False``.

        Raises:
            LLMEndpointBlockedError: The endpoint must not be fetched.
        """
        with self._session_lock:
            cached = self._pinned
            if cached is not None and cached[0] == url:
                return cached[1], cached[2]

            from app.core.config import settings as _settings
            from app.utils.url_validation import pinned_requests_session
            from app.utils.url_validation import resolve_pinned_target

            target, reason = resolve_pinned_target(
                url, allow_private=_settings.LLM_ALLOW_PRIVATE_ENDPOINTS
            )
            if target is None:
                logger.warning("Blocked LLM request to %r: %s", url, reason)
                raise LLMEndpointBlockedError(self.BLOCKED_ENDPOINT_MESSAGE)

            self._discard_pinned_session()
            stack = ExitStack()
            session = stack.enter_context(pinned_requests_session(target))
            # `pinned_requests_session` yields a bare Session. Restore the retry policy
            # in place: mounting a fresh HTTPAdapter would REPLACE the pinned adapter and
            # silently undo the SNI binding, leaving TLS verified against the IP.
            # `max_retries` is on HTTPAdapter, not the abstract BaseAdapter `adapters`
            # is typed with; every adapter mounted here is an HTTPAdapter.
            for adapter in session.adapters.values():
                if isinstance(adapter, HTTPAdapter):
                    adapter.max_retries = self._retry_strategy
            self._pinned_stack = stack
            self._pinned = (url, session, target)
            return session, target

    def _discard_pinned_session(self) -> None:
        """Close and forget the pinned session, if one was built."""
        stack, self._pinned_stack = self._pinned_stack, None
        self._pinned = None
        if stack is not None:
            with contextlib.suppress(Exception):
                stack.close()

    def _send_llm_request(
        self, url: str, payload: dict, headers: dict, timeout: int
    ) -> dict[str, Any]:
        """Send HTTP request to LLM provider and return parsed JSON response."""
        session, target = self._endpoint_session(url)
        start_time = time.time()
        # `allow_redirects=False`: the pin covers ONE hop. A public endpoint that passes
        # validation and answers `302 Location: http://169.254.169.254/` reaches cloud
        # instance metadata with no DNS control at all, and would additionally reuse this
        # session's SNI binding for a different host.
        response = session.post(
            target.url,
            json=payload,
            headers={**headers, **target.headers},
            timeout=timeout,
            allow_redirects=False,
        )
        request_time = time.time() - start_time

        logger.info(
            f"LLM request completed in {request_time:.2f}s with status {response.status_code}"
        )

        if response.status_code != 200:
            error_detail = f"LLM API error ({response.status_code}): {response.text[:500]}{'...' if len(response.text) > 500 else ''}"
            logger.error(error_detail)
            raise Exception(f"LLM API error: {response.status_code} - {response.text}")

        try:
            result: dict[str, Any] = response.json()
            return result
        except json.JSONDecodeError as e:
            logger.error(
                "Failed to parse LLM response body as JSON (%s)", describe_llm_text(response.text)
            )
            log_llm_text_excerpt(logger, "Unparseable LLM response body", response.text)
            raise Exception(f"Invalid JSON response: {e}") from e

    def _resolve_endpoint(self) -> str:
        """Return the HTTP endpoint for the configured provider, or raise.

        A plain subscript (``self.endpoints[provider]``) raises ``KeyError`` for any
        provider absent from the dict entirely — which is exactly what happened for
        Bedrock (not a key in ``self.endpoints` at all, since it is reached through
        boto3, not HTTP) before the SDK branch below existed to intercept it. This
        converts that into the same ``ValueError`` every other missing-endpoint case
        (``vllm``/``custom``/``openrouter`` with no ``base_url``, which ARE keys but
        map to ``None``) already raised.

        Raises:
            ValueError: No endpoint is configured for ``self.config.provider``.
        """
        url = self.endpoints.get(self.config.provider)
        if not url:
            raise ValueError(f"No endpoint configured for provider {self.config.provider}")
        return url

    def chat_completion_raw(
        self, messages: list[dict[str, str]], *, timeout: int = 300, **kwargs
    ) -> dict[str, Any]:
        """Send one chat completion and return the provider's parsed JSON, unshaped.

        :meth:`chat_completion` normalizes into :class:`LLMResponse` and raises on
        empty content, which is right for a summary and wrong for the reasoning
        capability probe (``services/llm_reasoning.py``): its activated arm can
        legitimately spend the whole budget in the thought channel and come back
        with ``content: null`` and 1,656 characters of reasoning. That is a
        successful measurement, not an error — and the fields the probe reads
        (``reasoning`` / ``reasoning_content``) are dropped by the normalization
        entirely.

        Args:
            messages: Messages in OpenAI format.
            timeout: Per-request timeout in seconds.
            **kwargs: Passed to :meth:`_prepare_payload`.

        Returns:
            The provider's decoded JSON response.

        Raises:
            LLMEndpointBlockedError: The endpoint is not a permitted target.
            ValueError: Bedrock has no "provider's parsed JSON" — it is an SDK call,
                not an HTTP endpoint, so there is nothing unshaped to return. This
                is a guard, not a regression: `llm_reasoning.PROBEABLE_PROVIDERS`
                deliberately excludes Bedrock already.
            Exception: Any non-200 response or unparseable body.
        """
        if self.config.provider == LLMProvider.BEDROCK:
            raise ValueError(
                "chat_completion_raw is not supported for the Bedrock provider: "
                "there is no provider JSON body to return unshaped for an SDK call."
            )
        url = self._resolve_endpoint()
        payload = self._prepare_payload(messages, **kwargs)
        return self._send_llm_request(url, payload, self._get_headers(), timeout)

    def chat_completion(self, messages: list[dict[str, str]], **kwargs) -> LLMResponse:
        """
        Send chat completion request to LLM provider
        """
        if self.config.provider == LLMProvider.BEDROCK:
            return self._bedrock_chat_completion(messages, **kwargs)

        url = self._resolve_endpoint()
        headers = self._get_headers()
        payload = self._prepare_payload(messages, **kwargs)

        total_content_length = sum(len(msg.get("content", "")) for msg in messages)
        logger.info(f"Sending request to {self.config.provider} ({url})")
        logger.info(f"Total request content length: {total_content_length} characters")
        logger.debug(f"Request payload keys: {list(payload.keys())}")

        timeout = min(1200, max(300, total_content_length // 1000))
        logger.info(f"Using timeout: {timeout} seconds for content length: {total_content_length}")

        try:
            data = self._send_llm_request(url, payload, headers, timeout)
            content, usage_tokens, finish_reason = self._extract_response_content(data)

            if not content:
                raise Exception("Empty content in LLM response")

            logger.info(f"LLM request successful, tokens: {usage_tokens}")

            return LLMResponse(
                content=content,
                usage_tokens=usage_tokens,
                finish_reason=finish_reason,
                model=self.config.model,
                provider=self.config.provider.value,
            )

        except requests.exceptions.Timeout as e:
            logger.error(f"Request timed out after {timeout}s for {self.config.provider}: {e}")
            raise Exception(
                f"Request timed out after {timeout} seconds. Content may be too long for processing."
            ) from e
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error to {self.config.provider}: {e}")
            raise Exception(f"Connection error: {e}") from e
        except requests.exceptions.RequestException as e:
            logger.error(f"HTTP error in LLM request: {type(e).__name__}: {e}")
            raise Exception(f"Network error: {e}") from e
        except Exception as e:
            logger.error(
                f"Unexpected error in LLM request to {self.config.provider}: {type(e).__name__}: {e}"
            )
            raise

    def _bedrock_chat_completion(self, messages: list[dict[str, str]], **kwargs) -> LLMResponse:
        """Non-streaming completion via the Bedrock Converse SDK path.

        Bedrock is an SDK call, not an HTTP endpoint (it has no entry in
        ``self.endpoints`` at all — see ``SDK_PROVIDERS``), so it cannot go through
        ``_send_llm_request``/``_extract_response_content`` like every other provider.
        ``llm_bedrock.converse`` drains the existing ``stream_converse`` generator and
        folds it into the same ``(content, usage_tokens, finish_reason)`` shape the
        HTTP extractors return, so this method only has to build the ``LLMResponse``.

        Raises:
            Exception: The stream reported an error (``llm_bedrock.BedrockStreamError``)
                or the response was empty, mirroring the HTTP path's behaviour.
        """
        from app.services.bedrock_settings import resolve_region
        from app.services.llm_bedrock import BedrockStreamError
        from app.services.llm_bedrock import converse

        try:
            content, usage_tokens, finish_reason = converse(
                model=self.config.model,
                region=resolve_region(self.config.bedrock),
                connection=self.config.bedrock,
                messages=messages,
                max_tokens=kwargs.get("max_tokens", self.config.response_tokens),
                temperature=kwargs.get("temperature"),
                top_p=kwargs.get("top_p"),
                attribution=kwargs.get("attribution"),
            )
        except BedrockStreamError as e:
            logger.error(f"Bedrock chat completion failed: {e}")
            raise Exception(f"Bedrock error: {e}") from e

        if not content:
            raise Exception("Empty content in LLM response")

        logger.info(f"LLM request successful, tokens: {usage_tokens}")

        return LLMResponse(
            content=content,
            usage_tokens=usage_tokens,
            finish_reason=finish_reason,
            model=self.config.model,
            provider=self.config.provider.value,
        )

    def _iter_stream_lines(
        self, response: requests.Response, cancel_event: threading.Event | None
    ) -> Iterator[str]:
        """Yield decoded response lines, stopping early when cancellation is requested."""
        for line in response.iter_lines(decode_unicode=True):
            if cancel_event is not None and cancel_event.is_set():
                return
            if line is None:
                continue
            yield str(line)

    def chat_completion_stream(
        self,
        messages: list[dict[str, str]],
        cancel_event: threading.Event | None = None,
        **kwargs,
    ) -> Iterator[LLMStreamEvent]:
        """Stream a chat completion token-by-token from the configured provider.

        The interactive counterpart to :meth:`chat_completion`. Payload construction is
        shared with the non-streaming path (``_prepare_payload``), so model, temperature
        and token settings can never drift between the two.

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
        # Bedrock is an SDK call, not an HTTP endpoint we POST to, so it branches out
        # ahead of the URL lookup below (its entry in `endpoints` is None by design).
        # The generator it returns is synchronous and honours the same cancel_event,
        # so the caller's iterate_in_threadpool driver is unchanged.
        if self.config.provider == LLMProvider.BEDROCK:
            from app.services.bedrock_settings import resolve_region
            from app.services.llm_bedrock import stream_converse

            yield from stream_converse(
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
        parser = get_stream_parser(provider)

        logger.info(
            f"Starting LLM stream: {provider}/{self.config.model}, "
            f"{len(messages)} messages, "
            f"{sum(len(m.get('content', '')) for m in messages)} chars"
        )

        # Same guard, same construction point as the non-streaming path — see
        # `_endpoint_session`. Pinning is transparent to streaming: it changes which
        # address the socket is opened to and which name TLS is verified against, and
        # nothing about the response body, so chunked transfer and `iter_lines` are
        # untouched. The only added latency is one DNS resolution on the FIRST call per
        # service instance, which the caller's first-token watchdog does not notice.
        try:
            session, target = self._endpoint_session(url)
        except LLMEndpointBlockedError as e:
            # In-band, like every other failure here: the SSE status line is already sent.
            yield LLMStreamEvent(type="error", message=str(e))
            return

        try:
            # (connect, read) — a 120s idle gap means the provider stopped producing.
            # `allow_redirects=False` for the reason given in `_send_llm_request`.
            response = session.post(
                target.url,
                json=payload,
                headers={**headers, **target.headers},
                timeout=(10, 120),
                stream=True,
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"LLM stream connection failed for {provider}: {type(e).__name__}")
            yield LLMStreamEvent(type="error", message=f"Connection error: {e}")
            return

        try:
            if response.status_code != 200:
                detail = response.text[:500]
                logger.error(f"LLM stream error ({response.status_code}): {detail}")
                yield LLMStreamEvent(
                    type="error",
                    message=f"LLM API error ({response.status_code}): {detail}",
                    transient=response.status_code in TRANSIENT_HTTP_STATUSES,
                )
                return

            for event in parser(self._iter_stream_lines(response, cancel_event)):
                if event.type == "done" and cancel_event is not None and cancel_event.is_set():
                    yield LLMStreamEvent(type="done", finish_reason="cancelled")
                    return
                yield event
                if event.type in ("done", "error"):
                    return
        except requests.exceptions.RequestException as e:
            logger.error(f"LLM stream interrupted for {provider}: {type(e).__name__}: {e}")
            yield LLMStreamEvent(type="error", message=f"Stream interrupted: {e}")
        finally:
            response.close()

    def close(self):
        """
        Close the session and clean up resources.

        Properly closes the HTTP session and releases any held connections.
        Should be called when the LLMService instance is no longer needed.
        """
        with self._session_lock:
            self._discard_pinned_session()
        logger.debug(f"Closed session for {self.config.provider}")

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with automatic cleanup."""
        self.close()

    @staticmethod
    def create_from_settings(user_id: int | None = None) -> "LLMService | None":
        """
        Create LLMService from user-specific settings only

        Args:
            user_id: If provided, attempts to load user-specific settings

        Returns:
            LLMService configured with user settings, or None if no user LLM is configured
        """
        # Try to load user-specific settings
        if user_id:
            try:
                user_service = LLMService.create_from_user_settings(user_id)
                if user_service:
                    return user_service
            except Exception as e:
                logger.warning(f"Failed to load user LLM settings for user {user_id}: {e}")

        # No fallback - users must explicitly configure LLM settings
        logger.info(f"No active LLM configuration found for user {user_id}")
        return None

    @staticmethod
    def create_from_config_id(user_id: int, config_id: int) -> "LLMService | None":
        """Create LLMService from ONE specific LLM configuration the user may use.

        Used by RAG chat, where a conversation can pin a model different from the
        user's active default. Shares the ownership check and key decryption with
        :meth:`create_from_user_settings` so a per-conversation override can never
        reach a configuration the caller isn't entitled to.

        Args:
            user_id: The caller (owner, or a user of a shared configuration).
            config_id: ``user_llm_settings.id`` to load.

        Returns:
            A configured service, or None if the config is missing or not theirs.
        """
        from app.db.base import SessionLocal
        from app.models.user_llm_settings import UserLLMSettings
        from app.utils.tenant_sharing import shared_usable_by

        db = SessionLocal()
        try:
            user_settings = (
                db.query(UserLLMSettings)
                .filter(
                    UserLLMSettings.id == config_id,
                    shared_usable_by(UserLLMSettings.user_id, UserLLMSettings.is_shared, user_id),
                )
                .first()
            )
            if not user_settings:
                logger.warning(f"LLM config {config_id} not available to user {user_id}")
                return None

            try:
                api_key, bedrock = LLMService.credentials_from_row(user_settings)
            except ValueError:
                logger.error(f"Failed to decrypt stored credentials for LLM config {config_id}")
                return None

            from app.services import llm_context_window

            declared_window = int(user_settings.max_tokens)
            config = LLMConfig(
                provider=LLMProvider(user_settings.provider),
                model=str(user_settings.model_name),
                api_key=api_key,
                base_url=str(user_settings.base_url) if user_settings.base_url else None,
                max_tokens=llm_context_window.effective_window(
                    db,
                    provider=str(user_settings.provider),
                    base_url=str(user_settings.base_url) if user_settings.base_url else None,
                    model=str(user_settings.model_name),
                    declared=declared_window,
                ),
                temperature=float(user_settings.temperature),
                bedrock=bedrock,
            )
            return LLMService(config)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Could not build LLMService from config {config_id}: {e}")
            return None
        finally:
            db.close()

    @staticmethod
    def _resolve_user_llm_settings(db: "Session", user_id: int) -> _ResolvedUserLLM | None:
        """The user's active LLM configuration, or ``None`` to fall back to system settings.

        DB-only (no network). Shared by :meth:`create_from_user_settings` and
        :meth:`is_configured_for_user` so the two cannot disagree about which row
        counts: a dangling or foreign ``active_llm_config_id``, an undecryptable key
        or an unknown provider string all resolve to ``None`` here in both.
        """
        from app import models
        from app.models.user_llm_settings import UserLLMSettings
        from app.utils.tenant_sharing import shared_usable_by

        active_config_setting = (
            db.query(models.UserSetting)
            .filter(
                models.UserSetting.user_id == user_id,
                models.UserSetting.setting_key == "active_llm_config_id",
            )
            .first()
        )
        if not active_config_setting or not active_config_setting.setting_value:
            logger.info(f"No active LLM configuration for user {user_id}, checking system settings")
            return None

        try:
            active_config_id = int(active_config_setting.setting_value)
        except (TypeError, ValueError):
            logger.error(
                f"Configuration error for user {user_id}: active_llm_config_id "
                f"{active_config_setting.setting_value!r} is not an integer"
            )
            return None

        # The active configuration may be the user's own or one shared within a tenant
        # both belong to; the stored pointer is re-validated because it outlives
        # membership changes.
        user_settings = (
            db.query(UserLLMSettings)
            .filter(
                UserLLMSettings.id == active_config_id,
                shared_usable_by(UserLLMSettings.user_id, UserLLMSettings.is_shared, user_id),
            )
            .first()
        )
        if not user_settings:
            logger.warning(
                f"Active LLM config {active_config_id} not found for user {user_id}, "
                "checking system settings"
            )
            return None

        try:
            provider = LLMProvider(user_settings.provider)
        except ValueError as e:
            logger.error(f"Configuration error for user {user_id}: {e}")
            return None

        try:
            api_key, bedrock = LLMService.credentials_from_row(user_settings)
        except ValueError:
            logger.error(f"Failed to decrypt stored credentials for user {user_id}")
            return None

        if provider == LLMProvider.BEDROCK and not resolve_region(bedrock):
            logger.warning(
                f"Bedrock configuration for user {user_id} has no region and the server has "
                "none either, checking system settings"
            )
            return None

        base_url = str(user_settings.base_url) if user_settings.base_url else None
        return _ResolvedUserLLM(user_settings, provider, api_key, base_url, bedrock)

    @staticmethod
    def credentials_from_row(
        user_settings: "UserLLMSettings",
    ) -> tuple[str | None, BedrockConnection | None]:
        """``(api_key, bedrock_connection)`` for a stored configuration row.

        The single decryption point for a row's ``api_key`` column. For ``bedrock`` the column
        holds an encrypted JSON blob of region and AWS credentials, so it is unpacked into a
        :class:`BedrockConnection` and ``api_key`` is ``None``; for every other provider it is
        the key itself. A blob or key that will not decrypt raises ``ValueError``.
        """
        stored = user_settings.api_key
        if str(user_settings.provider) == LLMProvider.BEDROCK.value:
            connection = connection_from_encrypted(str(stored)) if stored else BedrockConnection()
            if stored and not (connection.region or connection.has_credentials):
                raise ValueError("stored Bedrock credentials could not be decrypted")
            return None, connection
        if not stored:
            return None, None
        from app.utils.encryption import decrypt_api_key

        api_key = decrypt_api_key(str(stored))
        if not api_key:
            raise ValueError("stored API key could not be decrypted")
        return api_key, None

    @staticmethod
    def is_configured_for_user(db: "Session", user_id: int) -> bool:
        """Would :meth:`create_from_settings` build a service for this user?

        The cheap, DB-only form of that question — it reads the same settings and
        applies the same checks (provider set, credentials or endpoint present) but
        opens no connection, builds no client and measures no context window. For
        callers that decide whether to dispatch LLM work at all, such as the
        post-transcription recovery sweep: a looser check there dispatches tasks
        that can only skip.
        """
        return LLMService.describe_for_user(db, user_id) is not None

    @staticmethod
    def describe_for_user(db: "Session", user_id: int) -> tuple[str, str, str | None] | None:
        """``(provider, model, base_url)`` of the LLM this user's features would use.

        ``None`` exactly when :meth:`is_configured_for_user` is False; the same DB-only
        resolution (user config, else system settings), no network.
        """
        resolved = LLMService._resolve_user_llm_settings(db, user_id)
        if resolved is not None and LLMService._endpoint_resolves(
            resolved.provider, resolved.base_url
        ):
            return resolved.provider.value, str(resolved.settings.model_name), resolved.base_url
        system = LLMService._resolve_system_provider()
        if system is None:
            return None
        provider, model, _api_key, base_url = system
        return provider.value, model, base_url

    @staticmethod
    def create_from_user_settings(user_id: int) -> "LLMService | None":
        """Create LLMService from user-specific database settings"""
        from app.db.base import SessionLocal

        db = SessionLocal()
        try:
            resolved = LLMService._resolve_user_llm_settings(db, user_id)
            if resolved is None:
                return LLMService.create_from_system_settings()
            user_settings = resolved.settings

            # Create config from user settings - USER'S DECLARED CONTEXT WINDOW, narrowed
            # only by a MEASURED ceiling read off the live server (#833) -- never inferred.
            from app.services import llm_context_window

            temperature_float = float(user_settings.temperature)
            declared_window = int(user_settings.max_tokens)

            config = LLMConfig(
                provider=resolved.provider,
                model=str(user_settings.model_name),
                api_key=resolved.api_key,
                base_url=resolved.base_url,
                bedrock=resolved.bedrock,
                max_tokens=llm_context_window.effective_window(
                    db,
                    provider=str(user_settings.provider),
                    base_url=resolved.base_url,
                    model=str(user_settings.model_name),
                    declared=declared_window,
                ),
                temperature=temperature_float,
            )

            logger.info(
                f"Created LLMService for user {user_id}: {resolved.provider}/{user_settings.model_name}, user_context_window={user_settings.max_tokens}"
            )
            return LLMService(config)

        except (ValueError, KeyError) as e:
            logger.error(f"Configuration error for user {user_id}: {e}")
            return LLMService.create_from_system_settings()
        except Exception as e:
            logger.error(
                f"Unexpected error creating LLMService from user settings for user {user_id}: {e}"
            )
            return LLMService.create_from_system_settings()
        finally:
            db.close()

    @staticmethod
    def _get_provider_config(
        provider: LLMProvider,
    ) -> tuple[str, str | None, str | None] | None:
        """
        Get provider-specific configuration (model, api_key, base_url) with validation.

        Returns:
            Tuple of (model, api_key, base_url) if valid, None if validation fails.
        """
        provider_settings: dict[LLMProvider, dict[str, Any]] = {
            LLMProvider.VLLM: {
                "model": settings.VLLM_MODEL_NAME,
                "api_key": settings.VLLM_API_KEY,
                "base_url": settings.VLLM_BASE_URL,
                "requires_api_key": False,
                "invalid_model_defaults": ["gpt-oss"],
                "invalid_url_defaults": ["http://localhost:8012/v1"],
            },
            LLMProvider.OPENAI: {
                "model": settings.OPENAI_MODEL_NAME,
                "api_key": settings.OPENAI_API_KEY,
                "base_url": settings.OPENAI_BASE_URL,
                "requires_api_key": True,
            },
            LLMProvider.OLLAMA: {
                "model": settings.OLLAMA_MODEL_NAME,
                "api_key": None,
                "base_url": settings.OLLAMA_BASE_URL,
                "requires_api_key": False,
            },
            LLMProvider.CLAUDE: {
                "model": settings.ANTHROPIC_MODEL_NAME,
                "api_key": settings.ANTHROPIC_API_KEY,
                "base_url": settings.ANTHROPIC_BASE_URL,
                "requires_api_key": True,
            },
            LLMProvider.ANTHROPIC: {
                "model": settings.ANTHROPIC_MODEL_NAME,
                "api_key": settings.ANTHROPIC_API_KEY,
                "base_url": settings.ANTHROPIC_BASE_URL,
                "requires_api_key": True,
            },
            LLMProvider.OPENROUTER: {
                "model": settings.OPENROUTER_MODEL_NAME,
                "api_key": settings.OPENROUTER_API_KEY,
                "base_url": settings.OPENROUTER_BASE_URL,
                "requires_api_key": True,
            },
        }

        if provider == LLMProvider.CUSTOM:
            logger.info("Custom provider requires user-specific configuration via UI")
            return None

        if provider == LLMProvider.BEDROCK:
            # Bedrock has no `base_url`/`api_key` shape at all (SDK call, credentials via
            # the IAM chain — see `llm_bedrock.py`'s module docstring), so it does not fit
            # the dict-driven validation below, which assumes an HTTP endpoint and casts
            # `base_url` to `str()` unconditionally. Validated instead against the two
            # things that actually gate a Bedrock call: a model ID, and a region for
            # boto3 to construct the client against (`_client` in `llm_bedrock.py` raises
            # `BedrockNotConfiguredError` without one).
            from app.services.bedrock_settings import env_region
            from app.services.bedrock_settings import resolve_default_model

            model = resolve_default_model()
            if not env_region():
                logger.info("Bedrock provider configured but no AWS region set")
                return None
            return model, None, None

        if provider not in provider_settings:
            logger.warning(f"Unsupported LLM provider: {provider}")
            return None

        cfg = provider_settings[provider]
        model = str(cfg["model"])
        api_key_value = cfg["api_key"]
        api_key = str(api_key_value) if api_key_value else None
        base_url = str(cfg["base_url"])

        # Validate model
        if not model or not model.strip():
            logger.info(f"{provider.value} provider configured but no model name set")
            return None

        # Check for invalid model defaults (e.g., vLLM "gpt-oss")
        invalid_models = cfg.get("invalid_model_defaults", [])
        if model.strip() in invalid_models:
            logger.info(f"{provider.value} provider configured but no valid model name set")
            return None

        # Check for invalid URL defaults
        invalid_urls = cfg.get("invalid_url_defaults", [])
        if base_url in invalid_urls:
            logger.info(
                f"{provider.value} provider configured but using default localhost endpoint (likely not available)"
            )
            return None

        # Validate API key if required
        if cfg.get("requires_api_key") and (not api_key or not api_key.strip()):
            logger.info(f"{provider.value} provider configured but no API key set")
            return None

        return model, api_key, base_url

    @staticmethod
    def _resolve_system_provider() -> tuple[LLMProvider, str, str | None, str | None] | None:
        """``(provider, model, api_key, base_url)`` from the env settings, or ``None``.

        No I/O at all. ``LLM_PROVIDER`` alone is not enough: the provider also needs
        its credentials or endpoint (``_get_provider_config``) and an endpoint the
        client would accept.
        """
        if not settings.LLM_PROVIDER or settings.LLM_PROVIDER.strip() == "":
            logger.info("No LLM provider configured (LLM_PROVIDER not set)")
            return None

        try:
            provider = LLMProvider(settings.LLM_PROVIDER)
        except ValueError as e:
            logger.warning(f"Invalid LLM provider '{settings.LLM_PROVIDER}': {e}")
            return None

        provider_config = LLMService._get_provider_config(provider)
        if provider_config is None:
            return None

        model, api_key, base_url = provider_config
        if not LLMService._endpoint_resolves(provider, base_url):
            logger.info(f"{provider.value} provider configured but no usable endpoint")
            return None
        return provider, model, api_key, base_url

    @staticmethod
    def create_from_system_settings() -> "LLMService | None":
        """Create LLMService from system settings"""
        resolved = LLMService._resolve_system_provider()
        if resolved is None:
            return None

        provider, model, api_key, base_url = resolved

        window = 32768  # Conservative system default
        try:
            from app.db.base import SessionLocal
            from app.services import llm_context_window

            db = SessionLocal()
            try:
                window = llm_context_window.effective_window(
                    db, provider=str(provider), base_url=base_url, model=model, declared=window
                )
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 -- a capability read must not break the fallback service
            logger.warning("Context-window measurement unavailable for the system LLM: %s", exc)

        try:
            config = LLMConfig(
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                max_tokens=window,
                temperature=0.3,
            )

            logger.info(
                f"Created LLMService from system settings: {provider}/{model}, context_window={config.max_tokens}"
            )
            return LLMService(config)
        except Exception as e:
            logger.error(f"Failed to create LLMService from system settings: {e}")
            return None


# Utility function for quick LLM availability check
async def is_llm_available(user_id: int | None = None) -> bool:
    """Quick check to see if any LLM provider is available.

    Runs the blocking health check in a thread to avoid blocking the
    async event loop. Uses a 3-second timeout to fail fast when the
    LLM server is unreachable.
    """
    import asyncio

    def _check() -> bool:
        try:
            llm_service = LLMService.create_from_settings(user_id=user_id)
            if llm_service is None:
                return False
            health_ok = llm_service.health_check()
            llm_service.close()
            return health_ok
        except Exception as e:
            logger.error(f"LLM availability check failed: {e}")
            return False

    try:
        return await asyncio.to_thread(_check)
    except Exception:
        return False
