"""Provider enum, request/response dataclasses and the contract every ``LLMService`` mixin may assume.

The mixins (``llm_payloads``, ``llm_chunking``, ``llm_summarization``, ``llm_json_extract``,
``llm_speaker_id``, ``llm_health``) are mixed into :class:`~app.services.llm_service.LLMService`,
which sets the :class:`LLMHost` attributes in ``__init__`` and owns the transport methods.
Declaring them lets the type checker verify each mixin against the same contract; nothing in
``LLMHost`` exists at runtime beyond the annotations.
"""

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from app.core.enums import LLMProvider
from app.services.bedrock_settings import BedrockConnection

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
    #: Bedrock only: the configuration's own region and AWS credentials (None = the server's
    #: region and credential chain). Secrets are excluded from repr.
    bedrock: BedrockConnection | None = field(default=None, repr=False)


class LLMHost:
    """Contract a mixin may assume of the ``LLMService`` it is mixed into."""

    config: LLMConfig
    user_context_window: int
    response_tokens: int
    endpoints: dict[LLMProvider, str | None]

    if TYPE_CHECKING:

        def chat_completion(self, messages: list[dict[str, str]], **kwargs) -> LLMResponse: ...

        def _get_headers(self) -> dict[str, str]: ...
