"""Unit tests for the read-time masking function (pure, GPU-free, no network).

Golden-file driven: detection is simulated with known char offsets so the *masking*
layer is verified deterministically. Real model detection is covered by the
``@pytest.mark.models`` / integration suites.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.redaction.detectors import wordlist
from app.services.redaction.spans import RedactionSpan
from app.services.redaction.spans import apply_redactions

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "redaction"

# Simulated PII entities per segment index (substring → entity_type).
_PII = {
    1: [("John Smith", "NAME"), ("john.smith@example.com", "EMAIL")],
    2: [("555-123-4567", "PHONE"), ("123-45-6789", "SSN")],
    3: [("4111 1111 1111 1111", "CREDIT_CARD")],
}
_CUSTOM_WORDS = ["Bluefin"]


@pytest.fixture(scope="module")
def segments() -> list[dict]:
    segs: list[dict] = json.loads((FIXTURES / "segments.json").read_text())
    return segs


@pytest.fixture(scope="module")
def expected_label() -> dict:
    expected: dict = json.loads((FIXTURES / "expected_label_style.json").read_text())
    return expected


def _all_spans(seg: dict) -> list[RedactionSpan]:
    """Assemble pii (simulated) + profanity + custom spans for one segment."""
    text = seg["text"]
    spans: list[RedactionSpan] = []
    for substr, etype in _PII.get(seg["idx"], []):
        start = text.find(substr)
        assert start >= 0, f"fixture substring not found: {substr}"
        spans.append(
            RedactionSpan(
                char_start=start,
                char_end=start + len(substr),
                category="pii",
                entity_type=etype,
                detector="presidio",
                confidence=0.95,
            )
        )
    spans.extend(wordlist.find_profanity_spans(text, seg.get("words")))
    spans.extend(wordlist.find_custom_spans(text, _CUSTOM_WORDS, seg.get("words")))
    return spans


def test_label_style_matches_golden(segments, expected_label):
    """Every fixture segment masks to the committed golden output (label style)."""
    enabled = {"pii", "profanity", "custom"}
    for seg in segments:
        masked, _ = apply_redactions(seg["text"], _all_spans(seg), enabled_categories=enabled)
        assert masked == expected_label[str(seg["idx"])], f"segment {seg['idx']} mismatch"


def test_scunthorpe_not_masked(segments):
    """Word-boundary matching must not flag 'Scunthorpe' (contains a profanity substring)."""
    seg6 = segments[6]
    spans = wordlist.find_profanity_spans(seg6["text"])
    assert spans == []


def test_styles(segments):
    seg0 = segments[0]
    spans = wordlist.find_profanity_spans(seg0["text"], seg0["words"])
    aster, _ = apply_redactions(
        seg0["text"], spans, style="asterisks", enabled_categories={"profanity"}
    )
    assert aster == "This is ******* ridiculous, I can't believe it."
    first, _ = apply_redactions(
        seg0["text"], spans, style="first_letter", enabled_categories={"profanity"}
    )
    assert first == "This is f****** ridiculous, I can't believe it."
    blur, _ = apply_redactions(seg0["text"], spans, style="blur", enabled_categories={"profanity"})
    assert 'class="redacted"' in blur and 'data-cat="profanity"' in blur


def test_reveal_returns_original(segments):
    seg0 = segments[0]
    spans = wordlist.find_profanity_spans(seg0["text"], seg0["words"])
    masked, _ = apply_redactions(
        seg0["text"], spans, enabled_categories={"profanity"}, reveal_categories={"profanity"}
    )
    assert masked == seg0["text"]


def test_disabled_category_not_masked(segments):
    seg0 = segments[0]
    spans = wordlist.find_profanity_spans(seg0["text"], seg0["words"])
    masked, _ = apply_redactions(seg0["text"], spans, enabled_categories={"pii"})
    assert masked == seg0["text"]  # profanity not in enabled set


def test_out_of_bounds_clamped():
    spans = [RedactionSpan(char_start=100, char_end=200, category="pii", entity_type="NAME")]
    masked, applied = apply_redactions("short text", spans, enabled_categories={"pii"})
    assert masked == "short text"
    assert applied == []


def test_overlap_priority():
    """Overlapping spans merge; PII outranks profanity for the surviving label."""
    text = "the secret word here"
    spans = [
        RedactionSpan(char_start=4, char_end=10, category="profanity", entity_type="PROFANITY"),
        RedactionSpan(char_start=4, char_end=15, category="pii", entity_type="NAME"),
    ]
    masked, applied = apply_redactions(text, spans, enabled_categories={"pii", "profanity"})
    assert masked == "the [NAME] here"
    assert len(applied) == 1 and applied[0].entity_type == "NAME"
