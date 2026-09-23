"""Unit tests for effective-config resolution: user prefs ∪ admin-forced floor.

The DB loaders are monkeypatched so these run without a database. Verifies that a
user cannot disable an admin-forced category and that export/LLM locks propagate.
"""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.core.tenant_limits import RedactionFloor
from app.core.tenant_limits import reset_resolvers
from app.core.tenant_limits import set_redaction_floor_resolver
from app.services.redaction import config as cfgmod
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.config import resolve_effective_config

# The DB loaders are monkeypatched in every test, so the session is never used.
_DB = cast(Session, None)


@pytest.fixture(autouse=True)
def _reset_tenant_resolvers():
    """The floor resolver is process-global; leaking one poisons every later test."""
    reset_resolvers()
    yield
    reset_resolvers()


def _patch(monkeypatch, user_prefs: dict, admin: dict):
    monkeypatch.setattr(cfgmod, "_load_user_prefs", lambda db, uid: user_prefs)
    monkeypatch.setattr(cfgmod, "_load_admin_policy", lambda db: admin)


_EMPTY_ADMIN = {
    "forced_categories": set(),
    "forced_pii_entities": set(),
    "forced_custom_words": [],
    "force_toxicity_threshold": 0.5,
    "force_export_redacted": False,
    "force_redact_before_llm": False,
}


def test_defaults_when_nothing_set(monkeypatch):
    # Redaction is opt-out by default: nothing set → disabled, no categories masked.
    _patch(monkeypatch, {}, dict(_EMPTY_ADMIN))
    cfg = resolve_effective_config(_DB, 1)
    assert cfg.enabled is False
    assert cfg.enabled_categories == set()
    assert cfg.locked_categories == set()


def test_user_opts_in(monkeypatch):
    _patch(monkeypatch, {"redaction_enabled": "true"}, dict(_EMPTY_ADMIN))
    cfg = resolve_effective_config(_DB, 1)
    assert cfg.enabled is True
    # Default categories mask language, not identities: PII is deliberately
    # opt-in (every "[NAME]" interrupts reading conversational transcripts).
    assert cfg.enabled_categories == {"profanity", "toxicity", "custom"}
    assert "pii" not in cfg.enabled_categories


def test_user_opts_into_pii(monkeypatch):
    _patch(
        monkeypatch,
        {
            "redaction_enabled": "true",
            "redaction_categories": '["profanity", "toxicity", "custom", "pii"]',
        },
        dict(_EMPTY_ADMIN),
    )
    cfg = resolve_effective_config(_DB, 1)
    assert cfg.enabled is True
    assert "pii" in cfg.enabled_categories


def test_user_disables_redaction(monkeypatch):
    _patch(monkeypatch, {"redaction_enabled": "false"}, dict(_EMPTY_ADMIN))
    cfg = resolve_effective_config(_DB, 1)
    assert cfg.enabled is False
    assert cfg.enabled_categories == set()


def test_admin_force_overrides_user_disable(monkeypatch):
    """User turned redaction off, but admin forces PII → PII still enforced + locked."""
    admin = dict(_EMPTY_ADMIN)
    admin["forced_categories"] = {"pii"}
    _patch(monkeypatch, {"redaction_enabled": "false"}, admin)
    cfg = resolve_effective_config(_DB, 1)
    assert cfg.enabled is True
    assert "pii" in cfg.enabled_categories
    assert "pii" in cfg.locked_categories
    assert "pii" in cfg.detectors


def test_user_cannot_reveal_forced_category(monkeypatch):
    admin = dict(_EMPTY_ADMIN)
    admin["forced_categories"] = {"pii"}
    _patch(monkeypatch, {"redaction_categories": '["pii", "profanity"]'}, admin)
    cfg = resolve_effective_config(_DB, 1)
    reveal = cfg.reveal_categories(requested=True, is_owner=True)
    assert "pii" not in reveal  # locked
    assert "profanity" in reveal  # non-forced, owner can reveal


def test_export_and_llm_locks_propagate(monkeypatch):
    admin = dict(_EMPTY_ADMIN)
    admin["force_export_redacted"] = True
    admin["force_redact_before_llm"] = True
    _patch(monkeypatch, {"redaction_default_export_redacted": "false"}, admin)
    cfg = resolve_effective_config(_DB, 1)
    assert cfg.export_redacted is True
    assert cfg.export_locked is True
    assert cfg.redact_before_llm is True


def test_non_owner_never_reveals():
    cfg = EffectiveRedactionConfig(
        enabled=True, enabled_categories={"pii"}, locked_categories=set()
    )
    assert cfg.reveal_categories(requested=True, is_owner=False) == set()


def test_forced_custom_words_merged(monkeypatch):
    admin = dict(_EMPTY_ADMIN)
    admin["forced_custom_words"] = ["ProjectX"]
    _patch(monkeypatch, {"redaction_custom_words": '["mine"]'}, admin)
    cfg = resolve_effective_config(_DB, 1)
    assert "ProjectX" in cfg.custom_words and "mine" in cfg.custom_words


class TestTenantRedactionFloor:
    """The per-org floor seam (issue #982) — a THIRD input, and a strictly additive one.

    ``core.tenant_limits.resolve_redaction_floor`` is unioned into the global
    ``redaction.force_*`` policy at resolve time. Community registers no resolver,
    so every assertion above still describes a self-host deployment exactly.
    """

    def _register(self, floor_for: dict[int | None, RedactionFloor]) -> list[int | None]:
        """Register a floor resolver keyed by org id; returns the list of org ids it saw."""
        seen: list[int | None] = []

        def _resolver(db, org_id):
            seen.append(org_id)
            return floor_for.get(org_id)

        set_redaction_floor_resolver(_resolver)
        return seen

    def test_a_resolver_that_returns_none_changes_nothing(self, monkeypatch):
        """The community path: same inputs, same config, resolver registered or not."""
        _patch(monkeypatch, {"redaction_enabled": "true"}, dict(_EMPTY_ADMIN))
        baseline = resolve_effective_config(_DB, 1)

        self._register({})  # registered, but returns None for every org
        assert resolve_effective_config(_DB, 1, organization_id=42) == baseline

    def test_tenant_floor_forces_a_category_the_global_floor_does_not(self, monkeypatch):
        """The whole point: one org gets PII forced without touching the deployment."""
        _patch(monkeypatch, {"redaction_enabled": "false"}, dict(_EMPTY_ADMIN))
        self._register({42: RedactionFloor(forced_categories=frozenset({"pii"}))})

        cfg = resolve_effective_config(_DB, 1, organization_id=42)
        # Forced beats the user's own opt-out, exactly as the global floor does.
        assert cfg.enabled is True
        assert "pii" in cfg.enabled_categories
        assert "pii" in cfg.locked_categories
        assert "pii" in cfg.detectors
        # Locked by the tenant floor, so even the owner cannot reveal it.
        assert "pii" not in cfg.reveal_categories(requested=True, is_owner=True)

    def test_floor_applies_only_to_the_organization_it_was_resolved_for(self, monkeypatch):
        _patch(monkeypatch, {"redaction_enabled": "false"}, dict(_EMPTY_ADMIN))
        seen = self._register({42: RedactionFloor(forced_categories=frozenset({"pii"}))})

        assert resolve_effective_config(_DB, 1, organization_id=7).enabled is False
        # Personal scope — and every existing two-argument caller — resolves org None.
        assert resolve_effective_config(_DB, 1).enabled is False
        assert seen == [7, None]

    def test_tenant_floor_can_only_add_never_relax(self, monkeypatch):
        """An empty tenant floor must not unset anything the operator mandated."""
        admin = dict(_EMPTY_ADMIN)
        admin["forced_categories"] = {"profanity"}
        admin["forced_pii_entities"] = {"US_SSN"}
        admin["forced_custom_words"] = ["ProjectX"]
        admin["force_export_redacted"] = True
        admin["force_redact_before_llm"] = True
        _patch(monkeypatch, {}, admin)
        self._register({42: RedactionFloor()})  # says nothing about anything

        cfg = resolve_effective_config(_DB, 1, organization_id=42)
        assert cfg.locked_categories == {"profanity"}
        assert "US_SSN" in cfg.pii_entities
        assert "ProjectX" in cfg.custom_words
        assert cfg.export_locked is True
        assert cfg.redact_before_llm_locked is True

    def test_floors_union_rather_than_replace(self, monkeypatch):
        admin = dict(_EMPTY_ADMIN)
        admin["forced_categories"] = {"profanity"}
        admin["forced_pii_entities"] = {"US_SSN"}
        admin["forced_custom_words"] = ["ProjectX"]
        _patch(monkeypatch, {}, admin)
        self._register(
            {
                42: RedactionFloor(
                    forced_categories=frozenset({"pii"}),
                    forced_pii_entities=frozenset({"EMAIL_ADDRESS"}),
                    forced_custom_words=("acquisition",),
                    force_export_redacted=True,
                    force_redact_before_llm=True,
                )
            }
        )

        cfg = resolve_effective_config(_DB, 1, organization_id=42)
        assert cfg.locked_categories == {"profanity", "pii"}
        assert {"US_SSN", "EMAIL_ADDRESS"} <= cfg.pii_entities
        assert {"ProjectX", "acquisition"} <= set(cfg.custom_words)
        assert cfg.export_locked is True
        assert cfg.redact_before_llm_locked is True

    def test_toxicity_threshold_takes_the_more_sensitive_of_the_two(self, monkeypatch):
        """A LOWER threshold flags more text, so the tenant's 0.2 must beat the global 0.5."""
        admin = dict(_EMPTY_ADMIN)
        admin["forced_categories"] = {"toxicity"}
        admin["force_toxicity_threshold"] = 0.5
        _patch(monkeypatch, {"redaction_toxicity_threshold": "0.9"}, admin)
        self._register(
            {
                42: RedactionFloor(
                    forced_categories=frozenset({"toxicity"}), force_toxicity_threshold=0.2
                )
            }
        )

        assert resolve_effective_config(_DB, 1, organization_id=42).toxicity_threshold == 0.2

    def test_a_less_sensitive_tenant_threshold_never_loosens_the_global_one(self, monkeypatch):
        admin = dict(_EMPTY_ADMIN)
        admin["forced_categories"] = {"toxicity"}
        admin["force_toxicity_threshold"] = 0.3
        _patch(monkeypatch, {"redaction_toxicity_threshold": "0.9"}, admin)
        self._register({42: RedactionFloor(force_toxicity_threshold=0.8)})

        assert resolve_effective_config(_DB, 1, organization_id=42).toxicity_threshold == 0.3

    def test_a_broken_resolver_degrades_to_the_global_floor_not_to_nothing(self, monkeypatch):
        admin = dict(_EMPTY_ADMIN)
        admin["forced_categories"] = {"pii"}
        _patch(monkeypatch, {"redaction_enabled": "false"}, admin)

        def _boom(db, org_id):
            raise RuntimeError("billing lookup exploded")

        set_redaction_floor_resolver(_boom)

        cfg = resolve_effective_config(_DB, 1, organization_id=42)
        # Still masking — the deployment's own floor is unaffected by the failure.
        assert cfg.enabled is True
        assert "pii" in cfg.locked_categories
