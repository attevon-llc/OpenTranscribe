"""Provider enum and request/response dataclasses shared by the LLM service modules."""

from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum

from app.services.bedrock_settings import BedrockConnection


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
    #: Bedrock only: the configuration's own region and AWS credentials (None = the server's
    #: region and credential chain). Secrets are excluded from repr.
    bedrock: BedrockConnection | None = field(default=None, repr=False)
