"""An LLM "who is this?" answer that is not a name must never become a speaker suggestion.

Staging showed a speaker labelled "Unknown (not Robert)": the model's way of saying it could
not tell, stored verbatim as ``suggested_name`` and, at >= 0.75 confidence, promoted to the
canonical label by every index and export writer.
"""

from __future__ import annotations

import pytest

from app.services.llm_service import LLMService
from app.utils.speaker_labels import looks_like_a_person_name

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "value",
    [
        "Unknown (not Robert)",
        "unknown",
        "Unidentified speaker",
        "N/A",
        "n/a",
        "None",
        "Not Robert",
        "Cannot determine",
        "SPEAKER_02",
        "Speaker 3",
        "",
        "   ",
        None,
        42,
        "x" * 81,
    ],
)
def test_a_non_name_is_rejected(value):
    assert looks_like_a_person_name(value) is False


@pytest.mark.parametrize(
    "value",
    ["Robert", "John Smith", "Dr. Alexandria Montgomery-Whitfield", "Maria José O'Neil", "Knott"],
)
def test_a_real_name_is_accepted(value):
    assert looks_like_a_person_name(value) is True


def _validate(name):
    svc = object.__new__(LLMService)
    return svc._validate_speaker_prediction(
        {"speaker_label": "SPEAKER_01", "predicted_name": name, "confidence": 0.9}
    )


def test_prediction_validation_drops_a_non_name_and_keeps_a_name():
    assert _validate("Unknown (not Robert)") is False
    assert _validate("Robert Whitfield") is True


def test_a_stored_non_name_suggestion_never_becomes_the_canonical_label():
    """Rows written before predictions were validated must not surface either."""
    from app.utils.speaker_labels import canonical_speaker_label

    assert (
        canonical_speaker_label("SPEAKER_01", suggested_name="Unknown (not Robert)", confidence=0.9)
        == "SPEAKER_01"
    )
    assert (
        canonical_speaker_label("SPEAKER_01", suggested_name="Robert Whitfield", confidence=0.9)
        == "Robert Whitfield"
    )
