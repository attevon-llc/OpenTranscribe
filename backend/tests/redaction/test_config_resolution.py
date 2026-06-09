"""Unit tests for effective-config resolution: user prefs ∪ admin-forced floor.

The DB loaders are monkeypatched so these run without a database. Verifies that a
user cannot disable an admin-forced category and that export/LLM locks propagate.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.orm import Session

from app.services.redaction import config as cfgmod
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.config import resolve_effective_config

# The DB loaders are monkeypatched in every test, so the session is never used.
_DB = cast(Session, None)


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
