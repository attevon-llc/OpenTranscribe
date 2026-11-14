"""Summary masking never builds a detector model on a request thread; a worker may opt in."""

from __future__ import annotations

import sys
import types

import pytest

from app.core.redaction_config import EffectiveRedactionConfig
from app.services.redaction.detectors import pii_presidio
from app.services.redaction.detectors import toxicity


@pytest.fixture
def cold(monkeypatch):
    builds: list[str] = []
    monkeypatch.setattr(pii_presidio, "_analyzer", None)
    monkeypatch.setattr(pii_presidio, "_load_failed", False)
    monkeypatch.setattr(toxicity, "_pipes", {})
    monkeypatch.setattr(pii_presidio, "_build_analyzer", lambda _g: builds.append("pii"))
    monkeypatch.setitem(
        sys.modules, "transformers", types.SimpleNamespace(pipeline=lambda *a, **k: None)
    )
    return builds


def _cfg():
    return EffectiveRedactionConfig(enabled=True, enabled_categories={"pii"})


def test_summary_masking_is_guarded_by_default_and_worker_can_load(cold):
    from app.services.redaction.summary_redaction import SummaryMaskingUnavailableError
    from app.services.redaction.summary_redaction import mask_summary
    from app.services.redaction.summary_redaction import mask_summary_leaf

    with pytest.raises(SummaryMaskingUnavailableError):
        mask_summary_leaf("call 555-867-5309", _cfg())
    with pytest.raises(SummaryMaskingUnavailableError):
        mask_summary({"overview": "call 555-867-5309"}, _cfg())
    assert cold == []
    # A worker opts in explicitly: the loader is reached (the stub build yields no analyzer).
    with pytest.raises(SummaryMaskingUnavailableError):
        mask_summary_leaf("call 555-867-5309", _cfg(), allow_model_load=True)
    assert cold == ["pii"]
