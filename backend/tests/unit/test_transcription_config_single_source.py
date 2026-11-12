"""Transcription and engine defaults have one home each (issue #1206)."""

from __future__ import annotations

import ast
from pathlib import Path

from app.api.endpoints import engine_settings
from app.transcription import boundary_resolver
from app.transcription.engine.config import EngineConfig

TASKS_DIR = Path(__file__).resolve().parents[2] / "app" / "tasks"


def test_engine_settings_defaults_are_the_engine_config_defaults():
    cfg = EngineConfig()
    for field in (
        "transcriber_backend",
        "diarizer_backend",
        "boundary_acoustic_cosine_margin",
        "boundary_acoustic_max_word_dur",
    ):
        _env, default = engine_settings._ENV_DEFAULTS[field]
        assert default == getattr(cfg, field), field


def test_boundary_resolver_acoustic_defaults_are_the_engine_config_defaults():
    smoothing = boundary_resolver.BoundarySmoothingConfig()
    cfg = EngineConfig()
    assert smoothing.acoustic_cosine_margin == cfg.boundary_acoustic_cosine_margin
    assert smoothing.acoustic_max_word_dur == cfg.boundary_acoustic_max_word_dur


def _hardcoded_source_defaults(tree: ast.AST) -> list[int]:
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "diarization_source"
            and isinstance(node.args[1], ast.Constant)
        ):
            hits.append(node.lineno)
    return hits


def test_the_detector_fires_on_a_literal_default():
    assert _hardcoded_source_defaults(ast.parse('x.get("diarization_source", "provider")')) == [1]


def test_the_detector_stays_clean_on_a_named_default():
    tree = ast.parse('x.get("diarization_source", DEFAULT_DIARIZATION_SOURCE)')
    assert _hardcoded_source_defaults(tree) == []


def test_no_task_hardcodes_a_diarization_source_default():
    files = list(TASKS_DIR.rglob("*.py"))
    assert len(files) > 10
    offenders = {
        str(p.relative_to(TASKS_DIR)): lines
        for p in files
        if (lines := _hardcoded_source_defaults(ast.parse(p.read_text(encoding="utf-8"))))
    }
    assert not offenders, f"use DEFAULT_DIARIZATION_SOURCE: {offenders}"


def test_settings_has_no_unused_compute_type_property():
    from app.core.config import Settings

    assert not hasattr(Settings, "effective_compute_type")


def test_unwired_transcriber_backend_is_refused_with_422_before_any_write():
    from unittest.mock import MagicMock

    import pytest
    from fastapi import HTTPException
    from sqlalchemy.orm import Session

    from app import models

    body = engine_settings._EngineSettingsUpdate(transcriber_backend="whisperx")
    with pytest.raises(HTTPException) as caught:
        engine_settings.update_engine_settings(
            body, db=MagicMock(spec=Session), current_user=MagicMock(spec=models.User)
        )
    assert caught.value.status_code == 422
    assert "#366" in caught.value.detail
