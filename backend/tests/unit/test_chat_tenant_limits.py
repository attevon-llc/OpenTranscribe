"""Per-tenant chat limits (cloud-edition seam).

The community edition must behave EXACTLY as it did before this seam existed —
no limits, no model restriction — and a registered resolver must only ever be
able to *tighten* what the operator already permits. Both directions are tested
here because getting either wrong is silent: a widened limit escapes the admin's
settings, and an accidentally-applied community limit breaks self-hosting.
"""

from __future__ import annotations

import pytest

from app.core.tenant_limits import TenantChatLimits
from app.core.tenant_limits import reset_resolvers
from app.core.tenant_limits import resolve_allowed_models
from app.core.tenant_limits import resolve_chat_limits
from app.core.tenant_limits import set_allowed_models_resolver
from app.core.tenant_limits import set_chat_limits_resolver
from app.services.chat.settings import ChatSettings
from app.services.chat.settings import apply_tenant_limits


@pytest.fixture(autouse=True)
def _restore_community_resolvers():
    """Resolvers are module-global; a leaked one would corrupt unrelated tests."""
    yield
    reset_resolvers()


# --------------------------------------------------------------------------
# Community edition: the seam must be invisible
# --------------------------------------------------------------------------


def test_community_edition_has_no_chat_limits():
    assert resolve_chat_limits(None) is None
    assert resolve_chat_limits(42) is None


def test_community_edition_restricts_no_models():
    """None means "no restriction" — distinct from an empty set."""
    assert resolve_allowed_models(None) is None


def test_settings_pass_through_untouched_without_a_resolver():
    base = ChatSettings(messages_per_hour=120, final_chunks=12)
    assert apply_tenant_limits(base, None) is base


# --------------------------------------------------------------------------
# A tenant limit narrows, never widens
# --------------------------------------------------------------------------


def test_tenant_limit_tightens_the_admin_value():
    set_chat_limits_resolver(lambda _org: TenantChatLimits(messages_per_hour=20))
    out = apply_tenant_limits(ChatSettings(messages_per_hour=120), org_id := 1)
    assert out.messages_per_hour == 20
    assert org_id == 1


def test_tenant_limit_cannot_raise_the_admin_value():
    """The direction that matters. A resolver able to widen a limit would let a
    tenant escape the operator's own settings.
    """
    set_chat_limits_resolver(lambda _org: TenantChatLimits(messages_per_hour=10_000))
    out = apply_tenant_limits(ChatSettings(messages_per_hour=120), 1)
    assert out.messages_per_hour == 120


def test_unset_dimensions_are_left_alone():
    """A partial override must not reset the dimensions it says nothing about."""
    set_chat_limits_resolver(lambda _org: TenantChatLimits(max_output_tokens=1024))
    out = apply_tenant_limits(ChatSettings(messages_per_hour=120, final_chunks=12), 1)
    assert out.messages_per_hour == 120
    assert out.final_chunks == 12
    assert out.max_output_tokens == 1024


def test_retrieved_chunks_can_be_capped():
    """Retrieved excerpts dominate input tokens in a RAG chat, so this bounds cost
    at least as much as capping the answer does.
    """
    set_chat_limits_resolver(lambda _org: TenantChatLimits(max_retrieved_chunks=4))
    out = apply_tenant_limits(ChatSettings(final_chunks=12), 1)
    assert out.final_chunks == 4


def test_a_failing_resolver_falls_back_to_the_admin_settings():
    """A broken resolver must not break chat."""

    def _boom(_org):
        raise RuntimeError("billing lookup failed")

    set_chat_limits_resolver(_boom)
    base = ChatSettings(messages_per_hour=120)
    assert apply_tenant_limits(base, 1).messages_per_hour == 120


# --------------------------------------------------------------------------
# Model allowlist
# --------------------------------------------------------------------------


def test_allowlist_is_returned_verbatim():
    set_allowed_models_resolver(lambda _org: {"claude-haiku-4-5"})
    assert resolve_allowed_models(1) == {"claude-haiku-4-5"}


def test_empty_allowlist_is_distinct_from_no_restriction():
    """An empty set means "no model permitted" — how a suspended tenant is
    expressed. Collapsing it to None would silently un-suspend them.
    """
    set_allowed_models_resolver(lambda _org: set())
    result = resolve_allowed_models(1)
    assert result == set()
    assert result is not None


def test_a_failing_allowlist_resolver_does_not_lock_everyone_out():
    """Failing open here is deliberate: a broken billing lookup should not deny
    every tenant the product. A deployment wanting fail-closed enforces it in its
    before-message hook, where the request can be rejected outright.
    """

    def _boom(_org):
        raise RuntimeError("billing lookup failed")

    set_allowed_models_resolver(_boom)
    assert resolve_allowed_models(1) is None


# --------------------------------------------------------------------------
# chat.ungrounded capability
# --------------------------------------------------------------------------


def test_ungrounded_chat_is_allowed_by_default():
    """Open-source and paid tiers alike. It has legitimate uses ("rewrite this
    summary more formally"), so it is not gated by default anywhere.
    """
    from types import SimpleNamespace
    from typing import Any
    from typing import cast

    from app.api.endpoints.chat.common import resolve_use_context

    conv = cast(Any, SimpleNamespace(settings={"use_context": False}))
    assert resolve_use_context(conv, {"use_context_default": True}) is False


def test_disabling_the_capability_degrades_to_grounded_rather_than_rejecting():
    """Withholding the feature must not break chat: the user still gets an answer,
    just one anchored to their own transcripts.
    """
    from types import SimpleNamespace
    from typing import Any
    from typing import cast

    from app.api.endpoints.chat.common import resolve_use_context
    from app.core.capabilities import reset_capability_resolver
    from app.core.capabilities import set_capability_resolver

    try:
        set_capability_resolver(lambda _req: {"chat.ungrounded": False})
        conv = cast(Any, SimpleNamespace(settings={"use_context": False}))
        assert resolve_use_context(conv, {"use_context_default": True}) is True
    finally:
        reset_capability_resolver()


def test_capability_does_not_disturb_grounded_chat():
    from types import SimpleNamespace
    from typing import Any
    from typing import cast

    from app.api.endpoints.chat.common import resolve_use_context
    from app.core.capabilities import reset_capability_resolver
    from app.core.capabilities import set_capability_resolver

    try:
        set_capability_resolver(lambda _req: {"chat.ungrounded": False})
        conv = cast(Any, SimpleNamespace(settings={"use_context": True}))
        assert resolve_use_context(conv, {"use_context_default": False}) is True
    finally:
        reset_capability_resolver()
