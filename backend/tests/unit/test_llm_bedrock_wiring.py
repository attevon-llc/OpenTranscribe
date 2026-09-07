"""Configuration wiring for the Bedrock provider (issue #596).

``llm_bedrock.py`` implements the real Converse/ConverseStream call path (and
``test_llm_bedrock.py`` covers it thoroughly, including the boto3 request shape) — but
until this module, nothing let a deployment or a user actually SELECT Bedrock: no
``BEDROCK`` member on the user-settings schema enum, no branch in
``LLMService._get_provider_config`` (the system-settings resolver), and
``validate_connection`` fell through to the OpenAI-compatible "derive a /models URL"
branch, which has nothing to derive from for an SDK call.

These tests are the falsifiable core of the fix: each one is red against the
pre-wiring code for a distinct reason (`ValueError: 'bedrock' is not a valid
LLMProvider` for the schema enum, ``None`` for the config resolver, a `KeyError`/
"No endpoint configured" for the connection test) and green here.

None of this needs a live AWS account: the schema/config-resolution tests need
nothing at all, and the ones that reach the runtime call path mock ``boto3`` exactly
like ``test_llm_bedrock.py`` does — proving the WIRING reaches the real boto3 request
shape, not that AWS accepts it.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.schemas.llm_settings import LLMProvider as SchemaLLMProvider
from app.schemas.llm_settings import UserLLMSettingsCreate
from app.services.llm_service import PROVIDERS_REQUIRING_API_KEY
from app.services.llm_service import SDK_PROVIDERS
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService

MODEL = "anthropic.claude-haiku-4-5-20251001-v1:0"


# --------------------------------------------------------------------------
# Schema: BEDROCK is a real, creatable provider on the user-settings wire contract
# --------------------------------------------------------------------------


def test_schema_enum_has_a_bedrock_member():
    assert SchemaLLMProvider.BEDROCK.value == "bedrock"


def test_a_bedrock_user_config_validates_with_no_api_key_or_base_url():
    """The write contract `create_user_llm_configuration` (POST /llm-settings) validates
    against. Bedrock has neither an API key nor a base URL, unlike every HTTP provider.
    """
    config = UserLLMSettingsCreate(
        name="My Bedrock Config",
        provider=SchemaLLMProvider.BEDROCK,
        model_name=MODEL,
    )
    assert config.provider == SchemaLLMProvider.BEDROCK
    assert config.api_key is None
    assert config.base_url is None


def test_bedrock_is_excluded_from_providers_requiring_an_api_key():
    """Pins the existing exclusion (PROVIDERS_REQUIRING_API_KEY's docstring already
    names Bedrock) so a future edit cannot silently start demanding a key that has
    nowhere to be stored and nothing to authenticate.
    """
    assert LLMProvider.BEDROCK not in PROVIDERS_REQUIRING_API_KEY


def test_bedrock_is_an_sdk_provider():
    assert LLMProvider.BEDROCK in SDK_PROVIDERS


# --------------------------------------------------------------------------
# `_get_provider_config` — the system-settings resolver's new Bedrock branch
# --------------------------------------------------------------------------


def test_get_provider_config_returns_the_model_when_region_and_model_are_set(monkeypatch):
    monkeypatch.setattr(settings, "BEDROCK_MODEL_NAME", MODEL)
    monkeypatch.setattr(settings, "BEDROCK_REGION", "us-east-1")

    result = LLMService._get_provider_config(LLMProvider.BEDROCK)

    assert result == (MODEL, None, None)


def test_get_provider_config_is_none_without_a_region(monkeypatch):
    """A model with no region is not a usable Bedrock config: `_client` in
    `llm_bedrock.py` raises `BedrockNotConfiguredError` without one. Treating this as
    "not configured" here (rather than letting it fail at call time) is what
    `create_from_system_settings` needs to fall through the same way every other
    unconfigured provider does.
    """
    monkeypatch.setattr(settings, "BEDROCK_MODEL_NAME", MODEL)
    monkeypatch.setattr(settings, "BEDROCK_REGION", "")

    assert LLMService._get_provider_config(LLMProvider.BEDROCK) is None


def test_get_provider_config_is_none_without_a_model(monkeypatch):
    monkeypatch.setattr(settings, "BEDROCK_MODEL_NAME", "")
    monkeypatch.setattr(settings, "BEDROCK_REGION", "us-east-1")

    assert LLMService._get_provider_config(LLMProvider.BEDROCK) is None


# --------------------------------------------------------------------------
# `create_from_system_settings` — the end-to-end system-config dispatch
# --------------------------------------------------------------------------


def test_create_from_system_settings_builds_a_bedrock_service(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "bedrock")
    monkeypatch.setattr(settings, "BEDROCK_MODEL_NAME", MODEL)
    monkeypatch.setattr(settings, "BEDROCK_REGION", "us-east-1")

    service = LLMService.create_from_system_settings()

    assert service is not None
    assert service.config.provider == LLMProvider.BEDROCK
    assert service.config.model == MODEL
    assert service.config.api_key is None


def test_create_from_system_settings_is_none_when_bedrock_is_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "bedrock")
    monkeypatch.setattr(settings, "BEDROCK_MODEL_NAME", MODEL)
    monkeypatch.setattr(settings, "BEDROCK_REGION", "")

    assert LLMService.create_from_system_settings() is None


# --------------------------------------------------------------------------
# `LLMService.__init__` — the endpoints dict must not KeyError for Bedrock
# --------------------------------------------------------------------------


def test_endpoints_dict_carries_a_descriptive_entry_for_bedrock(monkeypatch):
    """Before this fix, `LLMProvider.BEDROCK` was entirely absent from `self.endpoints`
    — not even mapped to `None` — so `llm_service.endpoints[service_provider]` (the
    plain subscript in the `/llm-settings/test` endpoint) raised `KeyError`, and
    `validate_connection`'s init-time log line silently read as `endpoint=None`.
    """
    monkeypatch.setattr(settings, "BEDROCK_REGION", "us-east-1")
    service = LLMService(LLMConfig(provider=LLMProvider.BEDROCK, model=MODEL))

    endpoint = service.endpoints[LLMProvider.BEDROCK]  # must not KeyError
    assert endpoint is not None
    assert "bedrock-runtime" in endpoint
    assert "us-east-1" in endpoint


# --------------------------------------------------------------------------
# `validate_connection` — Test Connection must reach the real boto3 call path
# --------------------------------------------------------------------------


def _fake_client(events):
    client = MagicMock()
    client.converse_stream.return_value = {"stream": iter(events)}
    return client


def test_validate_connection_succeeds_through_the_real_boto3_call_shape():
    """Before this fix, Bedrock fell into the OpenAI-compatible branch of
    `validate_connection`, which derives a `/models` URL from `self.endpoints.get(...)`
    — `None` for Bedrock — and returned `False, "No endpoint configured for bedrock"`
    unconditionally. Mocking only `llm_bedrock._client` (the boto3 client
    constructor, exactly as `test_llm_bedrock.py` does) proves this now drives the
    same Converse request `chat_completion` would use for a real turn.
    """
    service = LLMService(LLMConfig(provider=LLMProvider.BEDROCK, model=MODEL))
    events = [
        {"contentBlockDelta": {"delta": {"text": "Hello from Bedrock"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 3, "outputTokens": 4}}},
    ]

    with patch("app.services.llm_bedrock._client", return_value=_fake_client(events)):
        success, message = service.validate_connection()

    assert success is True
    assert "Hello from Bedrock" in message


def test_validate_connection_fails_on_a_bedrock_stream_error():
    service = LLMService(LLMConfig(provider=LLMProvider.BEDROCK, model=MODEL))
    events = [{"validationException": {"message": "The model ID is invalid"}}]

    with patch("app.services.llm_bedrock._client", return_value=_fake_client(events)):
        success, message = service.validate_connection()

    assert success is False
    assert "Connection failed" in message


def test_validate_connection_does_not_require_an_api_key_for_bedrock():
    """Bedrock is excluded from `PROVIDERS_REQUIRING_API_KEY`, so an empty api_key must
    not trip the local pre-flight guard that blocks OpenAI/Anthropic/OpenRouter with no
    key before any outbound call.
    """
    service = LLMService(LLMConfig(provider=LLMProvider.BEDROCK, model=MODEL, api_key=None))

    with patch(
        "app.services.llm_bedrock._client",
        return_value=_fake_client(
            [
                {"contentBlockDelta": {"delta": {"text": "ok"}}},
                {"messageStop": {"stopReason": "end_turn"}},
            ]
        ),
    ):
        success, message = service.validate_connection()

    assert success is True
    assert "An API key is required" not in message


# --------------------------------------------------------------------------
# `GET /llm-settings/providers` — the frontend's provider catalog
# --------------------------------------------------------------------------


def test_provider_defaults_include_bedrock_with_no_api_key_or_custom_url():
    from app.api.endpoints.llm_settings import _get_provider_defaults

    defaults = {d.provider: d for d in _get_provider_defaults()}

    assert SchemaLLMProvider.BEDROCK in defaults
    bedrock = defaults[SchemaLLMProvider.BEDROCK]
    assert bedrock.requires_api_key is False
    assert bedrock.supports_custom_url is False
    assert bedrock.default_base_url is None
    assert bedrock.default_model


@pytest.mark.parametrize("provider", list(SchemaLLMProvider))
def test_every_non_legacy_schema_provider_has_a_service_side_counterpart(provider):
    """`ServiceLLMProvider(schema_provider.value)` is how `/llm-settings/test` maps the
    wire enum onto the service enum (`llm_settings.py`'s `test_llm_connection`). A
    schema member with no matching service member would raise `ValueError` (surfaced
    as a 500) on the very first test — so the real assertion is that the round trip
    lands on the SAME string, not merely that construction returns something.
    """
    assert LLMProvider(provider.value).value == provider.value
