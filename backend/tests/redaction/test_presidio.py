"""PII detector tests (Presidio + GLiNER).

Marked ``models`` and auto-skipped when presidio/spaCy/GLiNER or their weights are
unavailable (e.g. fast CI). Run in the celery-redaction container or a full env:
    pytest -m models tests/redaction/test_presidio.py
Asserts on entity categories + offsets (not brittle model internals).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.models

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "redaction"


@pytest.fixture(scope="module")
def detector():
    pytest.importorskip("presidio_analyzer")
    from app.services.redaction.detectors import pii_presidio

    if not pii_presidio.preload():
        pytest.skip("Presidio analyzer/model unavailable")
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
    assert "NAME" in types  # GLiNER or spaCy NER


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
