"""User-level RAG preferences.

One property carries the whole feature: a preference may only ever TIGHTEN what
the admin permits. If it could widen, every platform cost control would be
advisory — a user could simply ask for more excerpts than the operator allows.
That is the same direction of travel as the per-tenant ceiling, applied after it.
"""

from __future__ import annotations

import pytest

from app.services.chat.settings import ChatSettings
from app.services.chat.settings import apply_user_preferences


def _admin(**overrides) -> ChatSettings:
    return ChatSettings(**{"final_chunks": 12, "rerank_enabled": True, **overrides})


def test_no_preference_inherits_the_admin_value():
    result = apply_user_preferences(_admin())
    assert result.final_chunks == 12
    assert result.rerank_enabled is True


def test_a_user_may_ask_for_fewer_excerpts():
    """Cheaper and faster, at some recall — their call to make."""
    assert apply_user_preferences(_admin(), final_chunks=4).final_chunks == 4


def test_a_user_may_not_ask_for_more_excerpts_than_the_admin_allows():
    """THE property. Excerpts dominate input tokens, so this is the cost control."""
    assert apply_user_preferences(_admin(), final_chunks=100).final_chunks == 12


@pytest.mark.parametrize("requested", [1, 11, 12])
def test_values_at_or_below_the_ceiling_pass_through(requested):
    assert apply_user_preferences(_admin(), final_chunks=requested).final_chunks == requested


def test_a_user_may_turn_reranking_off():
    assert apply_user_preferences(_admin(), rerank_enabled=False).rerank_enabled is False


def test_a_user_may_not_turn_reranking_on_when_the_admin_has_it_off():
    """One-way on purpose: the model may not be installed on this deployment."""
    admin_off = _admin(rerank_enabled=False)
    assert apply_user_preferences(admin_off, rerank_enabled=True).rerank_enabled is False


def test_preferences_do_not_disturb_unrelated_settings():
    base = _admin(messages_per_hour=99, candidate_pool=48)
    result = apply_user_preferences(base, final_chunks=3, rerank_enabled=False)
    assert result.messages_per_hour == 99
    assert result.candidate_pool == 48


def test_the_original_settings_object_is_not_mutated():
    """Settings are resolved per request; mutating in place would leak across them."""
    base = _admin()
    apply_user_preferences(base, final_chunks=2, rerank_enabled=False)
    assert base.final_chunks == 12
    assert base.rerank_enabled is True
