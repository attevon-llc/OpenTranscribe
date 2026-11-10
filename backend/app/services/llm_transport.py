"""Endpoint resolution, headers, the pinned session and the HTTP send for :class:`LLMService`.

Split out of ``llm_service`` (v0.7.0 L07) as one concern: everything that decides *where* a
request goes and sends it. ``LLMService`` inherits these, so the call paths are unchanged.
"""

import contextlib
import json
import logging
import threading
import time
from contextlib import ExitStack
from typing import TYPE_CHECKING
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import settings
from app.core.exceptions import LLMResponseError
from app.services.llm_types import SDK_PROVIDERS
from app.services.llm_types import LLMHost
from app.services.llm_types import LLMProvider
from app.utils.llm_log_safety import describe_llm_text
from app.utils.llm_log_safety import log_llm_text_excerpt

if TYPE_CHECKING:
    from app.utils.url_validation import PinnedTarget

logger = logging.getLogger(__name__)


class LLMTransportMixin(LLMHost):
    """Endpoints, headers, the pinned session and the raw send (mixed into ``LLMService``)."""

    #: Returned to the operator when the endpoint is refused. Deliberately does NOT say
    #: *why* — the reason distinguishes "private address" from "cannot resolve", which
    #: turns an operator-visible error into a network scanner. The reason is logged.
    BLOCKED_ENDPOINT_MESSAGE = (
        "The configured LLM endpoint is not a permitted outbound target. It must be a "
        "publicly reachable http(s) address. Set LLM_ALLOW_PRIVATE_ENDPOINTS=true to "
        "allow a self-hosted endpoint on a private network."
    )

    # Set by ``LLMService.__init__``; declared so the checker sees them on this mixin.
    _retry_strategy: Retry
    _session_lock: threading.Lock
    _pinned_stack: ExitStack | None
    _pinned: tuple[str, requests.Session, "PinnedTarget"] | None

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
        return bool(LLMTransportMixin._build_endpoints(base_url).get(provider))

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
            from app.services.llm_service import LLMEndpointBlockedError
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
            raise LLMResponseError(f"LLM API error: HTTP {response.status_code}")

        try:
            result: dict[str, Any] = response.json()
            return result
        except json.JSONDecodeError as e:
            logger.error(
                "Failed to parse LLM response body as JSON (%s)", describe_llm_text(response.text)
            )
            log_llm_text_excerpt(logger, "Unparseable LLM response body", response.text)
            raise LLMResponseError("LLM response body is not valid JSON") from e

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
