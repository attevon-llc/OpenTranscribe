"""
LLM Service for OpenTranscribe

Provides unified interface for multiple LLM providers using synchronous HTTP requests.
Designed specifically for Celery tasks - no asyncio conflicts.
"""

import contextlib
import json
import logging
import re
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Any
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import settings
from app.core.constants import LLM_OUTPUT_LANGUAGES
from app.services.llm_stream import LLMStreamEvent
from app.services.llm_stream import apply_stream_payload
from app.services.llm_stream import get_stream_parser

if TYPE_CHECKING:  # pragma: no cover - import cost is paid only by type checkers
    from app.utils.url_validation import PinnedTarget

logger = logging.getLogger(__name__)

# OpenAI reasoning models that don't support temperature/sampling parameters
# These models use internal reasoning processes incompatible with temperature control
# See: https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/reasoning
OPENAI_REASONING_MODEL_PREFIXES = (
    "o1",  # o1, o1-mini, o1-preview
    "o3",  # o3, o3-mini
    "o4",  # o4-mini
    "gpt-5",  # gpt-5 series
)


class LLMProvider(StrEnum):
    OPENAI = "openai"
    VLLM = "vllm"
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    BEDROCK = "bedrock"  # AWS-native; boto3 Converse API, credentials via the IAM chain
    CUSTOM = "custom"
    # Legacy - kept for backward compatibility
    CLAUDE = "claude"  # Deprecated: use ANTHROPIC instead


#: Providers that cannot authenticate without an API key, so a blank one is refused locally
#: rather than sent as `Authorization: Bearer ` for the remote to reject. VLLM, OLLAMA, BEDROCK
#: and CUSTOM are excluded deliberately: the first two are self-hosted, BEDROCK uses the IAM
#: credential chain, and CUSTOM may front an unauthenticated gateway.
PROVIDERS_REQUIRING_API_KEY: frozenset[LLMProvider] = frozenset(
    {
        LLMProvider.OPENAI,
        LLMProvider.ANTHROPIC,
        LLMProvider.CLAUDE,
        LLMProvider.OPENROUTER,
    }
)


# Providers reached through a vendor SDK rather than an HTTP endpoint we POST to.
# They skip the endpoint-map validation and the shared requests.Session machinery.
SDK_PROVIDERS = frozenset({LLMProvider.BEDROCK})


@dataclass
class LLMResponse:
    """Standardized response from LLM"""

    content: str
    usage_tokens: int | None = None
    finish_reason: str | None = None
    model: str | None = None
    provider: str | None = None


@dataclass
class LLMConfig:
    """Configuration for LLM provider"""

    provider: LLMProvider
    model: str
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int = 8192  # User-configured context window
    temperature: float = 0.3
    response_tokens: int = 4000  # Max tokens for response


class LLMEndpointBlockedError(Exception):
    """The configured LLM endpoint is not a permitted outbound target.

    Raised *before* any connection is attempted, by the SSRF guard on the shared
    request path. Distinct from a network error so callers can tell "we refused to
    dial this" from "we dialled it and it failed".
    """


class LLMService:
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

        # Provider-specific endpoint mappings
        def build_endpoint(base_url: str) -> str:
            """Build chat completions endpoint"""
            clean_url = base_url.strip().rstrip("/")
            if clean_url.endswith("/v1"):
                return f"{clean_url}/chat/completions"
            else:
                return f"{clean_url}/v1/chat/completions"

        def build_ollama_endpoint(base_url: str) -> str:
            """Build Ollama chat endpoint using native API"""
            clean_url = base_url.strip().rstrip("/")
            # Remove /v1 suffix if present since we're using native API
            if clean_url.endswith("/v1"):
                clean_url = clean_url[:-3]
            return f"{clean_url}/api/chat"

        self.endpoints = {
            # Dynamic endpoints - respect custom base_url for OpenAI-compatible servers (vLLM, etc.)
            LLMProvider.OPENAI: build_endpoint(config.base_url)
            if config.base_url
            else "https://api.openai.com/v1/chat/completions",
            LLMProvider.VLLM: build_endpoint(config.base_url) if config.base_url else None,
            LLMProvider.OLLAMA: build_ollama_endpoint(config.base_url)
            if config.base_url
            else "http://localhost:11434/api/chat",
            LLMProvider.CUSTOM: build_endpoint(config.base_url) if config.base_url else None,
            LLMProvider.OPENROUTER: build_endpoint(config.base_url)
            if config.base_url
            else "https://openrouter.ai/api/v1/chat/completions",
            # Fixed endpoints - these providers don't support custom base URLs
            LLMProvider.CLAUDE: "https://api.anthropic.com/v1/messages",
            LLMProvider.ANTHROPIC: "https://api.anthropic.com/v1/messages",
        }

        # SDK-based providers have no HTTP endpoint to validate — Bedrock is reached
        # through boto3, which resolves the endpoint from the region itself.
        if config.provider not in SDK_PROVIDERS and not self.endpoints.get(config.provider):
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

    def _is_reasoning_model(self) -> bool:
        """
        Check if the current model is an OpenAI reasoning model.

        OpenAI reasoning models (o1, o3, o4, gpt-5 series) don't support
        temperature, top_p, presence_penalty, or frequency_penalty parameters.
        These parameters must be omitted entirely from API requests.
        """
        if self.config.provider not in [LLMProvider.OPENAI, LLMProvider.OPENROUTER]:
            return False

        model_lower = self.config.model.lower()
        return any(model_lower.startswith(prefix) for prefix in OPENAI_REASONING_MODEL_PREFIXES)

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
            headers["HTTP-Referer"] = "https://opentranscribe.ai"
            headers["X-Title"] = "OpenTranscribe"
        elif self.config.provider == LLMProvider.CUSTOM and self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        return headers

    def _prepare_claude_payload(self, messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
        """Prepare payload for Claude/Anthropic API."""
        system_message = ""
        user_messages = []

        for msg in messages:
            if msg.get("role") == "system":
                system_message = msg.get("content", "")
            elif msg.get("role") in ["user", "assistant"]:
                user_messages.append({"role": msg["role"], "content": msg["content"]})

        # Add response prefilling for JSON output if requested
        if (
            kwargs.get("prefill_json", False)
            and user_messages
            and user_messages[-1]["role"] == "user"
        ):
            user_messages.append({"role": "assistant", "content": "{"})

        payload = {
            "model": self.config.model,
            "messages": user_messages,
            "max_tokens": kwargs.get("max_tokens", self.config.response_tokens),
            "temperature": kwargs.get("temperature", self.config.temperature),
        }

        if system_message:
            payload["system"] = system_message

        return payload

    def _prepare_ollama_payload(self, messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
        """Prepare payload for Ollama native API."""
        payload = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": kwargs.get("temperature", self.config.temperature),
                "num_predict": kwargs.get("max_tokens", self.config.response_tokens),
                "num_ctx": kwargs.get("num_ctx", self.user_context_window),
            },
        }

        if "format" in kwargs:
            payload["format"] = kwargs["format"]

        return payload

    def _prepare_openai_payload(self, messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
        """Prepare payload for OpenAI-compatible APIs."""
        payload = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", self.config.response_tokens),
            "stream": False,
        }

        # Reasoning models don't support temperature
        if not self._is_reasoning_model():
            payload["temperature"] = kwargs.get("temperature", self.config.temperature)
            # Only when the caller asked for it. Sending a default would override
            # whatever the provider or model already tunes to, and reasoning
            # models reject it outright (handled by the branch above).
            if kwargs.get("top_p") is not None:
                payload["top_p"] = kwargs["top_p"]
        else:
            logger.info(
                f"Reasoning model detected ({self.config.model}): "
                f"omitting temperature and sampling parameters"
            )

        # Add vLLM-specific parameters
        if self.config.provider == LLMProvider.VLLM:
            payload.update(
                {
                    "top_p": kwargs.get("top_p", 0.9),
                    "frequency_penalty": kwargs.get("frequency_penalty", 0.0),
                    "presence_penalty": kwargs.get("presence_penalty", 0.0),
                }
            )
            # Reasoning must be ACTIVATED, or vLLM streams it as the answer (issue #439).
            #
            # A reasoning-capable chat template only emits its "start of thought"
            # token when asked (gemma4 and qwen3 both spell that ask
            # `enable_thinking`). Unasked, gemma4's template instead appends an
            # already-closed empty thought channel to the prompt — the model reasons
            # regardless, so its *generated* text carries no opener, only a bare
            # closer. vLLM's streaming reasoning parser enters reasoning mode on the
            # opener alone, so it never fires and the whole chain-of-thought is
            # streamed on `delta.content`; worse, the gemma4 parser disables
            # special-token stripping to protect its boundary tokens, so the bare
            # closer reaches the answer as a literal control token.
            #
            # Asking for thinking is therefore the fix, and it is the server's own
            # mechanism: the parser then splits the block itself and reasoning
            # arrives on `delta.reasoning` / `delta.reasoning_content`, both of which
            # `llm_stream.parse_openai_sse` already routes to the collapsible display.
            # A template with no such flag simply ignores an unused kwarg.
            #
            # vLLM only: `chat_template_kwargs` is its extension, and "custom"
            # OpenAI-clones 400 on unknown payload keys — the same reason they are
            # excluded from `llm_stream.USAGE_OPTION_PROVIDERS`.
            if kwargs.get("enable_thinking", True):
                payload["chat_template_kwargs"] = {"enable_thinking": True}

        return payload

    def _prepare_payload(self, messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
        """
        Prepare request payload for the API based on provider requirements.

        Converts standard OpenAI-format messages to provider-specific formats
        and adds appropriate parameters for each LLM provider.

        Args:
            messages: List of message dictionaries in OpenAI format
            **kwargs: Additional parameters to override defaults
                - prefill_json: If True, adds JSON prefill to force structured output

        Returns:
            Dictionary containing the API request payload

        Note:
            - Claude/Anthropic: Separates system messages from user/assistant messages
            - Ollama: Uses native /api/chat format with messages array
            - Other providers: Use standard OpenAI format with provider-specific params
            - Response prefilling: For Claude, adds assistant message with "{" to force JSON
        """
        if self.config.provider in [LLMProvider.CLAUDE, LLMProvider.ANTHROPIC]:
            return self._prepare_claude_payload(messages, **kwargs)

        if self.config.provider == LLMProvider.OLLAMA:
            return self._prepare_ollama_payload(messages, **kwargs)

        return self._prepare_openai_payload(messages, **kwargs)

    def _extract_claude_response(self, data: dict) -> tuple[str, int | None, str | None]:
        """Extract content, usage tokens, and finish reason from Claude/Anthropic response."""
        if "content" not in data or not data["content"]:
            raise Exception("No content in Claude response")

        content_blocks = data["content"]
        if isinstance(content_blocks, list) and content_blocks:
            content = content_blocks[0].get("text", "")
        else:
            content = str(content_blocks)

        usage_tokens = None
        if "usage" in data:
            usage_tokens = data["usage"].get("output_tokens", 0) + data["usage"].get(
                "input_tokens", 0
            )

        finish_reason = data.get("stop_reason")
        return content, usage_tokens, finish_reason

    def _extract_ollama_response(self, data: dict) -> tuple[str, int | None, str | None]:
        """Extract content, usage tokens, and finish reason from Ollama response."""
        if "message" not in data:
            logger.error(
                f"Ollama response missing 'message' field. Response keys: {list(data.keys())}"
            )
            logger.debug(f"Full Ollama response: {json.dumps(data, indent=2)}")
            raise Exception("No message in Ollama response")

        content = data["message"].get("content", "")
        finish_reason = data.get("done_reason", "stop")

        if not content:
            logger.error(
                f"Ollama message field exists but content is empty. Message: {data.get('message')}"
            )
            logger.debug(f"Full Ollama response: {json.dumps(data, indent=2)}")

        usage_tokens = None
        if "prompt_eval_count" in data and "eval_count" in data:
            usage_tokens = data.get("prompt_eval_count", 0) + data.get("eval_count", 0)

        return content, usage_tokens, finish_reason

    def _extract_openai_response(self, data: dict) -> tuple[str, int | None, str | None]:
        """Extract content, usage tokens, and finish reason from OpenAI-compatible response."""
        if "choices" not in data or not data["choices"]:
            raise Exception("No choices in LLM response")

        choice = data["choices"][0]
        # `or ""`, not a default: a reasoning model that spends its whole token
        # budget inside the thought channel returns `"content": null` with
        # `finish_reason: "length"` — the key is present, so `.get(k, "")` yields
        # None and every caller that concatenates or strips this blows up.
        content = choice.get("message", {}).get("content") or ""
        finish_reason = choice.get("finish_reason")

        usage_tokens = None
        if "usage" in data:
            usage_tokens = data["usage"].get("total_tokens")

        return content, usage_tokens, finish_reason

    def _extract_response_content(self, data: dict) -> tuple[str, int | None, str | None]:
        """Extract content from response based on provider type."""
        if self.config.provider in [LLMProvider.CLAUDE, LLMProvider.ANTHROPIC]:
            return self._extract_claude_response(data)
        elif self.config.provider == LLMProvider.OLLAMA:
            return self._extract_ollama_response(data)
        else:
            return self._extract_openai_response(data)

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
            logger.error(f"Failed to parse LLM response: {response.text}")
            raise Exception(f"Invalid JSON response: {e}") from e

    def chat_completion(self, messages: list[dict[str, str]], **kwargs) -> LLMResponse:
        """
        Send chat completion request to LLM provider
        """
        url = self.endpoints[self.config.provider]
        if url is None:
            raise ValueError(f"No endpoint configured for provider {self.config.provider}")
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
            from app.services.llm_bedrock import stream_converse

            yield from stream_converse(
                model=self.config.model,
                region=settings.BEDROCK_REGION,
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

    def estimate_tokens(self, text: str) -> int:
        """Public wrapper over the internal token heuristic (see :meth:`_estimate_tokens`)."""
        return self._estimate_tokens(text)

    def _estimate_tokens(self, text: str) -> int:
        """
        Estimate token count using more accurate heuristics.

        This method provides a reasonable approximation for OpenAI-style tokenization
        without requiring the tiktoken library. The estimation is conservative to
        prevent context window overflow.

        Args:
            text: Input text to tokenize

        Returns:
            Estimated token count

        Note:
            - Uses word-based and character-based heuristics
            - Accounts for common punctuation and formatting
            - Returns slightly higher estimates to be safe
        """
        if not text:
            return 0

        # Basic word count
        words = text.split()
        word_count = len(words)

        # Character-based estimation for better accuracy
        char_count = len(text)

        # Combine both methods:
        # - English averages ~4.7 characters per token
        # - But also consider word boundaries and punctuation
        from app.core.constants import CHARS_PER_TOKEN_ESTIMATE
        from app.core.constants import SUBWORD_TOKENIZATION_FACTOR
        from app.core.constants import TOKEN_ESTIMATION_BUFFER

        char_based_estimate = char_count / CHARS_PER_TOKEN_ESTIMATE
        word_based_estimate = word_count * SUBWORD_TOKENIZATION_FACTOR

        # Use the higher estimate to be conservative
        estimated_tokens = max(char_based_estimate, word_based_estimate)

        # Add buffer for safety
        return int(estimated_tokens * TOKEN_ESTIMATION_BUFFER)

    def _split_oversized_chunk_by_sentences(self, chunk: str, available_tokens: int) -> list[str]:
        """Split an oversized chunk into smaller chunks by sentence boundaries."""
        sentences = re.split(r"(?<=[.!?])\s+", chunk)
        sub_chunks = []
        sub_chunk = ""

        for sentence in sentences:
            test_chunk = sub_chunk + sentence + " "
            if self._estimate_tokens(test_chunk) <= available_tokens:
                sub_chunk = test_chunk
            else:
                if sub_chunk.strip():
                    sub_chunks.append(sub_chunk.strip())
                sub_chunk = sentence + " "

        if sub_chunk.strip():
            sub_chunks.append(sub_chunk.strip())

        return sub_chunks

    def _split_by_speaker_segments(self, transcript: str, available_tokens: int) -> list[str]:
        """Split transcript by speaker changes into appropriately sized chunks."""
        speaker_segments = re.split(r"(\n[A-Z_][A-Z0-9_]*:\s*\[\d+:\d+\])", transcript)
        chunks = []
        current_chunk = ""
        current_size = 0

        for segment in speaker_segments:
            segment_size = self._estimate_tokens(segment)

            if current_size + segment_size > available_tokens and current_chunk:
                chunks.append(current_chunk.strip())
                logger.debug(f"Created chunk {len(chunks)}: {len(current_chunk)} chars")
                current_chunk = segment
                current_size = segment_size
            else:
                current_chunk += segment
                current_size += segment_size

        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        return chunks

    def _chunk_transcript_intelligently(
        self, transcript: str, chunk_overlap: int = 200
    ) -> list[str]:
        """
        Split transcript into intelligent chunks using ONLY user's max_tokens setting
        """
        available_tokens = self.user_context_window - 2000  # Reserve for prompt + response
        estimated_tokens = self._estimate_tokens(transcript)

        logger.info(
            f"Chunking transcript: {len(transcript)} chars, estimated {estimated_tokens} tokens"
        )
        logger.info(
            f"Using user context window: {self.user_context_window}, available for content: {available_tokens}"
        )

        if estimated_tokens <= available_tokens:
            logger.info("Transcript fits in single chunk")
            return [transcript]

        target_chars_per_chunk = int(available_tokens * 2.5)
        logger.info(f"Target chars per chunk: {target_chars_per_chunk}")

        chunks = self._split_by_speaker_segments(transcript, available_tokens)

        # Handle oversized chunks by splitting on sentences
        final_chunks = []
        for chunk in chunks:
            if self._estimate_tokens(chunk) <= available_tokens:
                final_chunks.append(chunk)
            else:
                logger.warning("Chunk too large, splitting by sentences")
                final_chunks.extend(
                    self._split_oversized_chunk_by_sentences(chunk, available_tokens)
                )

        if not final_chunks and transcript:
            logger.warning("No chunks created, truncating original transcript")
            final_chunks = [transcript[:target_chars_per_chunk]]

        logger.info(
            f"Split transcript into {len(final_chunks)} chunks using user context window: {self.user_context_window}"
        )
        return final_chunks

    def _is_truncated(self, response: "LLMResponse") -> bool:
        """Check if an LLM response was truncated due to token limit."""
        return response.finish_reason in ("length", "max_tokens")

    def generate_summary(
        self,
        transcript: str,
        speaker_data: dict[str, Any] | None = None,
        user_id: int | None = None,
        output_language: str = "en",
        organization_context: str = "",
        prompt_uuid: str | None = None,
    ) -> dict[str, Any]:
        """
        Generate structured summary from transcript.

        Args:
            transcript: Full transcript text with speaker labels
            speaker_data: Optional speaker statistics (talk time, word count, etc.)
            user_id: Optional user ID for loading custom prompts
            output_language: ISO 639-1 code for output language (default: "en")
            organization_context: Organization/project context to inject into prompts

        Returns:
            Structured summary dict with metadata
        """
        from app.core.constants import LLM_OUTPUT_LANGUAGES
        from app.utils.prompt_manager import get_user_active_prompt

        prompt_template = get_user_active_prompt(user_id, prompt_uuid=prompt_uuid)

        # Get language name for prompt
        output_language_name = LLM_OUTPUT_LANGUAGES.get(output_language, "English")
        logger.info(f"Generating summary in {output_language_name} ({output_language})")
        if organization_context:
            logger.info(f"Organization context provided ({len(organization_context)} chars)")

        # Chunk transcript using ONLY user's context window setting
        transcript_chunks = self._chunk_transcript_intelligently(transcript)

        if len(transcript_chunks) == 1:
            # Single chunk processing
            logger.info(f"Processing transcript as single section ({len(transcript)} chars)")
            return self._process_single_chunk(
                transcript_chunks[0],
                speaker_data,
                prompt_template,
                output_language_name,
                organization_context,
            )
        else:
            # Multi-chunk processing
            logger.info(f"Processing transcript in {len(transcript_chunks)} sections")
            return self._process_multiple_chunks(
                transcript_chunks,
                speaker_data,
                prompt_template,
                output_language_name,
                organization_context,
            )

    def _build_org_context_block(self, organization_context: str) -> str:
        """Build the organization context block for system prompts."""
        if not organization_context or not organization_context.strip():
            return ""
        return (
            " Use the following organization/project context to inform your analysis"
            " and make the summary more relevant to the organization's domain:"
            f" [{organization_context.strip()}]"
        )

    def _process_single_chunk(
        self,
        transcript: str,
        speaker_data: dict[str, Any] | None,
        prompt_template: str,
        output_language_name: str = "English",
        organization_context: str = "",
    ) -> dict[str, Any]:
        """Process single transcript chunk"""
        formatted_prompt = prompt_template.format(
            transcript=transcript,
            speaker_data=json.dumps(speaker_data or {}, indent=2),
        )

        # Build system message with language instruction
        language_instruction = (
            f" Generate all output text in {output_language_name}."
            if output_language_name != "English"
            else ""
        )
        org_context_block = self._build_org_context_block(organization_context)

        messages = [
            {
                "role": "system",
                "content": f"You are an expert meeting analyst. Analyze transcripts and generate structured summaries in the exact JSON format specified.{language_instruction}{org_context_block}",
            },
            {"role": "user", "content": formatted_prompt},
        ]

        # Use response prefilling to force JSON output (bypasses preamble)
        response = self.chat_completion(
            messages, max_tokens=self.response_tokens, temperature=0.1, prefill_json=True
        )

        # Retry with doubled tokens if response was truncated
        if self._is_truncated(response):
            retry_tokens = min(self.response_tokens * 2, self.user_context_window // 2)
            logger.warning(
                f"Summary response truncated (finish_reason={response.finish_reason}), "
                f"retrying with max_tokens={retry_tokens}"
            )
            response = self.chat_completion(
                messages, max_tokens=retry_tokens, temperature=0.1, prefill_json=True
            )

        return self._parse_summary_response(response, len(transcript))

    def _process_multiple_chunks(
        self,
        chunks: list[str],
        speaker_data: dict[str, Any] | None,
        prompt_template: str,
        output_language_name: str = "English",
        organization_context: str = "",
    ) -> dict[str, Any]:
        """Process multiple transcript chunks in parallel using ThreadPoolExecutor.

        Each chunk is summarized independently, then combined into a final summary.
        Parallelism is capped at min(num_chunks, 4) to avoid overwhelming the LLM API.
        """
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import as_completed

        num_chunks = len(chunks)
        max_workers = min(num_chunks, 4)
        _error_placeholder: dict[str, Any] = {
            "key_points": [],
            "speakers_in_section": [],
            "decisions": [],
            "action_items": [],
            "topics_discussed": [],
        }
        # Pre-fill with error placeholders; every slot is overwritten on success.
        section_summaries: list[dict[str, Any]] = [
            {**_error_placeholder, "key_points": [f"Section {i + 1}: Not processed"]}
            for i in range(num_chunks)
        ]

        logger.info(f"Processing {num_chunks} sections in parallel (max_workers={max_workers})")

        def _process_one(index: int, chunk: str) -> tuple[int, dict[str, Any]]:
            """Process a single chunk, returning (index, result)."""
            logger.info(f"Processing section {index + 1}/{num_chunks} ({len(chunk)} chars)")
            return (
                index,
                self._summarize_section(
                    chunk,
                    index + 1,
                    num_chunks,
                    speaker_data,
                    prompt_template,
                    output_language_name,
                    organization_context,
                ),
            )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_process_one, i, chunk): i for i, chunk in enumerate(chunks)}

            for future in as_completed(futures):
                idx = futures[future]
                try:
                    result_idx, section_summary = future.result()
                    section_summaries[result_idx] = section_summary
                    logger.info(f"Section {result_idx + 1} processing completed successfully")
                except Exception as e:
                    logger.error(f"Failed to process section {idx + 1}: {type(e).__name__}: {e}")
                    section_summaries[idx] = {
                        "key_points": [f"Section {idx + 1}: Processing failed - {str(e)[:100]}..."],
                        "speakers_in_section": [],
                        "decisions": [],
                        "action_items": [],
                        "topics_discussed": [],
                    }

        # Combine sections into final summary
        logger.info("Combining section summaries into final comprehensive summary")
        return self._combine_sections(
            section_summaries,
            speaker_data,
            prompt_template,
            num_chunks,
            output_language_name,
            organization_context,
        )

    def _summarize_section(
        self,
        chunk: str,
        section_num: int,
        total_sections: int,
        speaker_data: dict[str, Any] | None,
        prompt_template: str,
        output_language_name: str = "English",
        organization_context: str = "",
    ) -> dict[str, Any]:
        """Summarize a single section"""
        formatted_prompt = prompt_template.format(
            transcript=chunk,
            speaker_data=json.dumps(speaker_data or {}, indent=2),
        )

        # Build system message with language instruction
        language_instruction = (
            f" Generate all output text in {output_language_name}."
            if output_language_name != "English"
            else ""
        )
        org_context_block = self._build_org_context_block(organization_context)

        messages = [
            {
                "role": "system",
                "content": f"You are analyzing section {section_num} of {total_sections}. Provide a structured summary of this section.{language_instruction}{org_context_block}",
            },
            {"role": "user", "content": formatted_prompt},
        ]

        # Use response prefilling for consistent JSON output
        response = self.chat_completion(
            messages, max_tokens=min(4000, self.response_tokens), temperature=0.1, prefill_json=True
        )

        try:
            content = response.content.strip()
            if content.startswith("```json") and content.endswith("```"):
                content = content[7:-3].strip()
            elif content.startswith("```") and content.endswith("```"):
                content = content[3:-3].strip()

            parsed_result: dict[str, Any] = json.loads(content)
            return parsed_result
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse section {section_num} JSON: {e}")
            return {
                "key_points": [f"Section {section_num}: Failed to parse structured summary"],
                "speakers_in_section": [],
                "decisions": [],
                "action_items": [],
                "topics_discussed": [],
            }

    def _combine_sections(
        self,
        sections: list[dict[str, Any]],
        speaker_data: dict[str, Any] | None,
        prompt_template: str,
        total_sections: int,
        output_language_name: str = "English",
        organization_context: str = "",
    ) -> dict[str, Any]:
        """Combine multiple section summaries into final summary"""
        combined_content = f"SECTION SUMMARIES TO COMBINE:\n{json.dumps(sections, indent=2)}"

        formatted_prompt = prompt_template.format(
            transcript=combined_content,
            speaker_data=json.dumps(speaker_data or {}, indent=2),
        )

        # Build system message with language instruction
        language_instruction = (
            f" Generate all output text in {output_language_name}."
            if output_language_name != "English"
            else ""
        )
        org_context_block = self._build_org_context_block(organization_context)

        messages = [
            {
                "role": "system",
                "content": f"You are combining multiple section summaries into a comprehensive BLUF format summary.{language_instruction}{org_context_block}",
            },
            {"role": "user", "content": formatted_prompt},
        ]

        try:
            # Use response prefilling for final combined summary
            response = self.chat_completion(
                messages, max_tokens=self.response_tokens, temperature=0.1, prefill_json=True
            )

            # Retry with doubled tokens if response was truncated
            if self._is_truncated(response):
                retry_tokens = min(self.response_tokens * 2, self.user_context_window // 2)
                logger.warning(
                    f"Combined summary truncated (finish_reason={response.finish_reason}), "
                    f"retrying with max_tokens={retry_tokens}"
                )
                response = self.chat_completion(
                    messages, max_tokens=retry_tokens, temperature=0.1, prefill_json=True
                )

            return self._parse_summary_response(
                response,
                0,
                {"sections_processed": total_sections, "processing_method": "multi-section"},
            )
        except Exception as e:
            logger.error(f"Failed to combine sections: {e}")
            return {
                "bluf": "Multi-section summary generation completed with partial results.",
                "brief_summary": f"Summary generated from {len(sections)} sections.",
                "major_topics": [],
                "action_items": [],
                "key_decisions": [],
                "follow_up_items": [],
                "metadata": {
                    "provider": self.config.provider.value,
                    "model": self.config.model,
                    "sections_processed": len(sections),
                    "error": f"Section combining failed: {str(e)}",
                },
            }

    def _parse_summary_response(
        self,
        response: LLMResponse,
        transcript_length: int,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Parse LLM response into flexible structured summary.

        IMPORTANT: This method accepts ANY valid JSON structure from custom AI prompts.
        No field validation is performed - we trust the LLM to follow the prompt format.
        """
        try:
            content = response.content.strip()

            # Handle response prefilling: if content starts with partial JSON due to prefill,
            # prepend the opening brace that was used in prefilling
            if not content.startswith("{") and not content.startswith("```"):
                content = "{" + content

            # Extract JSON from code blocks
            if content.startswith("```json") and content.endswith("```"):
                content = content[7:-3].strip()
            elif content.startswith("```") and content.endswith("```"):
                content = content[3:-3].strip()

            # Parse JSON - accept ANY structure
            summary_data: dict[str, Any] = json.loads(content)

            # NO FIELD VALIDATION - accept any structure from custom prompts

            # Add metadata
            metadata = {
                "provider": self.config.provider.value,
                "model": self.config.model,
                "usage_tokens": response.usage_tokens,
                "transcript_length": transcript_length,
                "user_context_window": self.user_context_window,
            }

            if extra_metadata:
                metadata.update(extra_metadata)

            summary_data["metadata"] = metadata

            logger.info(
                f"Successfully parsed flexible summary with fields: {list(summary_data.keys())}"
            )
            return summary_data

        except json.JSONDecodeError as e:
            logger.warning(f"Initial JSON parse failed: {e}")

            # Attempt JSON repair for truncated responses
            repaired = self._repair_truncated_json(content)
            if repaired is not None:
                logger.info("JSON repair succeeded, using repaired summary")
                metadata = {
                    "provider": self.config.provider.value,
                    "model": self.config.model,
                    "usage_tokens": response.usage_tokens,
                    "transcript_length": transcript_length,
                    "user_context_window": self.user_context_window,
                    "json_repaired": True,
                }
                if extra_metadata:
                    metadata.update(extra_metadata)
                repaired["metadata"] = metadata
                return repaired

            logger.error(f"JSON repair also failed. Response content: {response.content[:500]}...")

            # Return minimal error structure
            return {
                "error": "JSON parsing failed",
                "error_detail": str(e),
                "raw_response_preview": response.content[:500],
                "metadata": {
                    "provider": self.config.provider.value,
                    "model": self.config.model,
                    "error": f"JSON parsing failed: {str(e)}",
                    "user_context_window": self.user_context_window,
                },
            }

    def _repair_truncated_json(self, content: str) -> dict[str, Any] | None:  # noqa: C901
        """
        Attempt to repair truncated JSON by closing open strings, arrays, and objects.

        Returns parsed dict on success, None on failure.
        """
        try:
            text = content.rstrip()

            # Track JSON structure state
            in_string = False
            escape_next = False
            stack: list[str] = []  # tracks '{' and '['

            for char in text:
                if escape_next:
                    escape_next = False
                    continue
                if char == "\\":
                    if in_string:
                        escape_next = True
                    continue
                if char == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if char in ("{", "["):
                    stack.append(char)
                elif char == "}":
                    if stack and stack[-1] == "{":
                        stack.pop()
                elif char == "]" and stack and stack[-1] == "[":
                    stack.pop()

            # Build repair suffix
            repair = ""

            # Close unterminated string
            if in_string:
                repair += '"'

            # Check if we're mid-value in an object (e.g., truncated after a key's colon)
            # The closing brackets will handle structure
            stripped = (text + repair).rstrip()
            if stripped and stripped[-1] in (",", ":"):
                repair += '""'

            # Close open arrays and objects in reverse order
            for bracket in reversed(stack):
                if bracket == "{":
                    repair += "}"
                elif bracket == "[":
                    repair += "]"

            repaired_content = text + repair
            result: dict[str, Any] = json.loads(repaired_content)
            return result

        except (json.JSONDecodeError, Exception) as e:
            logger.debug(f"JSON repair failed: {e}")
            return None

    def validate_connection(self) -> tuple[bool, str]:
        """
        Validate connection to LLM provider.

        Uses the same endpoint resolution as chat_completion() to ensure
        the test accurately reflects what will happen during actual use.

        Returns:
            Tuple of (success, message)
        """
        # Reject a missing credential locally, before any outbound request. Nothing validated
        # the API key here, so a blank one produced `Authorization: Bearer ` and the provider
        # answered 401 — which meant the admin "test connection" button reported a generic
        # remote failure for a locally-obvious mistake, and the unit suite could only assert
        # it by really calling api.openai.com (issue #431).
        if (
            self.config.provider in PROVIDERS_REQUIRING_API_KEY
            and not (self.config.api_key or "").strip()
        ):
            return False, (
                f"An API key is required for {self.config.provider}. "
                "Add one in Settings before testing the connection."
            )

        try:
            headers = self._get_headers()

            # Claude/Anthropic providers don't have a models endpoint, test with a simple request
            if self.config.provider in [LLMProvider.CLAUDE, LLMProvider.ANTHROPIC]:
                # No local guard here: this branch reaches the network through
                # `chat_completion`, which now validates and PINS the URL it actually
                # POSTs to (`_endpoint_session`). The `is_safe_url(base_url)` check that
                # used to sit here was validate-only — it judged a *different* string
                # from the one fetched, resolved it a second time, and for these two
                # providers judged a `base_url` that is never used at all (their
                # endpoints are fixed). The refusal still surfaces here, as the
                # `LLMEndpointBlockedError` message caught below.
                #
                # Test with a simple message
                test_messages = [{"role": "user", "content": "Hi"}]
                response = self.chat_completion(test_messages, max_tokens=5)
                if response and response.content and response.content.strip():
                    return (
                        True,
                        f"Connection successful - Model responded: '{response.content[:50]}'",
                    )
                else:
                    return False, "Connection established but model returned empty response"

            else:
                # For OpenAI-compatible providers, derive models endpoint from chat completions endpoint
                # This ensures we test the same server that chat_completion() will use
                chat_endpoint = self.endpoints.get(self.config.provider)
                if not chat_endpoint:
                    return False, f"No endpoint configured for {self.config.provider}"

                # Derive models endpoint from chat completions endpoint
                # e.g., http://host:8000/v1/chat/completions → http://host:8000/v1/models
                if "/chat/completions" in chat_endpoint:
                    models_url = chat_endpoint.replace("/chat/completions", "/models")
                elif "/api/chat" in chat_endpoint:
                    # Ollama uses /api/chat, models endpoint is /api/tags
                    models_url = chat_endpoint.replace("/api/chat", "/api/tags")
                else:
                    # Fallback: try appending /models to base
                    models_url = chat_endpoint.rsplit("/", 1)[0] + "/models"

                logger.debug(
                    f"Testing connection to {self.config.provider}: {models_url} (derived from {chat_endpoint})"
                )

                # `models_url` is DERIVED from base_url, so the guard above validated a
                # different string than the one fetched. Validate and PIN the actual target:
                # `is_safe_url` resolves, judges, and discards, leaving requests to resolve
                # again at connect time — a rebinding window. `resolve_pinned_target` returns
                # the checked IP to dial plus the hostname to verify TLS against.
                from app.core.config import settings as _settings
                from app.utils.url_validation import pinned_requests_session
                from app.utils.url_validation import resolve_pinned_target

                target, reason = resolve_pinned_target(
                    models_url, allow_private=_settings.LLM_ALLOW_PRIVATE_ENDPOINTS
                )
                if target is None:
                    logger.warning("Connection test blocked for %s: %s", models_url, reason)
                    return False, (
                        "Connection failed: the endpoint must be a publicly reachable "
                        "http(s) address. Set LLM_ALLOW_PRIVATE_ENDPOINTS=true to allow a "
                        "self-hosted endpoint on a private network."
                    )

                # A one-off pinned session for this request: its SNI hostname is bound to
                # this target, so it must not be reused or redirected. It is deliberately
                # not the instance session `_endpoint_session` builds — that one is pinned
                # to the *chat* endpoint, and this is the derived models URL.
                # Losing the retry adapter is correct here — a connection *test* that
                # silently retries a 500 three times is reporting the wrong thing anyway.
                with pinned_requests_session(target) as pinned:
                    http_response = pinned.get(
                        target.url,
                        headers={**headers, **target.headers},
                        timeout=10,
                        allow_redirects=False,
                    )

                if http_response.status_code == 200:
                    return True, f"Connection successful (tested {models_url})"
                else:
                    return (
                        False,
                        f"Connection test failed with status {http_response.status_code} at {models_url}",
                    )

        except Exception as e:
            return False, f"Connection failed: {str(e)}"

    def close(self):
        """
        Close the session and clean up resources.

        Properly closes the HTTP session and releases any held connections.
        Should be called when the LLMService instance is no longer needed.
        """
        with self._session_lock:
            self._discard_pinned_session()
        logger.debug(f"Closed session for {self.config.provider}")

    def _build_known_speakers_context(self, known_speakers: list) -> str:
        """Build context string from known speaker profiles."""
        if not known_speakers:
            return "\n\nNo known speaker profiles provided for comparison.\n"

        context = "\n\nKNOWN SPEAKER PROFILES:\n"
        for i, speaker in enumerate(known_speakers[:15]):  # Limit to prevent token overflow
            description = speaker.get("description", "No description available")
            context += f"{i + 1}. {speaker['name']}: {description}\n"
        return context

    def _truncate_transcript_for_speakers(self, transcript: str, available_tokens: int) -> str:
        """Truncate transcript while preserving beginning and end context."""
        max_chars = available_tokens * 3  # Rough char to token ratio
        if len(transcript) <= max_chars:
            return transcript

        half_length = max_chars // 2
        return (
            transcript[:half_length]
            + "\n\n[... middle content truncated ...]\n\n"
            + transcript[-half_length:]
        )

    def _strip_markdown_fences(self, content: str) -> str:
        """Remove markdown code fences from content."""
        if content.startswith("```json") and "```" in content[7:]:
            fence_end = content.find("```", 7)
            return content[7:fence_end].strip()

        if content.startswith("```") and "```" in content[3:]:
            fence_end = content.find("```", 3)
            result = content[3:fence_end].strip()
            if result.startswith(("json", "JSON")):
                return result[4:].lstrip()
            return result

        return content

    def _find_json_object_bounds(self, content: str) -> str:
        """Find and extract complete JSON object from content."""
        json_start = content.find("{")
        if json_start > 0:
            content = content[json_start:]

        if not content.startswith("{"):
            return content

        brace_count = 0
        for i, char in enumerate(content):
            if char == "{":
                brace_count += 1
            elif char == "}":
                brace_count -= 1
                if brace_count == 0:
                    return content[: i + 1]

        return content

    def _extract_json_from_response(self, content: str) -> str:
        """Extract and clean JSON from LLM response content."""
        content = self._strip_markdown_fences(content)
        return self._find_json_object_bounds(content)

    def _validate_speaker_prediction(self, pred: dict) -> bool:
        """Validate a single speaker prediction has required fields and sufficient confidence."""
        if not isinstance(pred, dict):
            return False

        required_fields = ["speaker_label", "predicted_name", "confidence"]
        if not all(field in pred for field in required_fields):
            logger.warning(f"Skipping prediction with missing fields: {pred}")
            return False

        confidence = pred.get("confidence", 0.0)
        return isinstance(confidence, (int, float)) and confidence >= 0.5

    def _parse_speaker_identification_response(self, response: LLMResponse) -> dict:
        """Parse and validate speaker identification LLM response."""
        content = self._extract_json_from_response(response.content.strip())

        try:
            result = json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM identification response as JSON: {e}")
            logger.error(f"Raw response content: {response.content[:500]}...")
            return {"speaker_predictions": [], "error": f"Invalid JSON response: {str(e)}"}

        if not isinstance(result, dict):
            logger.error("LLM response is not a valid JSON object")
            return {
                "speaker_predictions": [],
                "error": "Invalid response format - not a JSON object",
            }

        if "speaker_predictions" not in result:
            logger.error("LLM response missing required 'speaker_predictions' field")
            return {
                "speaker_predictions": [],
                "error": "Invalid response format - missing speaker_predictions",
            }

        predictions = result["speaker_predictions"]
        if not isinstance(predictions, list):
            logger.error("speaker_predictions is not a list")
            return {
                "speaker_predictions": [],
                "error": "Invalid response format - speaker_predictions must be a list",
            }

        valid_predictions = [p for p in predictions if self._validate_speaker_prediction(p)]

        logger.info(
            f"Speaker identification completed: {len(valid_predictions)} valid predictions from {len(predictions)} total"
        )

        return {
            "speaker_predictions": valid_predictions,
            "overall_confidence": result.get("overall_confidence", "unknown"),
            "analysis_notes": result.get("analysis_notes", "No additional notes provided"),
        }

    def identify_speakers(
        self,
        transcript: str,
        speaker_segments: list,
        known_speakers: list,
        output_language: str = "en",
        metadata_context: str = "",
    ) -> dict:
        """
        Use LLM to suggest speaker identifications based on contextual analysis of speech patterns,
        conversation content, and known speaker profiles.

        Args:
            transcript: Full transcript text with speaker labels
            speaker_segments: List of speaker segments with metadata including timestamps and text
            known_speakers: List of known speaker profiles with names and descriptions
            output_language: Language code for output reasoning (default: "en")
            metadata_context: File metadata (title, author, description, tags) for extra context

        Returns:
            Dictionary containing speaker predictions with confidence scores and reasoning
        """
        try:
            # Build language instruction for non-English output
            output_language_name = LLM_OUTPUT_LANGUAGES.get(output_language, "English")
            if output_language_name != "English":
                language_instruction = (
                    f"\n\nIMPORTANT: Generate all reasoning, analysis_notes, and explanations "
                    f"in {output_language_name}. Speaker names should remain as identified "
                    f"(names are language-agnostic), but all descriptive text must be in {output_language_name}."
                )
            else:
                language_instruction = ""

            system_prompt = f"""You are an expert linguist and conversation analyst specializing in speaker identification. Your task is to analyze transcripts and identify speakers based on multiple contextual clues.{language_instruction}

ANALYSIS METHODOLOGY:
1. Speech Patterns & Style:
   - Vocabulary complexity and professional terminology
   - Sentence structure and communication style
   - Use of technical jargon, industry-specific language
   - Formal vs. informal speech patterns

2. Content Analysis:
   - Topics of expertise and knowledge domains
   - Professional roles and responsibilities mentioned
   - Personal anecdotes or experiences shared
   - Areas where speakers demonstrate authority or deep knowledge

3. Conversational Dynamics:
   - Who asks questions vs. provides answers
   - Leadership patterns and decision-making roles
   - Deference patterns between speakers
   - Introduction patterns and name mentions

4. Context Clues:
   - Direct name mentions in conversation
   - Role references ("as the CEO", "from engineering", etc.)
   - Historical context from previous conversations
   - Cross-references to known speaker profiles
   - File metadata (title, description, author, tags) may contain speaker names or roles
   - METADATA SPEAKER HINTS (if provided) contain names extracted from metadata with roles and confidence scores
     * Hosts typically speak first, introduce guests, and ask questions
     * Guests are introduced, answer questions, and demonstrate focused expertise
     * Channel/uploader name often corresponds to the primary host
     * Higher-confidence hints should carry more weight in your analysis

CONFIDENCE SCORING:
- 0.9-1.0: Multiple strong indicators align (name mentioned + role + speech pattern match)
- 0.7-0.89: Strong contextual match with known profile (expertise area + communication style)
- 0.5-0.69: Moderate confidence based on partial indicators
- Below 0.5: Insufficient evidence for reliable identification

Only provide predictions with confidence >= 0.5. Explain your reasoning clearly for each identification."""

            known_speakers_context = self._build_known_speakers_context(known_speakers)

            speaker_labels = list(
                set(
                    seg.get("speaker_label", "Unknown")
                    for seg in speaker_segments
                    if seg.get("speaker_label")
                )
            )

            # Build metadata section for additional context
            metadata_section = ""
            if metadata_context:
                # Split structured hints from raw metadata
                if "SPEAKER HINTS FROM METADATA:" in metadata_context:
                    parts = metadata_context.split("\nFile Title:", 1)
                    hints_section = parts[0]
                    raw_section = "File Title:" + parts[1] if len(parts) > 1 else ""
                    metadata_section = f"\n{hints_section}\n"
                    if raw_section:
                        metadata_section += f"\nFILE METADATA:\n{raw_section}\n"
                else:
                    metadata_section = f"\nFILE METADATA:\n{metadata_context}\n"

            reserved_tokens = (
                len(system_prompt) // 3
                + len(known_speakers_context) // 3
                + len(metadata_section) // 3
                + 2500
            )
            available_tokens = max(1000, self.user_context_window - reserved_tokens)
            transcript_content = self._truncate_transcript_for_speakers(
                transcript, available_tokens
            )

            user_prompt = f"""TRANSCRIPT TO ANALYZE:
{transcript_content}
{metadata_section}
CURRENT SPEAKER LABELS: {", ".join(speaker_labels)}
{known_speakers_context}

TASK:
Analyze this conversation transcript and identify each speaker label based on the methodology described. Look for patterns in:
- Speech complexity and professional vocabulary usage
- Areas of expertise demonstrated through conversation content
- Leadership and authority patterns in the discussion
- Any direct or indirect name mentions or role references
- Communication styles and interpersonal dynamics
- File metadata clues (title, description, author, tags)
- Metadata speaker hints (names extracted from metadata with roles and confidence scores)

For each speaker you can identify with reasonable confidence (>=0.5), provide a detailed analysis.

RESPONSE FORMAT (JSON):
{{
    "speaker_predictions": [
        {{
            "speaker_label": "SPEAKER_1",
            "predicted_name": "John Smith",
            "confidence": 0.85,
            "reasoning": "Detailed explanation of evidence including speech patterns, expertise areas, and specific quotes or behaviors that led to this identification",
            "evidence_types": ["speech_pattern", "expertise", "role_reference", "name_mention"]
        }}
    ],
    "overall_confidence": "high",
    "analysis_notes": "Brief summary of the identification process and any challenges encountered"
}}

IMPORTANT: Only include predictions with confidence >= 0.5. If you cannot confidently identify any speakers, return an empty predictions array."""

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
                {
                    "role": "assistant",
                    "content": "Let me identify the most relevant evidence for each speaker:\n\nRELEVANT QUOTES AND EVIDENCE:\n",
                },
            ]

            response_tokens = min(self.config.response_tokens, self.user_context_window // 4)
            response = self.chat_completion(
                messages=messages,
                max_tokens=response_tokens,
                temperature=0.2,
            )

            if not response or not response.content:
                logger.warning("LLM returned empty response for speaker identification")
                return {"speaker_predictions": [], "error": "No response from LLM"}

            return self._parse_speaker_identification_response(response)

        except Exception as e:
            logger.error(f"Speaker identification failed with error: {e}", exc_info=True)
            return {"speaker_predictions": [], "error": f"Identification process failed: {str(e)}"}

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with automatic cleanup."""
        self.close()

    def health_check(self) -> bool:
        """
        Quick health check using the models endpoint

        Returns:
            True if LLM is available, False otherwise
        """
        try:
            headers = self._get_headers()

            # Build models endpoint URL
            base_url = self.config.base_url.strip().rstrip("/") if self.config.base_url else None
            if not base_url:
                logger.info("Health check failed: No base URL configured")
                return False

            if base_url.endswith("/v1"):
                models_url = f"{base_url}/models"
            else:
                models_url = f"{base_url}/v1/models"

            logger.info(f"Health check using models endpoint: {models_url}")

            # The base URL is user-supplied config; refuse internal targets before
            # fetching (issue #284 A0.1) and PIN the checked address, so the request goes
            # to the address that was judged rather than to whatever the resolver answers
            # a second time (DNS rebinding).
            from app.core.config import settings as _settings
            from app.utils.url_validation import pinned_requests_session
            from app.utils.url_validation import resolve_pinned_target

            target, reason = resolve_pinned_target(
                models_url, allow_private=_settings.LLM_ALLOW_PRIVATE_ENDPOINTS
            )
            if target is None:
                logger.warning("Health check blocked for %s: %s", models_url, reason)
                return False

            # Use a short timeout with no retries for health checks — this must
            # not block a sync thread for 30+ seconds if the LLM server is down.
            # `allow_redirects=False`: the pin covers one hop, and a 302 to an internal
            # address would otherwise be followed with no check at all.
            with pinned_requests_session(target) as _session:
                response = _session.get(
                    target.url,
                    headers={**headers, **target.headers},
                    timeout=3,
                    allow_redirects=False,
                )
            logger.info(f"Health check response status: {response.status_code}")

            if response.status_code == 200:
                # Optionally verify our model is in the list
                try:
                    data = response.json()
                    if "data" in data:
                        model_ids = [model.get("id") for model in data["data"]]
                        if self.config.model in model_ids:
                            logger.info(f"Model {self.config.model} found in available models")
                            return True
                        else:
                            logger.warning(
                                f"Model {self.config.model} not found in available models: {model_ids}"
                            )
                            return False  # Model not available
                    return True
                except Exception as e:
                    logger.debug(f"Could not parse models response, but got 200: {e}")
                    return True  # Service is up even if we can't parse response
            else:
                logger.info(f"Models endpoint returned status {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"Health check failed for {self.config.provider}: {e}", exc_info=True)
            return False

    @staticmethod
    def create_from_settings(user_id: int | None = None) -> Optional["LLMService"]:
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
    def create_from_config_id(user_id: int, config_id: int) -> Optional["LLMService"]:
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
        from sqlalchemy import or_

        from app.db.base import SessionLocal
        from app.models.user_llm_settings import UserLLMSettings
        from app.utils.encryption import decrypt_api_key

        db = SessionLocal()
        try:
            user_settings = (
                db.query(UserLLMSettings)
                .filter(
                    UserLLMSettings.id == config_id,
                    or_(
                        UserLLMSettings.user_id == user_id,
                        UserLLMSettings.is_shared == True,  # noqa: E712
                    ),
                )
                .first()
            )
            if not user_settings:
                logger.warning(f"LLM config {config_id} not available to user {user_id}")
                return None

            api_key = None
            if user_settings.api_key:
                api_key = decrypt_api_key(str(user_settings.api_key))
                if not api_key:
                    logger.error(f"Failed to decrypt API key for LLM config {config_id}")
                    return None

            config = LLMConfig(
                provider=LLMProvider(user_settings.provider),
                model=str(user_settings.model_name),
                api_key=api_key,
                base_url=str(user_settings.base_url) if user_settings.base_url else None,
                max_tokens=int(user_settings.max_tokens),
                temperature=float(user_settings.temperature),
            )
            return LLMService(config)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Could not build LLMService from config {config_id}: {e}")
            return None
        finally:
            db.close()

    @staticmethod
    def create_from_user_settings(user_id: int) -> Optional["LLMService"]:
        """Create LLMService from user-specific database settings"""
        from app import models
        from app.db.base import SessionLocal
        from app.models.user_llm_settings import UserLLMSettings
        from app.utils.encryption import decrypt_api_key

        db = SessionLocal()
        try:
            # Get user's active LLM configuration
            active_config_setting = (
                db.query(models.UserSetting)
                .filter(
                    models.UserSetting.user_id == user_id,
                    models.UserSetting.setting_key == "active_llm_config_id",
                )
                .first()
            )

            if not active_config_setting or not active_config_setting.setting_value:
                logger.info(
                    f"No active LLM configuration for user {user_id}, checking system settings"
                )
                return LLMService.create_from_system_settings()

            # Get the active LLM configuration (own or shared)
            from sqlalchemy import or_

            active_config_id = int(active_config_setting.setting_value)
            user_settings = (
                db.query(UserLLMSettings)
                .filter(
                    UserLLMSettings.id == active_config_id,
                    or_(
                        UserLLMSettings.user_id == user_id,
                        UserLLMSettings.is_shared == True,  # noqa: E712
                    ),
                )
                .first()
            )

            if not user_settings:
                logger.warning(
                    f"Active LLM config {active_config_id} not found for user {user_id}, checking system settings"
                )
                return LLMService.create_from_system_settings()

            # Decrypt API key if present
            api_key = None
            if user_settings.api_key:
                api_key = decrypt_api_key(str(user_settings.api_key))
                if not api_key and user_settings.api_key:
                    logger.error(f"Failed to decrypt API key for user {user_id}")
                    return LLMService.create_from_system_settings()

            # Create config from user settings - USE ONLY USER'S MAX_TOKENS
            provider = LLMProvider(user_settings.provider)
            temperature_float = float(user_settings.temperature)

            config = LLMConfig(
                provider=provider,
                model=str(user_settings.model_name),
                api_key=api_key,
                base_url=str(user_settings.base_url) if user_settings.base_url else None,
                max_tokens=int(user_settings.max_tokens),  # USER'S CONTEXT WINDOW - NO INFERENCE
                temperature=temperature_float,
            )

            logger.info(
                f"Created LLMService for user {user_id}: {provider}/{user_settings.model_name}, user_context_window={user_settings.max_tokens}"
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
    def create_from_system_settings() -> Optional["LLMService"]:
        """Create LLMService from system settings"""
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

        try:
            config = LLMConfig(
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                max_tokens=32768,  # Conservative system default
                temperature=0.3,
            )

            logger.info(
                f"Created LLMService from system settings: {provider}/{model}, context_window={config.max_tokens}"
            )
            return LLMService(config)
        except Exception as e:
            logger.error(f"Failed to create LLMService from system settings: {e}")
            return None


# Context manager for proper cleanup
class LLMServiceContext:
    """Context manager for LLM service with proper cleanup"""

    def __init__(self, service: LLMService | None = None, user_id: int | None = None):
        self.service = service
        self.user_id = user_id
        self._created_service = service is None

    def __enter__(self) -> Optional["LLMService"]:
        if self.service is None:
            self.service = (
                LLMService.create_from_user_settings(self.user_id)
                if self.user_id
                else LLMService.create_from_system_settings()
            )
            if self.service is None:
                logger.info("LLM service is not available - no provider configured")
                return None
        return self.service

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.service and self._created_service:
            self.service.close()


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
