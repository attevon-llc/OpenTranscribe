"""The capability vocabulary is one contract shared by two codebases.

``app/core/capabilities.py`` declares the keys; ``SettingsModal.svelte`` gates
nav items on them. Nothing linked the two, and they drifted in opposite
directions: the frontend gated seven ``cap:`` keys the backend had never heard
of. They rendered only *because* they were undeclared —
``capability_enabled()`` fails **closed** for an unknown key while the store's
``isCapabilityEnabled()`` fails **open** — so declaring any of them as
``False``, or naming one in a cloud resolver, would have silently deleted those
admin panels with no other change.

This test is the link: every ``cap:`` string in the settings UI must be a
declared backend capability, and the two backend maps must agree on their key
sets so a new key can never ship unclassified.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.capabilities import CAPABILITY_AUDIENCE
from app.core.capabilities import COMMUNITY_CAPABILITIES

#: backend/tests/unit/<this file> -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
SETTINGS_MODAL = REPO_ROOT / "frontend" / "src" / "components" / "SettingsModal.svelte"

#: `{ id: 'x', label: ..., cap: 'watch_sources' }` — the sidebar item gate.
CAP_ATTR_RE = re.compile(r"\bcap:\s*'([^']+)'")

#: `capOn(capState, 'key')` / `orgAdminCapOn(capState, 'key')` — the same
#: vocabulary reached through a helper instead of a section-item attribute.
CAP_CALL_RE = re.compile(r"\b(?:capOn|orgAdminCapOn)\(\s*\w+\s*,\s*'([^']+)'\s*\)")


def _frontend_capability_keys() -> set[str]:
    """Capability keys referenced by the settings UI."""
    if not SETTINGS_MODAL.is_file():
        pytest.fail(f"SettingsModal.svelte not found at {SETTINGS_MODAL}")
    source = SETTINGS_MODAL.read_text(encoding="utf-8")
    return set(CAP_ATTR_RE.findall(source)) | set(CAP_CALL_RE.findall(source))


class TestCapabilityContract:
    def test_regex_finds_the_gates(self):
        """Guard the guard: a rename that breaks the parse must fail loudly
        rather than pass vacuously on an empty key set."""
        keys = _frontend_capability_keys()
        assert len(keys) >= 15, f"suspiciously few cap: gates parsed from SettingsModal: {keys}"
        # Spot-check one gate of each shape (attribute and helper call).
        assert "watch_sources" in keys
        assert "billing" in keys

    def test_frontend_gates_are_declared_capabilities(self):
        """Every `cap:` the UI gates on must exist in the backend map."""
        undeclared = _frontend_capability_keys() - set(COMMUNITY_CAPABILITIES)
        assert not undeclared, (
            "SettingsModal gates on capabilities the backend never declares: "
            f"{sorted(undeclared)}. They render today only because the frontend "
            "store fails open for unknown keys — declare them in "
            "COMMUNITY_CAPABILITIES + CAPABILITY_AUDIENCE or drop the gate."
        )

    def test_backend_maps_have_identical_key_sets(self):
        """Defaults and audiences are one vocabulary, not two."""
        caps = set(COMMUNITY_CAPABILITIES)
        audiences = set(CAPABILITY_AUDIENCE)
        assert not caps - audiences, f"capabilities missing an audience: {sorted(caps - audiences)}"
        assert not audiences - caps, (
            f"audiences for undeclared capabilities: {sorted(audiences - caps)}"
        )
