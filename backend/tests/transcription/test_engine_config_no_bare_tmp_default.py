"""Issue #661 phase 1.4 — EngineConfig must never fall back to a bare "/tmp".

The dataclass default and ``from_snapshot``'s ``.get(..., "/tmp")`` fallback both predated
``ENGINE_SHARED_VOLUME_DEFAULT`` and were never updated when it was introduced (issue #661
E0). A bare "/tmp" is world-writable and never the shared-volume mount, so a snapshot missing
the key silently pointed the engine at a container-local scratch dir the sidecar cannot see.
"""

from __future__ import annotations

from app.core.constants import ENGINE_SHARED_VOLUME_DEFAULT
from app.transcription.engine.config import EngineConfig


def test_dataclass_default_is_not_bare_tmp():
    assert EngineConfig().shared_volume_path == ENGINE_SHARED_VOLUME_DEFAULT
    assert EngineConfig().shared_volume_path != "/tmp"  # noqa: S108 -- asserting NON-equality to a bare tmp path, not creating one


def test_from_snapshot_missing_key_falls_back_to_the_constant_not_bare_tmp():
    engine = EngineConfig.from_snapshot({})
    assert engine.shared_volume_path == ENGINE_SHARED_VOLUME_DEFAULT
    assert engine.shared_volume_path != "/tmp"  # noqa: S108 -- asserting NON-equality to a bare tmp path, not creating one
