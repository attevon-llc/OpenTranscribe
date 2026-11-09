"""
LLM Service for OpenTranscribe

Provides unified interface for multiple LLM providers using synchronous HTTP requests.
Designed specifically for Celery tasks - no asyncio conflicts.
"""

import logging
import threading
from contextlib import ExitStack
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Any
from typing import Self

import requests
from urllib3.util.retry import Retry

from app.services.llm_chunking import LLMChunkingMixin
from app.services.llm_factory import LLMFactoryMixin
from app.services.llm_health import LLMHealthMixin
from app.services.llm_http_stream import LLMHttpStreamMixin
from app.services.llm_json_extract import LLMJsonExtractMixin
from app.services.llm_metering import LLMMeteringMixin
from app.services.llm_payloads import LLMPayloadMixin
from app.services.llm_speaker_id import LLMSpeakerIdMixin
from app.services.llm_summarization import LLMSummarizationMixin
from app.services.llm_transport import LLMTransportMixin
from app.services.llm_types import LLMConfig
from app.services.llm_types import LLMProvider
from app.services.llm_types import LLMResponse

if TYPE_CHECKING:  # pragma: no cover - import cost is paid only by type checkers
    from app.services.llm_usage import CallUsage
    from app.utils.url_validation import PinnedTarget

logger = logging.getLogger(__name__)


class LLMConfigFailure(StrEnum):
    """Why a user's ACTIVE LLM configuration cannot be used (never the "no config" case)."""

    BAD_POINTER = "bad_pointer"
    NOT_FOUND = "not_found"
    UNKNOWN_PROVIDER = "unknown_provider"
    CREDENTIALS = "credentials"
    BEDROCK_REGION = "bedrock_region"
    INVALID = "invalid"


_LLM_CONFIG_FAILURE_MESSAGES = {
    LLMConfigFailure.BAD_POINTER: "Your selected LLM configuration is invalid.",
    LLMConfigFailure.NOT_FOUND: "Your selected LLM configuration is no longer available.",
    LLMConfigFailure.UNKNOWN_PROVIDER: "Your selected LLM configuration uses an unsupported provider.",
    LLMConfigFailure.CREDENTIALS: "Your selected LLM configuration's credentials cannot be used.",
    LLMConfigFailure.BEDROCK_REGION: "Your selected LLM configuration has no AWS region.",
    LLMConfigFailure.INVALID: "Your selected LLM configuration is incomplete or invalid.",
}


class LLMConfigurationError(Exception):
    """A user HAS an active LLM configuration, but it cannot be used.

    Deliberately distinct from "no configuration": the latter falls back to the system
    provider, this must NOT. Callers decided transcript masking for the user's (possibly
    local) provider, so substituting the system provider -- possibly a cloud API -- would
    send unmasked text off the machine. ``str()`` is a fixed sentence per reason and is safe
    to show a user; the underlying exception is logged, never carried.
    """

    def __init__(self, reason: LLMConfigFailure) -> None:
        self.reason = reason
        super().__init__(_LLM_CONFIG_FAILURE_MESSAGES[reason])

    @property
    def user_message(self) -> str:
        """The fixed, user-safe sentence for this failure (never a caught exception's text)."""
        return _LLM_CONFIG_FAILURE_MESSAGES[self.reason]


class LLMEndpointBlockedError(Exception):
    """The configured LLM endpoint is not a permitted outbound target.

    Raised *before* any connection is attempted, by the SSRF guard on the shared
    request path. Distinct from a network error so callers can tell "we refused to
    dial this" from "we dialled it and it failed".
    """


class LLMService(
    LLMTransportMixin,
    LLMFactoryMixin,
    LLMMeteringMixin,
    LLMHttpStreamMixin,
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
        self.budget_user_id: int | None = None  # see LLMMeteringMixin
        self._local: bool | None = None

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

    def chat_completion_raw(
        self, messages: list[dict[str, str]], *, timeout: int = 300, **kwargs: Any
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

    def _chat_completion_untracked(
        self, messages: list[dict[str, str]], call: "CallUsage", **kwargs: Any
    ) -> LLMResponse:
        """Provider dispatch for :meth:`chat_completion`; fills ``call`` with reported usage."""
        if self.config.provider == LLMProvider.BEDROCK:
            return self._bedrock_chat_completion(messages, call=call, **kwargs)

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
            from app.services.llm_usage import split_usage

            call.usage = split_usage(data)
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

    def _bedrock_chat_completion(
        self, messages: list[dict[str, str]], call: "CallUsage | None" = None, **kwargs: Any
    ) -> LLMResponse:
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

        usage_out: dict[str, int] = {}
        try:
            content, usage_tokens, finish_reason = converse(
                usage_out=usage_out,
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
        finally:
            if call is not None and usage_out:
                from app.services.llm_usage import Usage

                call.usage = Usage(
                    prompt_tokens=usage_out.get("prompt_tokens"),
                    completion_tokens=usage_out.get("completion_tokens"),
                    cache_read_tokens=usage_out.get("cache_read_tokens", 0),
                    cache_write_tokens=usage_out.get("cache_write_tokens", 0),
                )

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

    def close(self) -> None:
        """
        Close the session and clean up resources.

        Properly closes the HTTP session and releases any held connections.
        Should be called when the LLMService instance is no longer needed.
        """
        with self._session_lock:
            self._discard_pinned_session()
        logger.debug(f"Closed session for {self.config.provider}")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Context manager exit with automatic cleanup."""
        self.close()


# Utility function for quick LLM availability check
async def is_llm_available(user_id: int | None = None) -> bool:
    """Check, in a worker thread, that a usable LLM provider exists (fail-fast health check).

    Raises:
        SQLAlchemyError: settings unreadable; an outage, not "none configured".
    """
    import asyncio

    from sqlalchemy.exc import SQLAlchemyError

    def _check() -> bool:
        try:
            llm_service = LLMService.create_from_settings(user_id=user_id)
            if llm_service is None:
                return False
            health_ok = llm_service.health_check()
            llm_service.close()
            return health_ok
        except SQLAlchemyError:
            raise
        except Exception as e:
            logger.error(f"LLM availability check failed: {e}")
            return False

    return await asyncio.to_thread(_check)
