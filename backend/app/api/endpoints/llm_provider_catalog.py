"""Supported LLM provider catalog."""

from typing import Any

from fastapi import APIRouter

from app import schemas
from app.core.constants import BEDROCK_REGIONS
from app.core.constants import BEDROCK_SUGGESTED_MODELS
from app.core.constants import DEFAULT_BEDROCK_MODEL_ID

router = APIRouter()


def _get_provider_defaults() -> list[schemas.ProviderDefaults]:
    """Get default configurations for all supported providers"""
    return [
        schemas.ProviderDefaults(
            provider=schemas.LLMProvider.OPENAI,
            default_model="gpt-4o-mini",
            default_base_url="https://api.openai.com/v1",
            requires_api_key=True,
            supports_custom_url=True,
            max_context_length=128000,
            description="OpenAI's GPT models - reliable and well-supported",
        ),
        schemas.ProviderDefaults(
            provider=schemas.LLMProvider.VLLM,
            default_model="gpt-oss",
            default_base_url="http://localhost:8012/v1",
            requires_api_key=False,
            supports_custom_url=True,
            max_context_length=32768,
            description="vLLM server for local or custom model deployment",
        ),
        schemas.ProviderDefaults(
            provider=schemas.LLMProvider.OLLAMA,
            default_model="llama3.2:latest",
            default_base_url="http://localhost:11434",
            requires_api_key=False,
            supports_custom_url=True,
            max_context_length=128000,
            description="Ollama for local model deployment - uses native /api/chat endpoint",
        ),
        schemas.ProviderDefaults(
            provider=schemas.LLMProvider.ANTHROPIC,
            default_model="claude-opus-4-5-20251101",
            default_base_url="https://api.anthropic.com/v1",
            requires_api_key=True,
            supports_custom_url=False,
            max_context_length=200000,
            description="Anthropic's Claude models - excellent for analysis",
        ),
        schemas.ProviderDefaults(
            provider=schemas.LLMProvider.OPENROUTER,
            default_model="anthropic/claude-3.5-haiku",
            default_base_url="https://openrouter.ai/api/v1",
            requires_api_key=True,
            supports_custom_url=False,
            max_context_length=200000,
            description="OpenRouter provides access to many model providers",
        ),
        schemas.ProviderDefaults(
            provider=schemas.LLMProvider.BEDROCK,
            default_model=DEFAULT_BEDROCK_MODEL_ID,
            default_base_url=None,
            requires_api_key=False,
            supports_custom_url=False,
            max_context_length=None,
            description=(
                "AWS Bedrock — Converse API access to Claude, Nova, Llama and Mistral "
                "models. Enter an AWS region and access key, or leave the key blank to use "
                "the server's own AWS credentials (an IAM role, profile or environment)."
            ),
            suggested_regions=list(BEDROCK_REGIONS),
            suggested_models=list(BEDROCK_SUGGESTED_MODELS),
        ),
        # Issue #839: this catalog is the source the admin UI's provider dropdown
        # renders from (GET /llm-settings/providers -> LLMSettings.svelte ->
        # LLMConfigModal's <select>). CUSTOM was a fully translated, fully
        # implemented provider (llm_service.py, is_local_provider, docker-compose
        # overlay docs telling operators to select it) that this catalog simply
        # never listed, making it unreachable from the UI it was built for.
        schemas.ProviderDefaults(
            provider=schemas.LLMProvider.CUSTOM,
            default_model="",
            default_base_url=None,
            requires_api_key=False,
            supports_custom_url=True,
            max_context_length=None,
            description=(
                "Any OpenAI-compatible endpoint not covered above — point it at a "
                "self-hosted or third-party server by base URL and model name."
            ),
        ),
    ]


@router.get("/providers", response_model=schemas.SupportedProvidersResponse)
def get_supported_providers() -> Any:
    """Get list of supported LLM providers with their default configurations.

    This is the canonical provider catalog (the former ``GET /api/llm/providers``
    handler was dead — it called a nonexistent ``LLMService`` method and always
    500'd — and has been removed).

    Deliberate posture: this route declares NO ``current_user`` dependency, so it
    is served WITHOUT authentication. The payload is non-sensitive static metadata
    (provider names, default models, capability flags) with no per-user or secret
    data, so an open catalog is intentional. Pinned by
    ``test_providers_no_auth_dependency`` so the auth posture can't change silently.
    """
    providers = _get_provider_defaults()
    return schemas.SupportedProvidersResponse(providers=providers)
