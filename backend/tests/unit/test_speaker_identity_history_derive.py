"""What one change records in the speaker identity history (issue #1336): the pure derivation.

``derive_event`` decides the kind, who, which model, the voiceprint flag and the before/after
values from the changed fields, the row after the change and the helpers' attribution notes.
The database half (the flush listener actually writing the row) is
``test_speaker_identity_history_flush.py``.
"""

from __future__ import annotations

import pytest

from app.models.speaker_identity_event import EVENT_KINDS
from app.utils.speaker_identity_history import derive_event

_HUMAN = {
    "identity_source": "human",
    "identity_source_user_id": 7,
    "identity_source_detail": {"origin": "typed"},
}
_MACHINE = {
    "identity_source": "voiceprint",
    "identity_source_user_id": None,
    "identity_source_detail": {
        "origin": "voice_match",
        "confidence": 0.91,
        "embedding_model": "wespeaker-r34",
        "matched_profile_uuid": "p-1",
    },
}


def test_nothing_changed_records_nothing():
    assert derive_event({}, _HUMAN, []) is None


def test_a_typed_rename_is_the_persons_with_before_and_after():
    draft = derive_event(
        {"display_name": ("Bob", "Robert")}, _HUMAN, [{"user_id": 7}], restamped=True
    )
    assert draft is not None
    assert (draft.kind, draft.actor_kind, draft.actor_user_id) == ("rename", "user", 7)
    assert (draft.before, draft.after) == ({"name": "Bob"}, {"name": "Robert"})
    assert (draft.origin, draft.voiceprint, draft.model) == ("typed", False, None)


def test_a_voiceprint_match_names_the_model_and_is_a_voiceprint_row():
    draft = derive_event(
        {
            "display_name": (None, "Alice"),
            "profile_id": (None, 4),
            "identity_source": (None, "voiceprint"),
        },
        _MACHINE,
        [],
        restamped=True,
    )
    assert draft is not None
    assert (draft.kind, draft.actor_kind, draft.actor_user_id) == ("rename", "system", None)
    assert (draft.model, draft.confidence, draft.voiceprint) == ("wespeaker-r34", 0.91, True)
    assert draft.after == {"name": "Alice", "profile_id": 4, "identity_source": "voiceprint"}
    assert draft.detail == _MACHINE["identity_source_detail"]


def test_a_persons_rename_of_a_voiceprint_name_does_not_copy_that_name():
    """The machine's value lives only in the machine's row, which a purge deletes."""
    draft = derive_event(
        {"display_name": ("Alice", "Alicia"), "identity_source": ("voiceprint", "human")},
        _HUMAN,
        [{"user_id": 7}],
        restamped=True,
    )
    assert draft is not None and draft.voiceprint is False
    assert draft.before == {"withheld": ["name"], "identity_source": "voiceprint"}
    assert draft.after == {"name": "Alicia", "identity_source": "human"}


def test_control_a_persons_rename_of_a_persons_name_keeps_the_old_name():
    draft = derive_event(
        {"display_name": ("Al", "Alicia")}, _HUMAN, [{"user_id": 7}], restamped=True
    )
    assert draft is not None and draft.before == {"name": "Al"}


def test_an_accepted_suggestion_is_its_own_kind():
    row = {**_HUMAN, "identity_source_detail": {"origin": "accepted_suggestion"}}
    draft = derive_event({"display_name": (None, "Bob")}, row, [{"user_id": 7}], restamped=True)
    assert draft is not None and draft.kind == "suggestion_accepted"


def test_an_unlink_by_a_person_withholds_a_voiceprint_link():
    row = {"identity_source": None, "identity_source_user_id": None}
    draft = derive_event(
        {"profile_id": (4, None), "identity_source": ("voiceprint_link", None)},
        row,
        [{"user_id": 9}],
    )
    assert draft is not None
    assert (draft.kind, draft.actor_user_id, draft.voiceprint) == ("unlink", 9, False)
    assert draft.before == {"withheld": ["profile_id"], "identity_source": "voiceprint_link"}
    assert draft.after == {"profile_id": None, "identity_source": None}


def test_a_cleared_name_is_name_cleared():
    draft = derive_event({"display_name": ("Bob", None)}, _HUMAN, [{"user_id": 7}])
    assert draft is not None and draft.kind == "name_cleared"


def test_an_llm_suggestion_is_not_a_voiceprint_row_and_names_its_model():
    row = {
        "identity_source": None,
        "suggestion_source": "llm_analysis",
        "suggestion_detail": {"origin": "llm", "model": "qwen3", "confidence": 0.6},
    }
    draft = derive_event({"suggested_name": (None, "Carol")}, row, [])
    assert draft is not None
    assert (draft.kind, draft.voiceprint, draft.model, draft.confidence) == (
        "suggested",
        False,
        "qwen3",
        0.6,
    )


def test_a_voice_suggestion_is_a_voiceprint_row():
    row = {"suggestion_source": "voice_match", "suggestion_detail": {"origin": "voice_match"}}
    draft = derive_event({"suggested_name": (None, "Carol")}, row, [])
    assert draft is not None and draft.voiceprint is True


def test_withdrawing_a_voice_suggestion_does_not_copy_it():
    row = {"suggestion_source": "voice_match"}
    draft = derive_event({"suggested_name": ("Carol", None)}, row, [{"user_id": 3}])
    assert draft is not None
    assert (draft.kind, draft.voiceprint) == ("suggestion_cleared", False)
    assert draft.before == {"withheld": ["suggested_name"]}


def test_an_explicit_kind_wins_and_records_with_no_field_change():
    draft = derive_event(
        {},
        {},
        [{"user_id": 2, "kind": "merge", "before": {"merged_name": "SPEAKER_02"}}],
    )
    assert draft is not None
    assert (draft.kind, draft.actor_user_id, draft.before) == (
        "merge",
        2,
        {"merged_name": "SPEAKER_02"},
    )


def test_a_rejection_is_recorded_as_the_persons():
    draft = derive_event(
        {"suggested_name": ("Dan", None), "profile_id": (5, None)},
        {"identity_source": None},
        [{"user_id": 4}, {"user_id": 4, "kind": "suggestion_rejected"}],
    )
    assert draft is not None
    assert (draft.kind, draft.actor_user_id, draft.voiceprint) == ("suggestion_rejected", 4, False)


def test_a_confirmation_with_no_value_change():
    row = {**_HUMAN, "identity_source_detail": {"origin": "confirmed"}}
    draft = derive_event({}, row, [{"user_id": 7}], restamped=True)
    assert draft is not None and draft.kind == "confirmed"


def test_a_cluster_merge_identity_is_a_merge():
    row = {**_HUMAN, "identity_source_detail": {"origin": "cluster_merged"}}
    draft = derive_event({"display_name": ("A", "B")}, row, [{"user_id": 7}], restamped=True)
    assert draft is not None and draft.kind == "merge"


def test_the_model_of_an_accepted_llm_suggestion_is_kept():
    row = {
        **_HUMAN,
        "identity_source_detail": {
            "origin": "accepted_suggestion",
            "accepted_suggestion": {"source": "llm_analysis", "llm": {"model": "qwen3"}},
        },
    }
    draft = derive_event({"display_name": (None, "Bob")}, row, [{"user_id": 7}], restamped=True)
    assert draft is not None and draft.model == "qwen3"


@pytest.mark.parametrize(
    ("changes", "row", "notes", "restamped"),
    [
        ({"display_name": ("a", "b")}, _HUMAN, [], True),
        ({"profile_id": (None, 1)}, _MACHINE, [], True),
        ({"profile_id": (1, None)}, {}, [{"user_id": 1}], False),
        ({"suggested_name": (None, "x")}, {"suggestion_source": "llm_analysis"}, [], False),
        ({"suggested_name": ("x", None)}, {}, [], False),
        ({"suggestion_source": (None, "user_skipped")}, {}, [{"user_id": 1}], False),
        ({}, _HUMAN, [], True),
        ({}, {}, [{"kind": "merge"}], False),
    ],
)
def test_every_kind_derived_is_one_the_database_admits(changes, row, notes, restamped):
    draft = derive_event(changes, row, notes, restamped=restamped)
    assert draft is not None and draft.kind in EVENT_KINDS
