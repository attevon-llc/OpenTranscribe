"""Unit tests for the wordlist detector (profanity + custom + allowlist, char↔word)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.redaction.detectors import wordlist
from app.services.redaction.spans import build_word_offsets
from app.services.redaction.spans import map_char_span_to_words

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "redaction"


@pytest.fixture(scope="module")
def segments() -> list[dict]:
    segs: list[dict] = json.loads((FIXTURES / "segments.json").read_text())
    return segs


def test_profanity_span_offsets(segments):
    seg0 = segments[0]
    spans = wordlist.find_profanity_spans(seg0["text"], seg0["words"])
    assert len(spans) == 1
    s = spans[0]
    assert seg0["text"][s.char_start : s.char_end] == "fucking"
    assert s.category == "profanity" and s.entity_type == "PROFANITY"


def test_custom_word_span(segments):
    seg5 = segments[5]
    spans = wordlist.find_custom_spans(seg5["text"], ["Bluefin"], seg5.get("words"))
    assert len(spans) == 1
    assert spans[0].category == "custom" and spans[0].entity_type == "CUSTOM"
    assert seg5["text"][spans[0].char_start : spans[0].char_end] == "Bluefin"


def test_scunthorpe_word_boundary(segments):
    seg6 = segments[6]
    assert wordlist.find_profanity_spans(seg6["text"]) == []


def test_allowlist_suppresses_match():
    text = "that is crap honestly"
    assert len(wordlist.find_profanity_spans(text)) == 1
    assert wordlist.find_profanity_spans(text, allowlist=["crap"]) == []


def test_custom_word_case_insensitive():
    spans = wordlist.find_custom_spans("the BLUEFIN project", ["bluefin"])
    assert len(spans) == 1


def test_char_word_mapping(segments):
    """The profanity span in seg0 maps to word index 2 ('fucking')."""
    seg0 = segments[0]
    spans = wordlist.find_profanity_spans(seg0["text"], seg0["words"])
    assert spans[0].word_start == 2 and spans[0].word_end == 2


def test_build_word_offsets_alignment(segments):
    seg0 = segments[0]
    offsets = build_word_offsets(seg0["text"], seg0["words"])
    assert len(offsets) == len(seg0["words"])
    # The 3rd word ('fucking') offset should slice back to 'fucking'.
    ws, we = offsets[2]
    assert seg0["text"][ws:we] == "fucking"


def test_map_char_span_to_words():
    offsets = [(0, 4), (5, 7), (8, 15)]
    assert map_char_span_to_words(offsets, 8, 15) == (2, 2)
    assert map_char_span_to_words([], 0, 5) == (None, None)
