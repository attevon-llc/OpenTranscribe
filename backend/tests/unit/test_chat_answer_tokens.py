"""Answer-budget resolution for a chat turn (issue #359).

The interesting property is precedence: a tenant cap must beat the user's
preference, and the context window must beat both, because exceeding it is a
provider error rather than a policy choice.
"""

from __future__ import annotations

import pytest

from app.services.chat.service import MIN_ANSWER_TOKENS
from app.services.chat.service import resolve_answer_tokens

WINDOW = 32_000
DEFAULT = 8_000


def resolve(
    *,
    requested: int | None = None,
    tenant_ceiling: int | None = None,
    default_tokens: int = DEFAULT,
    context_window: int = WINDOW,
) -> int:
    """Named passthrough so each test states only the limit it cares about."""
    return resolve_answer_tokens(
        requested=requested,
        tenant_ceiling=tenant_ceiling,
        default_tokens=default_tokens,
        context_window=context_window,
    )


def test_unset_uses_the_config_default():
    assert resolve() == DEFAULT


def test_request_is_honoured_within_limits():
    assert resolve(requested=12_000) == 12_000


def test_tenant_ceiling_beats_a_larger_request():
    assert resolve(requested=12_000, tenant_ceiling=4_000) == 4_000


def test_tenant_ceiling_does_not_raise_a_smaller_request():
    """A cap is a ceiling, not a target — asking for less must stay less."""
    assert resolve(requested=1_000, tenant_ceiling=4_000) == 1_000


def test_tenant_ceiling_applies_when_nothing_was_requested():
    assert resolve(tenant_ceiling=2_000) == 2_000


def test_context_window_caps_an_oversized_request():
    """Half the window: prompt and history must fit in the same budget."""
    assert resolve(requested=999_999) == WINDOW // 2


def test_context_window_caps_even_a_generous_tenant_ceiling():
    assert resolve(requested=999_999, tenant_ceiling=999_999) == WINDOW // 2


def test_floor_prevents_a_useless_budget():
    """A tiny window must not produce a reply cut off mid-sentence."""
    assert resolve(requested=1, context_window=100) == MIN_ANSWER_TOKENS


@pytest.mark.parametrize("window", [1_000, 8_000, 128_000, 1_000_000])
def test_result_is_always_positive_and_within_the_window(window):
    tokens = resolve(requested=999_999, context_window=window)
    assert tokens >= MIN_ANSWER_TOKENS
    assert tokens <= max(MIN_ANSWER_TOKENS, window)
