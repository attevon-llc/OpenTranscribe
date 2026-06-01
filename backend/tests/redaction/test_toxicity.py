"""Toxicity detector tests.

Marked ``models`` and auto-skipped when transformers / the toxicity model weights are
unavailable. Pins score *ranges* (not exact values) so minor model updates don't break CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.models

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "redaction"


@pytest.fixture(scope="module")
def tox():
    pytest.importorskip("transformers")
    from app.services.redaction.detectors import toxicity

    if not toxicity.preload():
        pytest.skip("Toxicity model unavailable")
    return toxicity


@pytest.fixture(scope="module")
def segments() -> list[dict]:
    segs: list[dict] = json.loads((FIXTURES / "segments.json").read_text())
    return segs


def test_toxic_segment_flagged(tox, segments):
    """The insult/hate segment scores higher than a benign one."""
    toxic = tox.score_text(segments[4]["text"])  # "You are an idiot and I hate you people."
    benign = tox.score_text(segments[1]["text"])  # "My name is John Smith ..."
    assert toxic is not None and benign is not None
    assert toxic.get("toxic", 0.0) > benign.get("toxic", 0.0)


def test_score_shape(tox, segments):
    scores = tox.score_text(segments[4]["text"])
    assert "toxic" in scores
    assert 0.0 <= float(scores["toxic"]) <= 1.0
    assert "model" in scores
