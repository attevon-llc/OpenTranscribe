"""Which request features each Claude model id accepts.

The expectations are the documented per-generation behaviour (Claude docs: "Migrating
away from prefilled responses", the Opus 5.5 / Sonnet 5.5 / Haiku 5.5 migration guides,
and the structured-outputs "Supported models" list), written out per id so a wrong
threshold in ``claude_capabilities`` fails on the exact model it misclassifies.
"""

import pytest

from app.services.llm_claude_capabilities import ClaudeCapabilities
from app.services.llm_claude_capabilities import claude_capabilities

LEGACY = ClaudeCapabilities(
    accepts_prefill=True, accepts_sampling_params=True, supports_structured_outputs=True
)
# Prefill gone, sampling still accepted: Opus 4.6 and Sonnet 4.6.
NO_PREFILL = ClaudeCapabilities(
    accepts_prefill=False, accepts_sampling_params=True, supports_structured_outputs=True
)
# Both gone.
MODERN = ClaudeCapabilities(
    accepts_prefill=False, accepts_sampling_params=False, supports_structured_outputs=True
)
SAFE_UNKNOWN = ClaudeCapabilities(
    accepts_prefill=False, accepts_sampling_params=False, supports_structured_outputs=False
)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # Still accept prefill and sampling.
        ("claude-haiku-4-5-20251001", LEGACY),
        ("claude-haiku-4-5", LEGACY),
        ("claude-sonnet-4-5-20250929", LEGACY),
        ("claude-opus-4-5-20251101", LEGACY),
        # Prefill rejected from 4.6, sampling still accepted on Opus 4.6 / Sonnet 4.6.
        ("claude-opus-4-6", NO_PREFILL),
        ("claude-sonnet-4-6", NO_PREFILL),
        # Sampling rejected from Opus 4.7 and from Sonnet 5 / Haiku 5.
        ("claude-opus-4-7", MODERN),
        ("claude-opus-4-8", MODERN),
        ("claude-opus-5", MODERN),
        ("claude-opus-5-5", MODERN),
        ("claude-sonnet-5", MODERN),
        ("claude-sonnet-5-5", MODERN),
        ("claude-haiku-5-5", MODERN),
        ("claude-fable-5-1", MODERN),
        ("claude-mythos-5", MODERN),
    ],
)
def test_documented_generations(model, expected):
    assert claude_capabilities(model) == expected


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-20250514",  # date suffix is not a minor version
        "claude-opus-4-1-20250805",
        "claude-3-5-sonnet-20241022",
        "claude-3-7-sonnet-latest",
        "claude-3-opus-20240229",
        "claude-3-5-haiku-20241022",
    ],
)
def test_pre_4_5_models_accept_prefill_and_sampling_but_not_structured_outputs(model):
    caps = claude_capabilities(model)
    assert caps.accepts_prefill and caps.accepts_sampling_params
    assert not caps.supports_structured_outputs


@pytest.mark.parametrize(
    ("bedrock_id", "expected"),
    [
        ("anthropic.claude-haiku-4-5-20251001-v1:0", LEGACY),
        ("us.anthropic.claude-sonnet-4-6-v1:0", NO_PREFILL),
        ("global.anthropic.claude-opus-5-v1:0", MODERN),
        (
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-sonnet-5-5-v1:0",
            MODERN,
        ),
    ],
)
def test_bedrock_ids_and_arns_resolve_to_the_same_table(bedrock_id, expected):
    assert claude_capabilities(bedrock_id) == expected


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-future",
        "claude-next-9",
        "claude-mythos-preview",
        "claude-2.1",
        "claude-instant-1.2",
    ],
)
def test_unrecognised_claude_ids_default_to_the_safe_behaviour(model):
    assert claude_capabilities(model) == SAFE_UNKNOWN


@pytest.mark.parametrize("model", ["test-model", "glm-4.6", "", None])
def test_non_claude_ids_behind_an_anthropic_endpoint_keep_the_permissive_behaviour(model):
    caps = claude_capabilities(model)
    assert caps.accepts_prefill and caps.accepts_sampling_params
    assert not caps.supports_structured_outputs
