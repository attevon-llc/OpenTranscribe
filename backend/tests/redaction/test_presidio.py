"""PII detector tests (Presidio + GLiNER).

Marked ``models``. Skipped when presidio / the spaCy pipeline / the GLiNER weights are NOT on
this machine (e.g. fast CI); when they ARE present and fail to load, or the configured name
backend silently degrades to spaCy, the tests FAIL. Run in the celery-redaction container or a
full env:
    pytest -m models tests/redaction/test_presidio.py
Asserts on entity categories + offsets (not brittle model internals).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fixtures.model_weights import hf_weights_cached
from fixtures.model_weights import package_installed
from fixtures.model_weights import require_loaded

pytestmark = pytest.mark.models

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "redaction"


@pytest.fixture(scope="module")
def detector():
    pytest.importorskip("presidio_analyzer")
    from app.services.redaction.detectors import pii_presidio

    require_loaded(
        pii_presidio.preload(),
        available=package_installed("en_core_web_sm"),
        what="Presidio analyzer with the en_core_web_sm spaCy pipeline",
    )
    return pii_presidio


@pytest.fixture(scope="module")
def segments() -> list[dict]:
    segs: list[dict] = json.loads((FIXTURES / "segments.json").read_text())
    return segs


def _entity_types(spans) -> set[str]:
    return {s.entity_type for s in spans}


def _cfg() -> dict:
    from app.services.redaction.config import detection_config_for_all

    return detection_config_for_all()


def test_email_and_name(detector, segments):
    spans = detector.detect_pii(segments[1]["text"], None, _cfg())
    types = _entity_types(spans)
    assert "EMAIL" in types
    assert "NAME" in types  # whichever backend the config selects -- see the backend tests below


def test_phone_and_ssn(detector, segments):
    spans = detector.detect_pii(segments[2]["text"], None, _cfg())
    types = _entity_types(spans)
    assert "PHONE" in types
    assert "SSN" in types


def test_credit_card(detector, segments):
    spans = detector.detect_pii(segments[3]["text"], None, _cfg())
    assert "CREDIT_CARD" in _entity_types(spans)


def test_offsets_slice_back(detector, segments):
    text = segments[2]["text"]
    spans = detector.detect_pii(text, None, _cfg())
    # Zero detected spans executed the loop zero times and the test passed — the offset
    # invariant went unchecked on every run where detection silently returned nothing, and
    # `redaction/spans.py` is a mutation-testing target precisely because an off-by-one here
    # leaks the character it should hide (issue #431). This segment carries a phone number
    # and an SSN (asserted by test_phone_and_ssn), so an empty result is a real failure.
    assert spans, "detector returned no spans — the offset invariant below would be vacuous"
    for s in spans:
        # Each span must slice to non-empty text within bounds.
        assert 0 <= s.char_start < s.char_end <= len(text)
        assert text[s.char_start : s.char_end], "span sliced to empty text"


def _gliner_registered(analyzer) -> bool:
    return any("gliner" in type(r).__name__.lower() for r in analyzer.registry.recognizers)


def test_the_default_name_backend_is_the_configured_one(detector):
    """The name backend follows ``REDACTION_PII_USE_GLINER``; GLiNER is opt-in and off by default.

    Without this, ``test_email_and_name`` is satisfied by either backend and cannot tell a
    configured spaCy run from a GLiNER run that silently fell back.
    """
    from app.core import constants as C  # noqa: N812

    analyzer = detector._get_analyzer(C.REDACTION_PII_USE_GLINER)
    assert _gliner_registered(analyzer) is bool(C.REDACTION_PII_USE_GLINER)


@pytest.fixture
def gliner_analyzer(detector):
    """An analyzer built with GLiNER requested; the default one is restored afterwards."""
    from app.core import constants as C  # noqa: N812

    if not hf_weights_cached(C.REDACTION_PII_GLINER_MODEL):
        pytest.skip(f"GLiNER weights {C.REDACTION_PII_GLINER_MODEL} not cached on this machine")
    analyzer = detector._get_analyzer(True)
    yield analyzer
    detector._get_analyzer(C.REDACTION_PII_USE_GLINER)


def test_gliner_is_used_when_requested_and_its_weights_are_cached(gliner_analyzer):
    """``_build_analyzer`` swallows a GLiNER load error and falls back to spaCy with a warning.

    With the weights cached that fallback is a regression (e.g. a gliner/transformers
    incompatibility), and the name-detection tests above would still pass on spaCy alone.
    """
    assert _gliner_registered(gliner_analyzer), (
        "GLiNER was requested and its weights are cached, but the analyzer fell back to "
        "spaCy NER for names; see the 'GLiNER recognizer unavailable' warning"
    )


def test_gliner_detects_a_name_when_enabled(detector, gliner_analyzer, segments):
    cfg = {**_cfg(), "pii_use_gliner": True}
    spans = detector.detect_pii(segments[1]["text"], None, cfg)
    assert "NAME" in _entity_types(spans)
