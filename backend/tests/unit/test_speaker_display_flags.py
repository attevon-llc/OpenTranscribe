"""A machine's guess must never render as a name a human confirmed.

``_compute_display_flags`` builds the ``input_placeholder`` the speaker editor
shows inside the name field. For a *high*-confidence suggestion it emitted the
**bare** ``suggested_name`` — and the frontend's ``translatePlaceholder``
returns anything it doesn't recognise as-is — so a >=0.75 voice match appeared
in the input looking exactly like an established value, with only a border
colour to say otherwise. That contradicts the repo-wide contract that speaker-ID
suggestions are surfaced for manual verification and never auto-applied.

Confirmed vs. guessed is keyed off ``display_name`` (a human set it; both
speaker write endpoints flip ``verified`` when they do), never off
``resolved_display_name``, which folds a confident suggestion into the same
string as a confirmed name.
"""

from __future__ import annotations

import pytest

from app.api.endpoints.speakers import _compute_display_flags
from app.models.media import Speaker

pytestmark = pytest.mark.unit


def _unnamed_speaker(confidence: float | None = 0.9) -> Speaker:
    """An unsaved row — this helper is pure formatting and touches no session."""
    return Speaker(name="SPEAKER_00", display_name=None, confidence=confidence)


def _profile_suggestion(confidence: float) -> dict:
    return {
        "name": "Priya Patel",
        "confidence": confidence,
        "confidence_percentage": int(confidence * 100),
        "suggestion_type": "voice_match",
        "reason": "",
    }


def test_a_high_confidence_suggestion_is_still_marked_as_a_suggestion():
    flags = _compute_display_flags(
        _unnamed_speaker(), "Priya Patel", "voice_match", [_profile_suggestion(0.92)]
    )

    assert flags["is_high_confidence"] is True
    assert flags["input_placeholder"] == "Suggested: Priya Patel"


def test_a_medium_confidence_suggestion_is_marked_as_a_suggestion():
    flags = _compute_display_flags(
        _unnamed_speaker(), "Priya Patel", "voice_match", [_profile_suggestion(0.6)]
    )

    assert flags["is_medium_confidence"] is True
    assert flags["input_placeholder"] == "Suggested: Priya Patel"


def test_a_metadata_hint_keeps_its_own_provenance_prefix():
    flags = _compute_display_flags(
        _unnamed_speaker(), "Priya Patel", "metadata_hint", [_profile_suggestion(0.92)]
    )

    assert flags["input_placeholder"] == "From metadata: Priya Patel"


def test_no_suggestion_falls_back_to_the_diarization_label():
    flags = _compute_display_flags(_unnamed_speaker(confidence=None), None, None, [])

    assert flags["input_placeholder"] == "Label SPEAKER_00"
