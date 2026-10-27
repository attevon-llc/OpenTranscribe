"""Toxicity detector tests.

Marked ``models``. Skipped when transformers or the toxicity weights are NOT on this machine;
when the weights are cached and the load still fails, the test FAILS (a skip there would hide
a transformers/torch incompatibility while production silently loses toxicity scoring).
Pins score *ranges* (not exact values) so minor model updates don't break CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fixtures.model_weights import hf_weights_cached
from fixtures.model_weights import require_loaded

pytestmark = pytest.mark.models

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "redaction"


@pytest.fixture(scope="module")
def tox():
    pytest.importorskip("transformers")
    from app.core import constants as C  # noqa: N812
    from app.services.redaction.detectors import toxicity

    require_loaded(
        toxicity.preload(),
        available=hf_weights_cached(C.REDACTION_TOXICITY_MODEL_EN),
        what=f"Toxicity model {C.REDACTION_TOXICITY_MODEL_EN}",
    )
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
