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


FRONTEND_SRC = REPO_ROOT / "frontend" / "src"

#: `isCapabilityEnabled($capabilities, 'key')` — a gate inside any component.
IS_ENABLED_RE = re.compile(r"\bisCapabilityEnabled\(\s*[$\w.]+\s*,\s*'([^']+)'\s*\)")

#: Deployment-locked controls (issue #1109). Each is enforced server-side, and
#: each must also be gated somewhere in the UI, or a deployment that turns it
#: off leaves users a control the server silently ignores.
DEPLOYMENT_LOCKED_KEYS = frozenset(
    {
        "url_ingest",
        "transcription.model_choice",
        "transcription.diarization_source",
        "transcription.advanced",
        "speaker_attributes.migration",
        "media_sources",
        "audio_extraction",
        "admin.flower",
    }
)


def _frontend_capability_keys() -> set[str]:
    """Capability keys referenced by the settings UI."""
    if not SETTINGS_MODAL.is_file():
        pytest.fail(f"SettingsModal.svelte not found at {SETTINGS_MODAL}")
    source = SETTINGS_MODAL.read_text(encoding="utf-8")
    return set(CAP_ATTR_RE.findall(source)) | set(CAP_CALL_RE.findall(source))


def _component_capability_keys() -> set[str]:
    """Capability keys gated through ``isCapabilityEnabled`` anywhere in the frontend."""
    keys: set[str] = set()
    for path in FRONTEND_SRC.rglob("*"):
        if path.suffix not in {".svelte", ".ts"} or path.name.endswith(".test.ts"):
            continue
        keys.update(IS_ENABLED_RE.findall(path.read_text(encoding="utf-8")))
    return keys


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

    def test_component_gates_are_declared_capabilities(self):
        """Same rule for gates outside the settings sidebar."""
        undeclared = _component_capability_keys() - set(COMMUNITY_CAPABILITIES)
        assert not undeclared, (
            f"frontend components gate on undeclared capabilities: {sorted(undeclared)}"
        )

    def test_deployment_locked_keys_are_declared_and_gated_in_the_ui(self):
        """Every deployment-locked key exists, defaults on, and has a UI gate."""
        assert set(COMMUNITY_CAPABILITIES) >= DEPLOYMENT_LOCKED_KEYS
        assert all(COMMUNITY_CAPABILITIES[k] is True for k in DEPLOYMENT_LOCKED_KEYS)
        ui_keys = _frontend_capability_keys() | _component_capability_keys()
        ungated = DEPLOYMENT_LOCKED_KEYS - ui_keys
        assert not ungated, f"deployment-locked keys with no frontend gate: {sorted(ungated)}"
