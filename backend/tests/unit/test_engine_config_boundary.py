"""Guard the boundary-acoustic settings on EngineConfig (issue #193).

The acoustic re-check runs inside the GPU engine stage but is DB-controlled: the flags
ride on ``EngineConfig`` so the engine itself stays DB-free. These tests pin that wiring —
defaults, env resolution, and (critically) snapshot round-trip — so an engine refactor
that drops the fields fails loudly instead of silently disabling the feature.
"""

from __future__ import annotations

import pytest

from app.transcription.engine.config import EngineConfig


def test_acoustic_defaults_off() -> None:
    cfg = EngineConfig.from_environment(min_speakers=2, max_speakers=2)
    assert cfg.boundary_acoustic_recheck_enabled is False
    assert cfg.boundary_acoustic_cosine_margin == pytest.approx(0.05)
    assert cfg.boundary_acoustic_max_word_dur == pytest.approx(1.0)


def test_acoustic_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGINE_BOUNDARY_ACOUSTIC_RECHECK_ENABLED", "true")
    monkeypatch.setenv("ENGINE_BOUNDARY_ACOUSTIC_COSINE_MARGIN", "0.11")
    monkeypatch.setenv("ENGINE_BOUNDARY_ACOUSTIC_MAX_WORD_DUR", "0.7")
    cfg = EngineConfig.from_environment(min_speakers=2, max_speakers=2)
    assert cfg.boundary_acoustic_recheck_enabled is True
    assert cfg.boundary_acoustic_cosine_margin == pytest.approx(0.11)
    assert cfg.boundary_acoustic_max_word_dur == pytest.approx(0.7)


def test_acoustic_survives_snapshot_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The split / multi-GPU paths serialize config via to_snapshot/from_snapshot."""
    monkeypatch.setenv("ENGINE_BOUNDARY_ACOUSTIC_RECHECK_ENABLED", "true")
    monkeypatch.setenv("ENGINE_BOUNDARY_ACOUSTIC_COSINE_MARGIN", "0.09")
    monkeypatch.setenv("ENGINE_BOUNDARY_ACOUSTIC_MAX_WORD_DUR", "0.8")
    cfg = EngineConfig.from_environment(min_speakers=2, max_speakers=2)

    snap = cfg.to_snapshot()
    assert snap["boundary_acoustic_recheck_enabled"] is True
    assert snap["boundary_acoustic_cosine_margin"] == pytest.approx(0.09)

    restored = EngineConfig.from_snapshot(snap)
    assert restored.boundary_acoustic_recheck_enabled is True
    assert restored.boundary_acoustic_cosine_margin == pytest.approx(0.09)
    assert restored.boundary_acoustic_max_word_dur == pytest.approx(0.8)


def test_junk_acoustic_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGINE_BOUNDARY_ACOUSTIC_COSINE_MARGIN", "not-a-number")
    cfg = EngineConfig.from_environment(min_speakers=2, max_speakers=2)
    assert cfg.boundary_acoustic_cosine_margin == pytest.approx(0.05)
