"""``utils/speaker_identity_source`` and the label contract that reads it (#1280, Phase 0).

The helpers record who wrote a speaker's name and profile link; the label hides a name a
voiceprint wrote while the file's voiceprints are off. Every hiding case has a control with
the switch on (or a person's identity) beside it, so "always hide" cannot pass.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.utils import speaker_identity_source as ids
from app.utils.speaker_labels import canonical_speaker_label
from app.utils.speaker_labels import canonical_speaker_label_for_row

_DETAIL = {"origin": "voice_match"}


def _row(**kw):
    base = {
        "identity_source": None,
        "display_name": None,
        "identity_source_at": None,
        "identity_source_user_id": None,
        "identity_source_detail": None,
        "suggested_name": None,
        "suggestion_source": None,
        "confidence": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


# --- writers' helpers ---------------------------------------------------------------------


def test_a_machine_written_name_is_a_voiceprint_identity():
    row = _row(display_name="Alice")
    ids.record_machine_identity(row, previous_display_name=None, detail=_DETAIL)
    assert row.identity_source == "voiceprint"


@pytest.mark.parametrize("previous", ["Alice", "alice ", "ALICE"])
def test_a_machine_write_onto_the_same_typed_name_records_only_the_link(previous):
    row = _row(display_name="Alice")
    ids.record_machine_identity(row, previous_display_name=previous, detail=_DETAIL)
    assert row.identity_source == "voiceprint_link"


@pytest.mark.parametrize("previous", ["Bob", "SPEAKER_03", ""])
def test_a_machine_write_over_a_different_or_placeholder_name_is_a_voiceprint(previous):
    row = _row(display_name="Alice")
    ids.record_machine_identity(row, previous_display_name=previous, detail=_DETAIL)
    assert row.identity_source == "voiceprint"


def test_a_voiceprint_identity_is_never_downgraded_by_a_later_machine_write():
    row = _row(display_name="Alice", identity_source="voiceprint")
    ids.record_machine_identity(row, previous_display_name="Alice", detail=_DETAIL)
    assert row.identity_source == "voiceprint"
    ids.record_machine_link(row, detail=_DETAIL)
    assert row.identity_source == "voiceprint"


@pytest.mark.parametrize("before", [None, "human", "voiceprint_link"])
def test_a_machine_link_alone_is_a_link_identity(before):
    row = _row(display_name="Alice", identity_source=before)
    ids.record_machine_link(row, detail=_DETAIL)
    assert row.identity_source == "voiceprint_link"


@pytest.mark.parametrize("before", [None, "voiceprint", "voiceprint_link", "human"])
def test_a_person_makes_any_identity_human(before):
    row = _row(display_name="Alice", identity_source=before)
    ids.record_human_identity(row, user_id=1, origin="typed")
    assert row.identity_source == "human"


@pytest.mark.parametrize(
    ("display_name", "before", "after"),
    [
        (None, "voiceprint", None),  # name emptied: nothing left to describe
        ("", "human", None),
        ("Alice", "voiceprint_link", None),  # the name predates the machine's link
        ("Alice", "voiceprint", "voiceprint"),  # the name itself is still the machine's
        ("Alice", "human", "human"),
        ("Alice", None, None),
    ],
)
def test_after_unlink(display_name, before, after):
    row = _row(display_name=display_name, identity_source=before)
    ids.after_unlink(row)
    assert row.identity_source == after


@pytest.mark.parametrize(
    ("profile_id", "source", "enabled", "expected"),
    [
        (7, "voiceprint", False, None),
        (7, "voiceprint_link", False, None),
        (7, "human", False, 7),
        (7, None, False, 7),
        (7, "voiceprint", True, 7),
        (7, "voiceprint_link", True, 7),
        (None, "voiceprint", True, None),
    ],
)
def test_visible_profile_id_matrix(profile_id, source, enabled, expected):
    assert ids.visible_profile_id(profile_id, source, enabled) == expected


# --- the pure label ------------------------------------------------------------------------


def _label(**kw: Any) -> str:
    fields: dict[str, Any] = {"display_name": "Alice", "verified": True}
    fields.update(kw)
    return canonical_speaker_label("SPEAKER_00", **fields)


def test_a_voiceprint_name_falls_back_to_the_diarizer_label_when_off():
    assert _label(identity_source="voiceprint", voiceprint_names=False) == "SPEAKER_00"
    assert _label(identity_source="voiceprint", voiceprint_names=True) == "Alice"


def test_a_voiceprint_link_keeps_its_name_when_off():
    assert _label(identity_source="voiceprint_link", voiceprint_names=False) == "Alice"


@pytest.mark.parametrize("source", ["human", None])
def test_a_human_or_unknown_name_is_never_hidden(source):
    assert _label(identity_source=source, voiceprint_names=False) == "Alice"


def _suggested(**kw: Any) -> str:
    fields: dict[str, Any] = {
        "display_name": None,
        "suggested_name": "Alice",
        "suggestion_source": "voice_match",
        "confidence": 0.9,
    }
    fields.update(kw)
    return canonical_speaker_label("SPEAKER_00", **fields)


def test_a_voice_suggestion_is_not_promoted_when_off():
    assert _suggested(verified=False, voiceprint_names=False) == "SPEAKER_00"
    assert _suggested(verified=False, voiceprint_names=True) == "Alice"


def test_a_suggestion_a_person_verified_stays_unless_a_voiceprint_did_the_verifying():
    assert _suggested(verified=True, voiceprint_names=False) == "Alice"
    assert (
        _suggested(verified=True, identity_source="voiceprint", voiceprint_names=False)
        == "SPEAKER_00"
    )


def test_a_non_voiceprint_suggestion_is_never_promoted_either_way():
    assert _suggested(suggestion_source="llm_analysis", voiceprint_names=True) == "SPEAKER_00"


# --- the row label -------------------------------------------------------------------------


def _stand_in(**kw):
    fields = {
        "name": "SPEAKER_00",
        "display_name": "Bob",
        "suggested_name": None,
        "confidence": None,
        "suggestion_source": None,
        "verified": True,
    }
    fields.update(kw)
    return SimpleNamespace(**fields)


def test_for_row_without_identity_source_raises_when_off():
    row = _stand_in()
    with pytest.raises(TypeError, match="identity_source"):
        canonical_speaker_label_for_row(row, voiceprint_names=False)


def test_for_row_without_identity_source_still_labels_when_on():
    assert canonical_speaker_label_for_row(_stand_in(), voiceprint_names=True) == "Bob"


def test_for_row_without_identity_source_and_without_a_name_needs_no_answer():
    """Nothing a person or a machine named: the diarizer label is safe either way."""
    row = _stand_in(display_name=None)
    assert canonical_speaker_label_for_row(row, voiceprint_names=False) == "SPEAKER_00"


@pytest.mark.parametrize(
    ("source", "off_label"),
    [("voiceprint", "SPEAKER_00"), ("voiceprint_link", "Bob"), ("human", "Bob"), (None, "Bob")],
)
def test_for_row_applies_a_decision_the_caller_passes(source, off_label):
    row = _stand_in(identity_source=source)
    assert canonical_speaker_label_for_row(row, voiceprint_names=False) == off_label
    assert canonical_speaker_label_for_row(row, voiceprint_names=True) == "Bob"
